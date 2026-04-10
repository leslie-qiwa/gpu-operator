#!/bin/bash

if [ -z "${RELEASE:-}" ]
then
  echo "RELEASE is not set, return"
  exit 0
fi

echo "Copying gpu-operator artifacts and pushing docker image with tag: $RELEASE"

setup_dir () {
    ls -al /gpu-operator/
    BUNDLE_DIR=/gpu-operator/output/
    mkdir -p $BUNDLE_DIR
}

copy_artifacts () {
    # copy gpu-operator container image
    cp /gpu-operator/gpu-operator.tar.gz $BUNDLE_DIR/gpu-operator-$RELEASE.tar.gz
    # copy gpu-operator utils container image
    cp /gpu-operator/gpu-operator-utils.tar.gz $BUNDLE_DIR/gpu-operator-utils-$RELEASE.tar.gz
    # copy k8s helm package
    cp /gpu-operator/gpu-operator-helm-k8s.tgz $BUNDLE_DIR/gpu-operator-helm-k8s-$RELEASE.tgz
    # copy gpu operator OLM bundle package
    cp /gpu-operator/gpu-operator-olm-bundle.tar.gz $BUNDLE_DIR/gpu-operator-olm-bundle-$RELEASE.tar.gz
    # copy remediation configmap util container image
    cp /gpu-operator/gpu-operator-remediation-configmap-util.tar.gz $BUNDLE_DIR/gpu-operator-remediation-configmap-util-$RELEASE.tar.gz
    # list the artifacts copied out
    ls -la $BUNDLE_DIR
}

docker_push () {
    if [ -z "${DOCKERHUB_TOKEN:-}" ]
    then
      echo "DOCKERHUB_TOKEN is not set, skipping docker push"
      return
    fi
    docker login --username=shreyajmeraamd --password-stdin <<< "${DOCKERHUB_TOKEN}"
    # push operator controller image
    docker load -i /gpu-operator/gpu-operator.tar.gz
    docker push docker.io/amdpsdo/gpu-operator:$RELEASE
    # push utils image
    docker load -i /gpu-operator/gpu-operator-utils.tar.gz
    docker push docker.io/amdpsdo/gpu-operator-utils:$RELEASE
    # push OLM bundle image
    docker load -i /gpu-operator/gpu-operator-olm-bundle.tar.gz
    docker push docker.io/amdpsdo/gpu-operator-bundle:$RELEASE
    # push remediation configmap util image
    docker load -i /gpu-operator/gpu-operator-remediation-configmap-util.tar.gz
    docker push docker.io/amdpsdo/gpu-operator-remediation-configmap-util:$RELEASE
}

helm_push () {
    if [ -z "${DOCKERHUB_TOKEN:-}" ]
    then
      echo "DOCKERHUB_TOKEN is not set, skipping helm push"
      return
    fi
    helm registry login docker.io --username=shreyajmeraamd --password-stdin <<< "${DOCKERHUB_TOKEN}"
    # Push amdpsdo k8s helm chart
    helm push /gpu-operator/gpu-operator-helm-k8s.tgz oci://docker.io/amdpsdo
}

setup () {
    setup_dir
    copy_artifacts
}

upload () {
    cd $BUNDLE_DIR
    find . -type f -print0 | while IFS= read -r -d $'\0' file;
      do asset-push builds hourly-gpu-operator $RELEASE "$file" ;
      if [ $? -ne 0 ]; then
        exit 1
      fi
    done
}

main () {
  setup
  upload

  # docker push need happen after asset-push in case docker is not fully started yet
  docker_push
  helm_push
}

main

exit 0
