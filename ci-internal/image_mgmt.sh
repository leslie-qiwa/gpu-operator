#!/bin/bash

#set -x

function usage() {
    echo ""
    echo "Usage: $0 [options]"
    echo "          --help print help/usage information"
    echo "          --bundle-images"
    echo "          --load-images"
    echo "          --registry <selection: local|master|global>"
    echo "          --skip-registry-setup"
    echo ""
    echo ""
    echo "Utility to setup registry with GPU-Operator/Operand images in a registry"
    echo ""
    echo "Step-1: Download and bundle images - helm-chart(s) and container images"
    echo ""
    echo "        Command: ./ci-internal/image_mgmt.sh --bundle-images"
    echo ""
    echo "        This will create gpuop-images-<BRANCH>.tgz containing all images and utility scripts."
    echo "        Currently, branch is assumed to be main"
    echo "        Once the archive is ready, transfer to remote cluster/regsistry server"
    echo ""
    echo "Step-2: Extract the gpuop-images-<BRANCH>.tgz and setup registry & images"
    echo ""
    echo "        If a registry is already setup and configured (insecure etc)"
    echo "        Command: ./sandbox/image_mgmt.sh --load-images --registry local --skip-registry-setup"
    echo ""
    echo "        If a registry is not setup yet"
    echo "        Command: ./sandbox/image_mgmt.sh --load-images --registry local"
    echo ""
    echo "        This will load container images, tag and push to specified registry"
    echo "        It will generate images.yaml file which can be further used with test-automation"
    echo ""
}

DEPLOYMENT="k8"
BRANCH="main"
LOCAL_REGISTRY_PORT="5000"
REGISTRY_SELECTION="local"
GEN_IMAGE_MANIFEST="$PWD/images.yaml"
GLOBAL_REGISTRY="registry.test.pensando.io:5000"
SEED_IMAGE_MANIFEST="$PWD/sandbox/sandbox-sanity-images.yml"
REGISTRY=""
SETUP_REGISTRY="YES"
TESTBED_JSON=/tmp/testbed.json

# Tasks
BUNDLE_IMAGES="NO"
LOAD_IMAGES="NO"

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
        docker run -d -p $LOCAL_REGISTRY_PORT:$LOCAL_REGISTRY_PORT --name registry --restart always registry:2
        if [ $? -eq 0 ]; then
            echo "Registry is ready, mark it insecure for pushing images"
            DOCKER_CONFIG_FILE="/etc/docker/daemon.json"
            sudo jq --arg host_ip "$HOST_IP" --arg reg_port "$LOCAL_REGISTRY_PORT"   '.["insecure-registries"] += ["\($host_ip):\($reg_port)"]' "$DOCKER_CONFIG_FILE" > /tmp/daemon.json.tmp && sudo mv /tmp/daemon.json.tmp "$DOCKER_CONFIG_FILE"
            sudo systemctl restart docker
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
    if [[ ${REGISTRY_SELECTION} == "local" ]] ;
    then
        if [[ ${SETUP_REGISTRY} == "YES" ]] ;
	then
            echo "Setting up local registry"
            start_registry
	fi
        REGISTRY="${HOST_IP}:${LOCAL_REGISTRY_PORT}"
    elif [[ ${REGISTRY_SELECTION} == "master" ]];
    then
        echo "Extract master node details, setup registry : TODO"
        exit 1
    elif [[ ${REGISTRY_SELECTION} == "global" ]];
    then
        REGISTRY=${GLOBAL_REGISTRY}
    else
        echo "FATAL ERROR: Invalid registry-selection - ABORT"
        exit 1
    fi
    echo ""
}

function load_images() {
    echo ""
    echo "Run k8_jobd_ctl to "
    echo "    (1) load images into registry : ${REGISTRY}"
    echo "    (2) generate image-manifest-yaml for test"
    echo ""

    python3 -m venv venv
    source venv/bin/activate
    pip install docker
    pip install ruamel.yaml

    python3 sandbox/k8_jobd_ctl.py image --load-images --seed-image-manifest $SEED_IMAGE_MANIFEST --registry $REGISTRY --image-manifest $GEN_IMAGE_MANIFEST --testbed $TESTBED_JSON --target $DEPLOYMENT
    RET=$?
    if [[ "$RET" != "0" ]]
    then
        echo "FATAL ERROR: Failed load images and generate image-manfiest-yaml "
        exit $RET
    fi
    echo ""
}

function bundle_images() {
    echo "Downloading all artifacts of gpu-operator to local folder : sandbox"
    rm -rf $PWD/sandbox && mkdir -p $PWD/sandbox
    CI_USE_SANITY_BUILD=1 $PWD/ci-internal/flexibuilder/build.sh build-external-device-metrics-exporter-helm-artifact \
        --alien-repo pensando/device-metrics-exporter --alien-target build-helm-artifact --alien-branch ${BRANCH}
    CI_USE_SANITY_BUILD=1 $PWD/ci-internal/flexibuilder/build.sh build-external-device-metrics-exporter \
        --alien-repo pensando/device-metrics-exporter --alien-target build-device-metrics-exporter-docker-ubi9.6 --alien-branch ${BRANCH}
    CI_USE_SANITY_BUILD=1 $PWD/ci-internal/flexibuilder/build.sh build-external-test-runner \
        --alien-repo pensando/device-metrics-exporter --alien-target build-test-runner-docker --alien-branch ${BRANCH}
    CI_USE_SANITY_BUILD=1 $PWD/ci-internal/flexibuilder/build.sh build-external-test-runner-agfhc \
       --alien-repo pensando/device-metrics-exporter --alien-target build-test-runner-docker-agfhc --alien-branch ${BRANCH}
    CI_USE_SANITY_BUILD=1 $PWD/ci-internal/flexibuilder/build.sh build-external-config-manager \
       --alien-repo pensando/device-config-manager --alien-target build-device-config-manager-docker-ubi9 --alien-branch ${BRANCH}
    CI_USE_SANITY_BUILD=1 $PWD/ci-internal/flexibuilder/build.sh build-external-kernel-module-manager \
       --alien-repo pensando/kernel-module-management --alien-target build-kernel-module-management --alien-branch ${BRANCH}
    CI_USE_SANITY_BUILD=1 $PWD/ci-internal/flexibuilder/build.sh build-external-kernel-module-signimage \
       --alien-repo pensando/kernel-module-management --alien-target build-signer --alien-branch ${BRANCH}
    CI_USE_SANITY_BUILD=1 $PWD/ci-internal/flexibuilder/build.sh build-external-kernel-module-webhook-server \
       --alien-repo pensando/kernel-module-management --alien-target build-webhook-server --alien-branch ${BRANCH}
    CI_USE_SANITY_BUILD=1 $PWD/ci-internal/flexibuilder/build.sh build-external-kernel-module-worker \
       --alien-repo pensando/kernel-module-management --alien-target build-worker --alien-branch ${BRANCH}
    CI_USE_SANITY_BUILD=1 $PWD/ci-internal/flexibuilder/build.sh build-gpu-operator \
       --alien-repo pensando/gpu-operator --alien-target build-gpu-operator --alien-branch ${BRANCH}
    CI_USE_SANITY_BUILD=1 $PWD/ci-internal/flexibuilder/build.sh build-gpu-operator-k8s \
       --alien-repo pensando/gpu-operator --alien-target build-gpu-operator-k8s --alien-branch ${BRANCH}
    cp $PWD/ci-internal/sandbox-sanity-images.yml $PWD/sandbox
    cp $PWD/ci-internal/k8_jobd_ctl.py $PWD/sandbox
    cp $PWD/ci-internal/image_mgmt.sh $PWD/sandbox
    echo ""
    echo "Collected all the image artifacts, creating archive"
    echo ""
    tar -zcf gpuop-images-${BRANCH}.tgz sandbox
    rm -rf sandbox
    echo "Archive is ready to use"
    ls -ltrh gpuop-images-${BRANCH}.tgz
    echo ""
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --registry)
            REGISTRY_SELECTION="$2"
            shift
        ;;
        --bundle-images)
            BUNDLE_IMAGES="YES"
        ;;
        --load-images)
            LOAD_IMAGES="YES"
        ;;
        --help)
            usage
            exit 0
        ;;
	--skip-registry-setup)
	    SETUP_REGISTRY="NO"
        ;;
        --*)
            echo "Unknown option $1"
            exit 1
        ;;
    esac
    shift
done

function main() {
    if [[ "${BUNDLE_IMAGES}" == "YES" ]] ;
    then
        bundle_images
    elif [[ "${LOAD_IMAGES}" == "YES" ]] ;
    then
        if [ -n "$HOST_IP" ]; then
            echo "HOST_IP is defined."
        else
            echo "HOST_IP is not valid. Set HOST_IP=<IP> of the registry"
            exit 1
        fi
        setup_registry
        load_images
    else
        usage
    fi
}

main
