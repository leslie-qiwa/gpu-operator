FROM registry.access.redhat.com/ubi9/ubi-minimal:9.7

ARG version=dev
ARG release=dev

LABEL name="amd-gpu-operator-remediation-config-utils"
LABEL maintainer="udayb@amd.com"
LABEL vendor="Advanced Micro Devices, Inc."
LABEL version="${version}"
LABEL release="${release}"
LABEL summary="AMD GPU Operator Remediation ConfigMap Image"
LABEL description="This image packages the default remediation ConfigMap used by the AMD GPU Operator's auto node remediation feature. When referenced via spec.remediationWorkflow.configMapImage in a DeviceConfig, the operator runs a Job from this image to create the condition-to-workflow mapping ConfigMap in the cluster. The ConfigMap defines which remediation workflows to trigger for specific GPU node conditions."

RUN microdnf install -y tar gzip && \
    microdnf clean all

RUN mkdir -p /oc && cd /oc && \
    curl -SsLO 'https://mirror.openshift.com/pub/openshift-v4/clients/ocp/latest/openshift-client-linux-amd64-rhel9.tar.gz' && \
    tar -xzf openshift-client-linux-amd64-rhel9.tar.gz -C /oc && \
    cp ./kubectl /usr/local/bin && chmod +x /usr/local/bin/kubectl && \
    cp ./oc /usr/local/bin && chmod +x /usr/local/bin/oc && \
    rm -rf /oc

RUN mkdir -p /remediation
COPY configmap.yaml /remediation/configmap.yaml

ENTRYPOINT ["/bin/sh"]
