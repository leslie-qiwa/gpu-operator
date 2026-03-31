#!/bin/bash

#set -x

function usage() {
    echo ""
    echo "Usage: $0 [options]"
    echo "          --help print help/usage information"
    echo "          --deployment <deployment> Eg: k8, openshift"
    echo "          --app <app-name> Eg: gpu-operator"
    echo "          --base-version <version> Eg: v1.4.1 (released version to upgrade FROM)"
    echo "          --testbed /path/to/testbed.json, default /warmd.json"
    echo "          --seed-image-manifest <path-to-seed-image-manifest> (RC images to upgrade TO)"
    echo ""
}

LOCAL_REGISTRY_PORT="5000"
REGISTRY_SELECTION="local"
TESTBED_JSON="/warmd.json"
DEPLOYMENT="NA"
BASE_VERSION="NA"
AMDGPU_DRIVER="default-deviceconfig"
GEN_RC_MANIFEST="/tmp/rc_images.yaml"
SEED_IMAGE_MANIFEST="/gpu-operator/ci-internal/sanity-images.yml"
GLOBAL_REGISTRY="docker.io/amdpsdo"
APP_NAME="NA"
K8_VERSION="1.35.2"

function collect_logs() {
    echo "Collect test run logs"
    tar -zcf pytest_logs.tgz logs/
    ls -ltr $PWD/pytest_logs.tgz
}

function upload_reports() {
    echo "JOB_ID=${JOB_ID}"
    echo "TARGET_NAME=${TARGET_NAME}"
    echo "TARGET_ID=${TARGET_ID}"
    echo "JOB_PR=${JOB_PR}"
    if [[ ! -z "${JOB_ID}" ]] ;
    then
        echo "Using JOBD Environment variables to evaluate PR/JOB/Target"
        final_report="${TARGET_ID}.html"
        sshpass -p vm timeout 30 ssh -o LogLevel=ERROR -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no vm@10.11.18.6 "rm -rf /var/www/html/${TARGET_NAME}/${TARGET_ID}"
        sshpass -p vm timeout 30 ssh -o LogLevel=ERROR -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no vm@10.11.18.6 "mkdir -p /var/www/html/${TARGET_NAME}/${TARGET_ID}"
        local HTML_REPORT_FILE="${DEPLOYMENT}/${APP_NAME}_upgrade.html"
        sshpass -p vm timeout 30 scp -o LogLevel=ERROR -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no ${PWD}/logs/${HTML_REPORT_FILE} vm@10.11.18.6:/var/www/html/${TARGET_NAME}/${TARGET_ID}/${final_report}

        echo "Links:"
        echo "Consolidated report       : http://10.11.18.6/${TARGET_NAME}/${TARGET_ID}/${final_report}"
        echo ""
    fi
}

function start_registry() {
    # Maximum number of retries
    local MAX_RETRIES=20
    # Retry counter
    local RETRY_COUNT=0

    # Start Docker if START_DOCKER is set
    if [ ! -z "$START_DOCKER" ]; then
        echo "START_DOCKER is set. Attempting to start Docker..."
        dockerd -s vfs &
        sleep 2
    fi

    # Check if a `docker run` command succeeds
    while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
        # start local docker registry for upgrade ci test
        docker run -d -p $LOCAL_REGISTRY_PORT:$LOCAL_REGISTRY_PORT --name registry --restart always registry.test.pensando.io:5000/pensando/registry:2
        if [ $? -eq 0 ]; then
            echo "Registry is ready, mark it insecure for pushing images"
            DOCKER_CONFIG_FILE="/etc/docker/daemon.json"
            sudo jq --arg host_ip "$HOST_IP" --arg reg_port "$LOCAL_REGISTRY_PORT"   '.["insecure-registries"] += ["\($host_ip):\($reg_port)"]' "$DOCKER_CONFIG_FILE" > /tmp/daemon.json.tmp && sudo mv /tmp/daemon.json.tmp "$DOCKER_CONFIG_FILE"
            sudo pkill -HUP dockerd
            sleep 30
            return
        fi

        ((RETRY_COUNT++))
        echo "Registry is not ready. Retrying... ($RETRY_COUNT/$MAX_RETRIES)"
        sleep 2
    done

    echo "Docker is not ready after $MAX_RETRIES attempts."
    exit 1
}

function setup_registry() {
    echo ""
    echo "Setting up registry configuration"
    jq -n '{}' > $PWD/registries.json

    # Always start local registry
    start_registry

    # Driver registry is always local
    DRIVER_REGISTRY="${HOST_IP}:${LOCAL_REGISTRY_PORT}"
    SECURE="no"
    TYPE="hosted"
    jq --arg val "$DRIVER_REGISTRY" --arg sec "$SECURE" --arg reg_type "$TYPE" \
        '.["driver-registry"] = {"value": $val, "secure": $sec, "type" : $reg_type}' \
        $PWD/registries.json > /tmp/tmp.json && cp /tmp/tmp.json $PWD/registries.json

    # Image registry can be local or global (amdpsdo)
    if [[ ${REGISTRY_SELECTION} == "local" ]];
    then
        IMG_REGISTRY="${HOST_IP}:${LOCAL_REGISTRY_PORT}"
        SECURE="no"
        TYPE="hosted"
    elif [[ ${REGISTRY_SELECTION} == "amdpsdo" ]];
    then
        IMG_REGISTRY=${GLOBAL_REGISTRY}
        SECURE="yes"
        TYPE="global"
    else
        echo "FATAL ERROR: Invalid registry-selection - ABORT"
        exit 1
    fi
    jq --arg val "$IMG_REGISTRY" --arg sec "$SECURE" --arg reg_type "$TYPE" \
        '.["image-registry"] = {"value": $val, "secure": $sec, "type" : $reg_type }' \
        $PWD/registries.json > /tmp/tmp.json && cp /tmp/tmp.json $PWD/registries.json
    echo ""
    echo "Registry Information:"
    jq . $PWD/registries.json
    echo ""
}

function load_images() {
    echo ""
    echo "=========================================================================="
    echo "Loading images for upgrade testing"
    echo "=========================================================================="
    echo ""

    # Load base version images
    BASE_MANIFEST_PATH="/gpu-operator/tests/pytests/image-manifest/${APP_NAME}/${BASE_VERSION}_external_images.yaml"
    if [[ ! -f "${BASE_MANIFEST_PATH}" ]]; then
        echo "FATAL ERROR: Base version manifest not found: ${BASE_MANIFEST_PATH}"
        exit 1
    fi

    echo "Base version manifest: ${BASE_MANIFEST_PATH}"
    echo "RC version manifest  : ${SEED_IMAGE_MANIFEST}"

    # Load RC images and generate RC manifest (these are the current build artifacts)
    echo ""
    echo "Loading RC version images..."
    /gpu-operator/ci-internal/k8_jobd_ctl.py image --load-images --seed-image-manifest $SEED_IMAGE_MANIFEST --registries $PWD/registries.json --image-manifest $GEN_RC_MANIFEST --testbed $TESTBED_JSON --setup-insecure-registry --target $DEPLOYMENT
    RET=$?
    if [[ "$RET" != "0" ]]
    then
        echo "FATAL ERROR: Failed to load RC images and configure cluster registries"
        exit $RET
    fi

    echo ""
    echo "****** BASE VERSION IMAGES ******"
    cat ${BASE_MANIFEST_PATH}
    echo ""
    echo "****** RC VERSION IMAGES ******"
    cat ${GEN_RC_MANIFEST}
    echo ""

    echo "Pull RC images on worker nodes..."
    /gpu-operator/ci-internal/k8_jobd_ctl.py image --seed-image-manifest $SEED_IMAGE_MANIFEST --registries $PWD/registries.json --testbed $TESTBED_JSON --pull-images --target $DEPLOYMENT
    RET=$?
    if [[ "$RET" != "0" ]]
    then
        echo "WARNING: Failed to pull RC images on worker node(s)"
    fi
    echo ""
}

function prepare_cluster() {
    echo "Run k8_jobd_ctl to "
    echo "    (1) deploy k8"
    echo "    (2) reboot worker-nodes"
    echo "    (3) fetch kube-config"
    echo ""
    jq .Instances[].RawJSON ${TESTBED_JSON} | tee /gpu-operator/tests/pytests/testbed.json
    /gpu-operator/ci-internal/ansible/generate_inventory.py -i /gpu-operator/tests/pytests/testbed.json -o /gpu-operator/ci-internal/ansible/inventory.ini
    RET=$?
    if [[ "$RET" != "0" ]]
    then
        echo "FATAL ERROR: Could not generate inventory file"
        exit $RET
    fi
    if [[ "${DEPLOYMENT}" == "k8" ]];
    then
        pushd /gpu-operator/ci-internal/ansible

        # Step 1: Uninstall Docker
        echo "Uninstalling Docker..."
        ansible-playbook -i inventory.ini uninstall-docker.yml
        RET=$?
        if [[ "$RET" != "0" ]]
        then
            echo "WARNING: Docker uninstall had issues (may not be installed)"
        fi

        # Step 2: Uninstall GPU Operator if present
        echo "Uninstalling GPU Operator..."
        ansible-playbook -i inventory.ini uninstall-gpu-operator.yml
        RET=$?
        if [[ "$RET" != "0" ]]
        then
            echo "WARNING: GPU Operator uninstall had issues (may not be installed)"
        fi

        # Step 3: Uninstall K8s
        echo "Uninstalling Kubernetes..."
        ansible-playbook -i inventory.ini uninstall-k8s.yml
        RET=$?
        if [[ "$RET" != "0" ]]
        then
            echo "FATAL ERROR: Could not uninstall K8s"
            exit $RET
        fi
        sleep 30

        # Step 4: Install K8s
        echo "Installing Kubernetes ${K8_VERSION}..."
        ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=${K8_VERSION}"
        RET=$?
        if [[ "$RET" != "0" ]]
        then
            echo "FATAL ERROR: Could not deploy K8s ${K8_VERSION} and build k8-cluster"
            exit $RET
        fi

        # Step 5: Install Docker with insecure registry config
        echo "Installing Docker..."
        ansible-playbook -i inventory.ini install-docker.yml
        RET=$?
        if [[ "$RET" != "0" ]]
        then
            echo "FATAL ERROR: Could not install Docker"
            exit $RET
        fi

        # Step 6: Install Prometheus (optional, non-fatal)
        echo "Installing Prometheus Operator..."
        ansible-playbook -i inventory.ini install-prometheus.yml
        RET=$?
        if [[ "$RET" != "0" ]]
        then
            echo "WARNING: Prometheus installation failed - continuing anyway"
        else
            echo "Prometheus installed successfully"
        fi

        popd
    fi
    mkdir -p $HOME/.kube
    /gpu-operator/ci-internal/k8_jobd_ctl.py testbed --testbed $TESTBED_JSON --reboot-workers --fetch-kube-config --target $DEPLOYMENT
    RET=$?
    if [[ "$RET" != "0" ]]
    then
        echo "FATAL ERROR: Failed to reboot-worker nodes and/or fetch kube-config from master"
        exit $RET
    fi
    echo ""
}

function setup_python_env() {
    echo ""
    echo "========================================================================"
    echo "Setting up Python environment"
    echo "========================================================================"

    cd /gpu-operator/tests/pytests

    if [[ -f venv/bin/activate ]]; then
        echo "Python venv already exists, activating..."
        source venv/bin/activate
    else
        echo "Creating Python venv and installing dependencies..."
        ./scripts/prepare_env.sh $PWD/venv
        source $PWD/venv/bin/activate
    fi

    export PYTHONPATH=$PYTHONPATH:$PWD
    echo "Python environment ready"
    echo "========================================================================"
    echo ""
}

function launch_upgrade_tests() {
    echo ""
    echo "=========================================================================="
    echo "Launch upgrade tests"
    echo "=========================================================================="
    echo "Upgrade path: ${BASE_VERSION} → RC"
    echo ""

    local SECRETS="/tmp/secrets.json"
    curl -s http://pm.test.pensando.io/systest/gpu-operator-secrets/secrets.json -o ${SECRETS}

    # Pass RC manifest and base version to pytest
    CMD_OPTS=" --image-manifest ${GEN_RC_MANIFEST}"
    CMD_OPTS+=" --base-version ${BASE_VERSION}"
    CMD_OPTS+=" --secrets-json ${SECRETS}"
    CMD_OPTS+=" --amdgpu-driver-spec lib/files/amd-deviceconfig-default-driver-spec.json"

    echo "Running upgrade tests with CMD_OPTS: ${CMD_OPTS}"

    if [[ "${APP_NAME}" == "gpu-operator" ]];
    then
        cp /gpu-operator/tools/techsupport_dump.sh /gpu-operator/tests/pytests/gpu_operator_techsupport_dump.sh
        chmod +x /gpu-operator/tests/pytests/gpu_operator_techsupport_dump.sh
        export TECH_SUPPORT_TOOL=/gpu-operator/tests/pytests/gpu_operator_techsupport_dump.sh
    fi

    # Setup Python environment before running tests
    setup_python_env

    # Run upgrade tests only (using -m upgrade marker)
    mkdir -p logs
    pytest k8/${APP_NAME}/upgrade -m upgrade --verbose --show-capture=log --no-header \
        -p no:warnings --disable-warnings --self-contained-html \
        --deployment ${DEPLOYMENT} --log-file=logs/${DEPLOYMENT}_upgrade_test_run.log \
        --junit-xml=logs/${DEPLOYMENT}/${APP_NAME}_upgrade.xml \
        --html logs/${DEPLOYMENT}/${APP_NAME}_upgrade.html \
        ${CMD_OPTS}
    RET=$?

    echo ""
    /gpu-operator/ci-internal/k8_jobd_ctl.py report --show --testbed $TESTBED_JSON --target $DEPLOYMENT
    echo ""
    upload_reports
    collect_logs
    if [[ "$RET" != "0" ]]
    then
        exit $RET
    fi
}

function start_pod_monitor() {
    echo ""
    echo "========================================================================"
    echo "Starting Pod Monitor"
    echo "========================================================================"

    # Configure pod monitor
    export POD_MONITOR_NAMESPACES="${POD_MONITOR_NAMESPACES:-kube-amd-gpu,kube-amd-exporter,default,openshift-amd-gpu}"
    export POD_MONITOR_LOG="${POD_MONITOR_LOG:-/gpu-operator/tests/pytests/logs/upgrade-pod-monitor-$(date +%Y%m%d_%H%M%S).log}"
    export POD_MONITOR_FORMAT="${POD_MONITOR_FORMAT:-text}"
    export POD_MONITOR_INTERVAL="${POD_MONITOR_INTERVAL:-5}"

    # Ensure logs directory exists
    mkdir -p /gpu-operator/tests/pytests/logs

    # Start pod monitor
    if /gpu-operator/ci-internal/pod_monitor.sh start; then
        echo "Pod monitor started successfully"
        # Ensure pod monitor stops on script exit
        trap '/gpu-operator/ci-internal/pod_monitor.sh stop' EXIT INT TERM
    else
        echo "WARNING: Pod monitor failed to start - continuing without monitoring"
        echo "Check that kr8s is installed and kubeconfig is accessible"
    fi

    echo "========================================================================"
    echo ""
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --deployment)
            DEPLOYMENT="$2"
            shift
        ;;
        --app)
            APP_NAME="$2"
            shift
        ;;
        --base-version)
            BASE_VERSION="$2"
            shift
        ;;
        --testbed)
            TESTBED_JSON="$2"
            shift
        ;;
        --seed-image-manifest)
            SEED_IMAGE_MANIFEST="$2"
            shift
        ;;
        --help)
            usage
            exit 0
        ;;
        --*)
            echo "Unknown option $1"
            exit 1
        ;;
    esac
    shift
done

if [[ "${BASE_VERSION}" == "NA" ]];
then
    echo "ERROR: Missing argument --base-version"
    usage
    exit 1
fi

if [[ "${APP_NAME}" == "NA" ]];
then
    echo "ERROR: Missing argument --app"
    usage
    exit 1
fi

if [[ "${DEPLOYMENT}" == "NA" ]];
then
    echo "ERROR: Missing argument --deployment"
    usage
    exit 1
fi

function main() {
    prepare_cluster
    setup_registry
    load_images
    echo "Completed setting up environment for upgrade testing"

    # Start pod monitoring before running tests
    start_pod_monitor

    launch_upgrade_tests
}

main
