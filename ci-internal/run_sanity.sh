#!/bin/bash

#set -x

function usage() {
    echo ""
    echo "Usage: $0 [options]"
    echo "          --help print help/usage information"
    echo "          --deployment <deployment> Eg: k8, openshift, standalone"
    echo "          --k8-version <k8-version> Eg: 1.29.14 or 1.33.9, default 1.35.2"
    echo "          --app <app-name> Eg: gpu-operator, exporter, network-operator, debian, docker"
    echo "          --module <module-name>"
    echo "          --registry <selection: local|amdpsdo>"
    echo "          --testbed /path/to/testbed.json, default /warmd.json"
    echo "          --amdgpu-driver <selection: inbox|default-deviceconfig|{version} eg: 7.0.1>"
    echo "          --seed-image-manifest <path-to-seed-image-manifest>"
    echo ""
}

LOCAL_REGISTRY_PORT="5000"
REGISTRY_SELECTION="local"
TESTBED_JSON="/warmd.json"
DEPLOYMENT="NA"
MODULE="ALL"
AMDGPU_DRIVER="default-deviceconfig"
GEN_IMAGE_MANIFEST="/tmp/images.yaml"
SEED_IMAGE_MANIFEST="/gpu-operator/ci-internal/sanity-images.yml"
GLOBAL_REGISTRY="docker.io/amdpsdo"
TYPE="NA"
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
        local HTML_REPORT_FILE="${DEPLOYMENT}/${APP_NAME}.html"
        if [[ "${MODULE}" != "ALL" ]] ;
        then
            HTML_REPORT_FILE="${DEPLOYMENT}_${APP_NAME}_${MODULE}.html"
        fi
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
        sleep 2 # Give some time for the daemon to potentially start
    fi

    # Check if a `docker run` command succeeds
    while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
        # start local docker registry for sanity ci test
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

    # Always start local registry - needed for driver builds even when using amdpsdo for images
    start_registry

    # Driver registry is always local (driver builds are pushed here)
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
    echo "Run k8_jobd_ctl to:"
    echo "    (1) Load/push images to registry (hosted mode) or reference global images (amdpsdo mode)"
    echo "    (2) Configure insecure registries on cluster nodes"
    echo "    (3) Generate image-manifest-yaml for test"
    echo ""

    /gpu-operator/ci-internal/k8_jobd_ctl.py image --load-images --seed-image-manifest $SEED_IMAGE_MANIFEST --registries $PWD/registries.json --image-manifest $GEN_IMAGE_MANIFEST --testbed $TESTBED_JSON --setup-insecure-registry --target $DEPLOYMENT
    RET=$?
    if [[ "$RET" != "0" ]]
    then
        echo "FATAL ERROR: Failed to load images and configure cluster registries"
        exit $RET
    fi
    echo ""

    echo ""
    echo "Run k8_jobd_ctl to:"
    echo "    (1) Pull images on each worker node (optional pre-cache for faster pod startup)"
    echo ""
    /gpu-operator/ci-internal/k8_jobd_ctl.py image --seed-image-manifest $SEED_IMAGE_MANIFEST --registries $PWD/registries.json --testbed $TESTBED_JSON --pull-images --target $DEPLOYMENT
    RET=$?
    if [[ "$RET" != "0" ]]
    then
        echo "WARNING : Failed to pull images on each worker node(s)"
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
    if [[ "${DEPLOYMENT}" == "k8" || "${DEPLOYMENT}" == "standalone" ]];
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
            # Don't exit - GPU operator might not be installed yet
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

function launch_pytest_k8() {
    echo "Launching k8_test_launcher"
    local SECRETS="/tmp/secrets.json"
    curl -s http://pm.test.pensando.io/systest/gpu-operator-secrets/secrets.json -o ${SECRETS}
    CMD_OPTS=" --image-manifest ${GEN_IMAGE_MANIFEST} --secrets ${SECRETS} --app ${APP_NAME}"
    if [[ "${AMDGPU_DRIVER}" == "inbox" ]];
    then
        CMD_OPTS+=" --amdgpu-driver-spec lib/files/amd-inbox-driver-spec.json"
    elif [[ "${AMDGPU_DRIVER}" == "default-deviceconfig" ]];
    then
        CMD_OPTS+=" --amdgpu-driver-spec lib/files/amd-deviceconfig-default-driver-spec.json"
    else
        local DRIVER_SPEC="lib/files/amd-deviceconfig-driver-spec-${AMDGPU_DRIVER}.json"
        if [[ ! -f "${DRIVER_SPEC}" ]] ;
        then
            echo "FATAL ERROR: AMDGPU Driver spec file not found for ${AMDGPU_DRIVER} - ${DRIVER_SPEC}"
            exit 1
        fi
        CMD_OPTS+=" --amdgpu-driver-spec ${DRIVER_SPEC}"
    fi
    echo "Running k8 pytests with CMD_OPTS: ${CMD_OPTS}"
    if [[ "${APP_NAME}" == "gpu-operator" ]];
    then
        cp /gpu-operator/tools/techsupport_dump.sh /gpu-operator/tests/pytests/gpu_operator_techsupport_dump.sh
        chmod +x /gpu-operator/tests/pytests/gpu_operator_techsupport_dump.sh
        export TECH_SUPPORT_TOOL=/gpu-operator/tests/pytests/gpu_operator_techsupport_dump.sh
    fi
    if [[ "${APP_NAME}" == "exporter" ]];
    then
        cp /device-metrics-exporter/tools/techsupport_dump.sh /gpu-operator/tests/pytests/exporter_techsupport_dump.sh
        chmod +x /gpu-operator/tests/pytests/exporter_techsupport_dump.sh
        export TECH_SUPPORT_TOOL=/gpu-operator/tests/pytests/exporter_techsupport_dump.sh
    fi
    if [[ "${MODULE}" != "ALL" ]] ;
    then
        CMD_OPTS+=" --module ${MODULE}"
    fi
    /gpu-operator/tests/pytests/k8_test_launcher.sh ${CMD_OPTS}
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

function launch_pytest_openshift() {
    echo "Launching oc_test_launcher"
    local SECRETS="/tmp/secrets.json"
    curl -s http://pm.test.pensando.io/systest/gpu-operator-secrets/secrets.json -o ${SECRETS}
    CMD_OPTS=" --image-manifest ${GEN_IMAGE_MANIFEST} --secrets ${SECRETS}"
    if [[ "${AMDGPU_DRIVER}" == "inbox" ]];
    then
        CMD_OPTS+=" --amdgpu-driver-spec lib/files/amd-inbox-driver-spec.json"
    elif [[ "${AMDGPU_DRIVER}" == "default-deviceconfig" ]];
    then
        CMD_OPTS+=" --amdgpu-driver-spec lib/files/amd-deviceconfig-default-driver-spec.json"
    else
        local DRIVER_SPEC="lib/files/amd-deviceconfig-driver-spec-${AMDGPU_DRIVER}.json"
        if [[ ! -f "${DRIVER_SPEC}" ]] ;
        then
            echo "FATAL ERROR: AMDGPU Driver spec file not found for ${AMDGPU_DRIVER} - ${DRIVER_SPEC}"
            exit 1
        fi
        CMD_OPTS+=" --amdgpu-driver-spec ${DRIVER_SPEC}"
    fi
    if [[ "${MODULE}" != "ALL" ]] ;
    then
        CMD_OPTS+=" --module ${MODULE}"
    fi
    echo "Running openshift pytests with CMD_OPTS: ${CMD_OPTS}"
    TECH_SUPPORT_TOOL=/gpu-operator/tools/techsupport_dump.sh /gpu-operator/tests/pytests/oc_test_launcher.sh ${CMD_OPTS}
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

function launch_pytest_standalone() {
    echo "Launching standalone_test_launcher"
    local SECRETS="/tmp/secrets.json"
    curl -s http://pm.test.pensando.io/systest/gpu-operator-secrets/secrets.json -o ${SECRETS}
    CMD_OPTS=" --image-manifest ${GEN_IMAGE_MANIFEST} --secrets ${SECRETS} --app ${APP_NAME}"
    if [[ "${AMDGPU_DRIVER}" == "inbox" ]];
    then
        CMD_OPTS+=" --amdgpu-driver-spec lib/files/amd-inbox-driver-spec.json"
    elif [[ "${AMDGPU_DRIVER}" == "default-deviceconfig" ]];
    then
        CMD_OPTS+=" --amdgpu-driver-spec lib/files/amd-deviceconfig-default-driver-spec.json"
    else
        local DRIVER_SPEC="lib/files/amd-deviceconfig-driver-spec-${AMDGPU_DRIVER}.json"
        if [[ ! -f "${DRIVER_SPEC}" ]] ;
        then
            echo "FATAL ERROR: AMDGPU Driver spec file not found for ${AMDGPU_DRIVER} - ${DRIVER_SPEC}"
            exit 1
        fi
        CMD_OPTS+=" --amdgpu-driver-spec ${DRIVER_SPEC}"
    fi
    if [[ "${MODULE}" != "ALL" ]] ;
    then
        CMD_OPTS+=" --module ${MODULE}"
    fi
    CMD_OPTS+=" --testbed /gpu-operator/tests/pytests/testbed.json"
    echo "Running standalone pytests with CMD_OPTS: ${CMD_OPTS}"
    /gpu-operator/tests/pytests/standalone_test_launcher.sh ${CMD_OPTS}
    RET=$?
    echo ""
    /gpu-operator/ci-internal/k8_jobd_ctl.py report --show --testbed $TESTBED_JSON --target $DEPLOYMENT
    RPT=$?
    echo ""
    upload_reports
    collect_logs
    if [[ "$RET" != "0" ]]
    then
        exit $RET
    fi
    exit $RPT
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
        --module)
            MODULE="$2"
            shift
        ;;
        --registry)
            REGISTRY_SELECTION="$2"
            shift
        ;;
        --testbed)
            TESTBED_JSON="$2"
            shift
        ;;
        --amdgpu-driver)
            AMDGPU_DRIVER="$2"
            shift
        ;;
        --k8-version)
            K8_VERSION="$2"
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

function start_pod_monitor() {
    echo ""
    echo "========================================================================"
    echo "Starting Pod Monitor"
    echo "========================================================================"

    # Configure pod monitor
    export POD_MONITOR_NAMESPACES="${POD_MONITOR_NAMESPACES:-kube-amd-gpu,kube-amd-exporter,default,openshift-amd-gpu}"
    export POD_MONITOR_LOG="${POD_MONITOR_LOG:-/gpu-operator/tests/pytests/logs/sanity-pod-monitor-$(date +%Y%m%d_%H%M%S).log}"
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

function main() {
    prepare_cluster
    setup_registry
    load_images
    echo "Completed setting up environment for launching pytest"

    # Start pod monitoring before running tests
    start_pod_monitor

    if [[ "${DEPLOYMENT}" == "k8" ]];
    then
        launch_pytest_k8
    elif [[ "${DEPLOYMENT}" == "openshift" ]];
    then
        launch_pytest_openshift
    elif [[ "${DEPLOYMENT}" == "standalone" ]];
    then
        launch_pytest_standalone
    else
        echo "Invalid selection for DEPLOYMENT=${DEPLOYMENT}"
        exit 1
    fi
}

main
