#!/bin/bash

# podman is used by openshift/RHEL instead of docker, this checks which one is available and uses that container engine.
if command -v podman >/dev/null 2>&1; then
  ENGINE=podman
elif command -v docker >/dev/null 2>&1; then
  if docker --version 2>/dev/null | grep -qi 'podman'; then
    ENGINE=podman
  else
    ENGINE=docker
  fi
else
  echo "No container engine found" >&2
  exit 1
fi

echo "Using $ENGINE"

echo "List builds for each sw-artifacts from amdpsdo"

RELEASE="na"
ARTIFACT="all"

declare -a build_artifacts=(
    "gpu-operator"
    "device-metrics-exporter"
    "test-runner"
    "device-config-manager"
    "kernel-module-management-signimage"
    "kernel-module-management-worker"
    "kernel-module-management-operator"
    "kernel-module-management-webhook-server"
    "gpu-operator-bundle"
)

function usage() {
    echo ""
    echo "Usage: $0 [options]"
    echo "    --help print help/usage information"
    echo "    --release release/prefix to match for"
    echo "    --artifact [all|<name>]"
    echo ""

    echo "Artifacts:"
    for artifact in "${build_artifacts[@]}" ;
    do
        echo " * ${artifact}"
    done
    echo ""
}

function retrieve_all_posted_image_tags() {
    echo ""
    for artifact in "${build_artifacts[@]}" ;
    do
        echo "${artifact}:"
        $ENGINE run --rm -it quay.io/skopeo/stable list-tags --creds=amdpsdo:dckr_oat_YirfnS7e0IMqU1vv-jMTn8rdBnQZeO5K docker://amdpsdo/$artifact | grep $RELEASE | sort 
        echo ""
    done
}

function retrieve_given_posted_image_tags() {
    artifact=$ARTIFACT
    echo ""
    echo "${artifact}:"
    $ENGINE run --rm -it quay.io/skopeo/stable list-tags --creds=amdpsdo:dckr_oat_YirfnS7e0IMqU1vv-jMTn8rdBnQZeO5K docker://amdpsdo/$artifact | grep $RELEASE | sort 
    echo ""
}

while [[ $# -gt 0 ]] ;
do
    case $1 in
        --release)
            RELEASE="$2"
            shift
        ;;
        --artifact)
            ARTIFACT="$2"
            shift
        ;;
        --help)
            usage
            exit 0
        ;;
        --*)
            echo "Unknown option $1"
            usage
            exit 1
        ;;
    esac
    shift
done

if [[ "$RELEASE" == "na" ]] ;
then
    usage
    exit 1
fi

if [[ "$ARTIFACT" == "all" ]] ;
then
    retrieve_all_posted_image_tags
else
    retrieve_given_posted_image_tags
fi

