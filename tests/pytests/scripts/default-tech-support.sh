#!/bin/bash

# Default Tech-Support Log Collection Script
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# This script collects diagnostic information when component-specific
# tech-support tools are not available. It auto-detects deployed
# components and collects relevant logs and resource states.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_DIR="${PWD}/techsupport-${TIMESTAMP}"
KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

# Create output directory
mkdir -p "${OUTPUT_DIR}"
log_info "Collecting diagnostics to: ${OUTPUT_DIR}"

# =============================================================================
# Function: Check if namespace exists
# =============================================================================
namespace_exists() {
    kubectl get namespace "$1" &>/dev/null
}

# =============================================================================
# Function: Collect pod logs from namespace
# =============================================================================
collect_namespace_logs() {
    local namespace=$1
    local output_subdir="${OUTPUT_DIR}/${namespace}"

    if ! namespace_exists "${namespace}"; then
        log_warn "Namespace ${namespace} does not exist, skipping"
        return
    fi

    log_info "Collecting logs from namespace: ${namespace}"
    mkdir -p "${output_subdir}"

    # Get all pods
    kubectl get pods -n "${namespace}" -o json > "${output_subdir}/pods.json" 2>/dev/null || true
    kubectl get pods -n "${namespace}" -o wide > "${output_subdir}/pods.txt" 2>/dev/null || true

    # Collect logs from each pod
    while IFS= read -r pod_name; do
        log_info "  Collecting logs for pod: ${pod_name}"

        # Get pod describe
        kubectl describe pod "${pod_name}" -n "${namespace}" > "${output_subdir}/${pod_name}_describe.txt" 2>/dev/null || true

        # Get pod YAML
        kubectl get pod "${pod_name}" -n "${namespace}" -o yaml > "${output_subdir}/${pod_name}.yaml" 2>/dev/null || true

        # Get logs for each container
        containers=$(kubectl get pod "${pod_name}" -n "${namespace}" -o jsonpath='{.spec.containers[*].name}' 2>/dev/null || echo "")
        for container in ${containers}; do
            log_info "    Container: ${container}"
            # Current logs
            kubectl logs "${pod_name}" -n "${namespace}" -c "${container}" --tail=5000 > "${output_subdir}/${pod_name}_${container}.log" 2>/dev/null || true

            # Previous logs (if pod restarted)
            kubectl logs "${pod_name}" -n "${namespace}" -c "${container}" --previous --tail=5000 > "${output_subdir}/${pod_name}_${container}_previous.log" 2>/dev/null || log_warn "    No previous logs for ${container}"
        done
    done < <(kubectl get pods -n "${namespace}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | tr ' ' '\n' || true)

    # Get events
    kubectl get events -n "${namespace}" --sort-by='.lastTimestamp' > "${output_subdir}/events.txt" 2>/dev/null || true
}

# =============================================================================
# Function: Collect custom resources
# =============================================================================
collect_custom_resources() {
    local api_group=$1
    local resource_type=$2
    local output_file=$3

    log_info "Collecting ${resource_type} (${api_group})"

    # Try to get the resources
    kubectl get "${resource_type}.${api_group}" --all-namespaces -o yaml > "${output_file}" 2>/dev/null || {
        log_warn "  Resource ${resource_type}.${api_group} not available"
        return 1
    }
}

# =============================================================================
# Auto-detect deployed components and collect logs
# =============================================================================

# GPU Operator namespace
GPU_OPERATOR_NS="${GPU_OPERATOR_NAMESPACE:-kube-amd-gpu}"
if namespace_exists "${GPU_OPERATOR_NS}"; then
    log_info "Detected GPU Operator deployment"
    collect_namespace_logs "${GPU_OPERATOR_NS}"

    # Collect DeviceConfig CRs
    collect_custom_resources "amd.com" "deviceconfigs" "${OUTPUT_DIR}/deviceconfigs.yaml"

    # Collect AmdGpu CRs (if using v1.0.0)
    collect_custom_resources "amd.com" "amdgpus" "${OUTPUT_DIR}/amdgpus.yaml"
fi

# DRA Driver namespace
DRA_DRIVER_NS="${DRA_DRIVER_NAMESPACE:-kube-amd-gpu-dra}"
if namespace_exists "${DRA_DRIVER_NS}"; then
    log_info "Detected DRA Driver deployment"
    collect_namespace_logs "${DRA_DRIVER_NS}"

    # Collect DRA resources
    log_info "Collecting DRA resources"
    mkdir -p "${OUTPUT_DIR}/dra-resources"

    # Try v1 first (K8s 1.34+), then v1beta2 (OpenShift 4.20, K8s 1.33), then v1beta1 (K8s 1.32)
    for version in v1 v1beta2 v1beta1; do
        if kubectl api-resources --api-group=resource.k8s.io | grep -q "resource.k8s.io/${version}"; then
            log_info "  Using DRA API version: ${version}"
            kubectl get resourceslices.resource.k8s.io -o yaml > "${OUTPUT_DIR}/dra-resources/resourceslices_${version}.yaml" 2>/dev/null || true
            kubectl get deviceclasses.resource.k8s.io -o yaml > "${OUTPUT_DIR}/dra-resources/deviceclasses_${version}.yaml" 2>/dev/null || true
            kubectl get resourceclaims.resource.k8s.io --all-namespaces -o yaml > "${OUTPUT_DIR}/dra-resources/resourceclaims_${version}.yaml" 2>/dev/null || true
            break
        fi
    done
fi

# Metrics Exporter namespace
EXPORTER_NS="${EXPORTER_NAMESPACE:-kube-amd-exporter}"
if namespace_exists "${EXPORTER_NS}"; then
    log_info "Detected Metrics Exporter deployment"
    collect_namespace_logs "${EXPORTER_NS}"
fi

# NFD namespace (Node Feature Discovery)
NFD_NS="node-feature-discovery"
if namespace_exists "${NFD_NS}"; then
    log_info "Detected NFD deployment"
    collect_namespace_logs "${NFD_NS}"
fi

# Cert-Manager namespace
CERT_MANAGER_NS="cert-manager"
if namespace_exists "${CERT_MANAGER_NS}"; then
    log_info "Collecting cert-manager logs"
    collect_namespace_logs "${CERT_MANAGER_NS}"
fi

# =============================================================================
# Collect cluster-wide information
# =============================================================================
log_info "Collecting cluster-wide information"
mkdir -p "${OUTPUT_DIR}/cluster-info"

# Nodes
kubectl get nodes -o wide > "${OUTPUT_DIR}/cluster-info/nodes.txt" 2>/dev/null || true
kubectl get nodes -o yaml > "${OUTPUT_DIR}/cluster-info/nodes.yaml" 2>/dev/null || true

# GPU nodes (with NFD label)
kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true -o wide > "${OUTPUT_DIR}/cluster-info/gpu-nodes.txt" 2>/dev/null || true

# API resources
kubectl api-resources > "${OUTPUT_DIR}/cluster-info/api-resources.txt" 2>/dev/null || true

# API versions
kubectl api-versions > "${OUTPUT_DIR}/cluster-info/api-versions.txt" 2>/dev/null || true

# Cluster info
kubectl cluster-info dump --output-directory="${OUTPUT_DIR}/cluster-info/dump" --namespaces="" 2>/dev/null || log_warn "cluster-info dump failed"

# Version info
kubectl version > "${OUTPUT_DIR}/cluster-info/version.txt" 2>/dev/null || true

# =============================================================================
# Collect Docker-based metrics exporter (if deployed)
# =============================================================================

# Helper function to collect Docker info from a node
collect_docker_from_node() {
    local node_name=$1
    local output_subdir="${OUTPUT_DIR}/docker-exporter/${node_name}"

    log_info "  Checking Docker on node: ${node_name}"
    mkdir -p "${output_subdir}"

    # Use kubectl debug to run on the node
    # Note: Commands run in chroot /host to access host filesystem
    local docker_check=$(kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
        chroot /host bash -c "docker ps -a --filter 'name=amd-metrics-exporter' --format '{{.Names}}' 2>/dev/null || echo ''")

    if [ -n "${docker_check}" ] && [ "${docker_check}" != "error"* ]; then
        log_info "    Found Docker containers on ${node_name}"

        # Container list
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host docker ps -a --filter "name=amd-metrics-exporter" --format "table {{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Image}}" \
            > "${output_subdir}/containers.txt" 2>/dev/null || true

        # For each container, collect detailed info
        for container in ${docker_check}; do
            log_info "      Container: ${container}"

            # Container inspect
            kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
                chroot /host docker inspect "${container}" \
                > "${output_subdir}/${container}_inspect.json" 2>/dev/null || true

            # Container logs
            kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
                chroot /host docker logs "${container}" --tail 5000 \
                > "${output_subdir}/${container}.log" 2>/dev/null || true

            # Container stats
            kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
                chroot /host docker stats --no-stream "${container}" \
                > "${output_subdir}/${container}_stats.txt" 2>/dev/null || true
        done

        # Check for amdgpuhealth on host
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host bash -c "[ -f /var/lib/amd-metrics-exporter/amdgpuhealth ] && /var/lib/amd-metrics-exporter/amdgpuhealth" \
            > "${output_subdir}/amdgpuhealth_output.txt" 2>/dev/null || log_warn "      No amdgpuhealth on ${node_name}"

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host ls -lh /var/lib/amd-metrics-exporter/ \
            > "${output_subdir}/amdgpuhealth_dir.txt" 2>/dev/null || true
    fi
}

# Check for Docker-based deployments
log_info "Checking for Docker-based metrics exporter deployments on GPU nodes"

# Get GPU nodes
gpu_nodes=$(kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")

if [ -n "${gpu_nodes}" ]; then
    for node in ${gpu_nodes}; do
        collect_docker_from_node "${node}"
    done
elif command -v docker &>/dev/null; then
    # Fallback: check locally if we're running on a GPU node itself
    log_info "Checking for Docker-based metrics exporter deployments (local)"

    # Find containers with amd-metrics-exporter in the name
    docker_containers=$(docker ps -a --filter "name=amd-metrics-exporter" --format "{{.Names}}" 2>/dev/null || echo "")

    if [ -n "${docker_containers}" ]; then
        log_info "Found Docker-based metrics exporter deployment(s)"
        mkdir -p "${OUTPUT_DIR}/docker-exporter"

        # List all exporter containers
        docker ps -a --filter "name=amd-metrics-exporter" --format "table {{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Image}}" > "${OUTPUT_DIR}/docker-exporter/containers.txt" 2>/dev/null || true

        while IFS= read -r container; do
            log_info "  Collecting logs for Docker container: ${container}"

            # Container inspect
            docker inspect "${container}" > "${OUTPUT_DIR}/docker-exporter/${container}_inspect.json" 2>/dev/null || true

            # Container logs
            docker logs "${container}" --tail 5000 > "${OUTPUT_DIR}/docker-exporter/${container}.log" 2>/dev/null || true

            # Container stats (non-streaming snapshot)
            docker stats --no-stream "${container}" > "${OUTPUT_DIR}/docker-exporter/${container}_stats.txt" 2>/dev/null || true
        done <<< "${docker_containers}"

        # Check for amdgpuhealth on host (Docker exporter may write there)
        if [ -f /var/lib/amd-metrics-exporter/amdgpuhealth ]; then
            log_info "  Found amdgpuhealth binary on host"
            /var/lib/amd-metrics-exporter/amdgpuhealth > "${OUTPUT_DIR}/docker-exporter/amdgpuhealth_output.txt" 2>/dev/null || log_warn "  Failed to run amdgpuhealth"
            ls -lh /var/lib/amd-metrics-exporter/ > "${OUTPUT_DIR}/docker-exporter/amdgpuhealth_dir.txt" 2>/dev/null || true
        fi
    fi
fi

# =============================================================================
# Collect Debian package-based metrics exporter (if installed)
# =============================================================================

# Helper function to collect Debian package info from a node
collect_debian_from_node() {
    local node_name=$1
    local output_subdir="${OUTPUT_DIR}/debian-exporter/${node_name}"

    log_info "  Checking Debian package on node: ${node_name}"
    mkdir -p "${output_subdir}"

    # Check if package is installed on the node
    local pkg_check=$(kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
        chroot /host bash -c "dpkg -l 2>/dev/null | grep amd-metrics-exporter || echo ''")

    if [ -n "${pkg_check}" ]; then
        log_info "    Found Debian package on ${node_name}"

        # Package information
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host bash -c "dpkg -l | grep amd-metrics-exporter" \
            > "${output_subdir}/package_info.txt" 2>/dev/null || true

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host dpkg -s amd-metrics-exporter \
            > "${output_subdir}/package_status.txt" 2>/dev/null || true

        # Installed files
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host dpkg -L amd-metrics-exporter \
            > "${output_subdir}/installed_files.txt" 2>/dev/null || true

        # Systemd service status
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host systemctl status amd-metrics-exporter \
            > "${output_subdir}/service_status.txt" 2>/dev/null || true

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host systemctl cat amd-metrics-exporter \
            > "${output_subdir}/service_unit.txt" 2>/dev/null || true

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host journalctl -u amd-metrics-exporter --no-pager -n 5000 \
            > "${output_subdir}/service_logs.txt" 2>/dev/null || true

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host systemctl show amd-metrics-exporter \
            > "${output_subdir}/service_properties.txt" 2>/dev/null || true

        # Configuration files
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host bash -c "[ -d /etc/amd-metrics-exporter ] && tar -czf - -C / etc/amd-metrics-exporter" \
            > "${output_subdir}/config.tar.gz" 2>/dev/null || true

        # Runtime data
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host ls -lhR /var/lib/amd-metrics-exporter \
            > "${output_subdir}/runtime_dir.txt" 2>/dev/null || true

        # Run amdgpuhealth
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host bash -c "[ -x /var/lib/amd-metrics-exporter/amdgpuhealth ] && /var/lib/amd-metrics-exporter/amdgpuhealth" \
            > "${output_subdir}/amdgpuhealth_output.txt" 2>/dev/null || log_warn "      No amdgpuhealth on ${node_name}"

        # Log files
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host bash -c "[ -d /var/log/amd-metrics-exporter ] && tar -czf - -C / var/log/amd-metrics-exporter" \
            > "${output_subdir}/logs.tar.gz" 2>/dev/null || true
    fi
}

# Helper function to collect RPM package info from a node
collect_rpm_from_node() {
    local node_name=$1
    local output_subdir="${OUTPUT_DIR}/rpm-exporter/${node_name}"

    log_info "  Checking RPM package on node: ${node_name}"
    mkdir -p "${output_subdir}"

    # Check if package is installed on the node
    local pkg_check=$(kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
        chroot /host bash -c "rpm -qa 2>/dev/null | grep amd-metrics-exporter || echo ''")

    if [ -n "${pkg_check}" ]; then
        log_info "    Found RPM package on ${node_name}"

        # Package information
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host bash -c "rpm -qa | grep amd-metrics-exporter" \
            > "${output_subdir}/package_info.txt" 2>/dev/null || true

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host rpm -qi amd-metrics-exporter \
            > "${output_subdir}/package_details.txt" 2>/dev/null || true

        # Installed files
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host rpm -ql amd-metrics-exporter \
            > "${output_subdir}/installed_files.txt" 2>/dev/null || true

        # Systemd service (same as Debian)
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host systemctl status amd-metrics-exporter \
            > "${output_subdir}/service_status.txt" 2>/dev/null || true

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host systemctl cat amd-metrics-exporter \
            > "${output_subdir}/service_unit.txt" 2>/dev/null || true

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host journalctl -u amd-metrics-exporter --no-pager -n 5000 \
            > "${output_subdir}/service_logs.txt" 2>/dev/null || true

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host systemctl show amd-metrics-exporter \
            > "${output_subdir}/service_properties.txt" 2>/dev/null || true

        # Configuration and runtime data (same locations as Debian)
        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host bash -c "[ -d /etc/amd-metrics-exporter ] && tar -czf - -C / etc/amd-metrics-exporter" \
            > "${output_subdir}/config.tar.gz" 2>/dev/null || true

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host ls -lhR /var/lib/amd-metrics-exporter \
            > "${output_subdir}/runtime_dir.txt" 2>/dev/null || true

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host bash -c "[ -x /var/lib/amd-metrics-exporter/amdgpuhealth ] && /var/lib/amd-metrics-exporter/amdgpuhealth" \
            > "${output_subdir}/amdgpuhealth_output.txt" 2>/dev/null || log_warn "      No amdgpuhealth on ${node_name}"

        kubectl debug "node/${node_name}" --image=ubuntu --quiet -- \
            chroot /host bash -c "[ -d /var/log/amd-metrics-exporter ] && tar -czf - -C / var/log/amd-metrics-exporter" \
            > "${output_subdir}/logs.tar.gz" 2>/dev/null || true
    fi
}

# Check for package-based deployments on GPU nodes
log_info "Checking for package-based metrics exporter on GPU nodes"

# Get GPU nodes
gpu_nodes=$(kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")

if [ -n "${gpu_nodes}" ]; then
    for node in ${gpu_nodes}; do
        # Try Debian first
        collect_debian_from_node "${node}"
        # Then try RPM
        collect_rpm_from_node "${node}"
    done
elif command -v dpkg &>/dev/null; then
    # Fallback: check locally if we're running on a GPU node itself
    log_info "Checking for Debian package-based metrics exporter (local)"

    # Check if package is installed
    if dpkg -l | grep -q amd-metrics-exporter 2>/dev/null; then
        log_info "Found Debian package-based metrics exporter installation"
        mkdir -p "${OUTPUT_DIR}/debian-exporter"

        # Package information
        dpkg -l | grep amd-metrics-exporter > "${OUTPUT_DIR}/debian-exporter/package_info.txt" 2>/dev/null || true
        dpkg -s amd-metrics-exporter > "${OUTPUT_DIR}/debian-exporter/package_status.txt" 2>/dev/null || true

        # List installed files
        dpkg -L amd-metrics-exporter > "${OUTPUT_DIR}/debian-exporter/installed_files.txt" 2>/dev/null || true

        # Systemd service status (if using systemd)
        if command -v systemctl &>/dev/null; then
            log_info "  Collecting systemd service information"

            # Service status
            systemctl status amd-metrics-exporter > "${OUTPUT_DIR}/debian-exporter/service_status.txt" 2>/dev/null || true

            # Service unit file
            systemctl cat amd-metrics-exporter > "${OUTPUT_DIR}/debian-exporter/service_unit.txt" 2>/dev/null || true

            # Journal logs
            journalctl -u amd-metrics-exporter --no-pager -n 5000 > "${OUTPUT_DIR}/debian-exporter/service_logs.txt" 2>/dev/null || true

            # Service properties
            systemctl show amd-metrics-exporter > "${OUTPUT_DIR}/debian-exporter/service_properties.txt" 2>/dev/null || true
        fi

        # Configuration files (common locations)
        if [ -d /etc/amd-metrics-exporter ]; then
            log_info "  Collecting configuration files"
            cp -r /etc/amd-metrics-exporter "${OUTPUT_DIR}/debian-exporter/config" 2>/dev/null || true
        fi

        # Runtime data
        if [ -d /var/lib/amd-metrics-exporter ]; then
            log_info "  Collecting runtime data"
            ls -lhR /var/lib/amd-metrics-exporter > "${OUTPUT_DIR}/debian-exporter/runtime_dir.txt" 2>/dev/null || true

            # Run amdgpuhealth if available
            if [ -x /var/lib/amd-metrics-exporter/amdgpuhealth ]; then
                /var/lib/amd-metrics-exporter/amdgpuhealth > "${OUTPUT_DIR}/debian-exporter/amdgpuhealth_output.txt" 2>/dev/null || log_warn "  Failed to run amdgpuhealth"
            fi
        fi

        # Log files
        if [ -d /var/log/amd-metrics-exporter ]; then
            log_info "  Collecting log files"
            cp -r /var/log/amd-metrics-exporter "${OUTPUT_DIR}/debian-exporter/logs" 2>/dev/null || true
        fi
    fi
elif command -v rpm &>/dev/null; then
    log_info "Checking for RPM package-based metrics exporter"

    # Check if package is installed
    if rpm -qa | grep -q amd-metrics-exporter 2>/dev/null; then
        log_info "Found RPM package-based metrics exporter installation"
        mkdir -p "${OUTPUT_DIR}/rpm-exporter"

        # Package information
        rpm -qa | grep amd-metrics-exporter > "${OUTPUT_DIR}/rpm-exporter/package_info.txt" 2>/dev/null || true
        rpm -qi amd-metrics-exporter > "${OUTPUT_DIR}/rpm-exporter/package_details.txt" 2>/dev/null || true

        # List installed files
        rpm -ql amd-metrics-exporter > "${OUTPUT_DIR}/rpm-exporter/installed_files.txt" 2>/dev/null || true

        # Systemd service status (similar to Debian)
        if command -v systemctl &>/dev/null; then
            log_info "  Collecting systemd service information"
            systemctl status amd-metrics-exporter > "${OUTPUT_DIR}/rpm-exporter/service_status.txt" 2>/dev/null || true
            systemctl cat amd-metrics-exporter > "${OUTPUT_DIR}/rpm-exporter/service_unit.txt" 2>/dev/null || true
            journalctl -u amd-metrics-exporter --no-pager -n 5000 > "${OUTPUT_DIR}/rpm-exporter/service_logs.txt" 2>/dev/null || true
            systemctl show amd-metrics-exporter > "${OUTPUT_DIR}/rpm-exporter/service_properties.txt" 2>/dev/null || true
        fi

        # Configuration and runtime data (same locations as Debian)
        if [ -d /etc/amd-metrics-exporter ]; then
            cp -r /etc/amd-metrics-exporter "${OUTPUT_DIR}/rpm-exporter/config" 2>/dev/null || true
        fi

        if [ -d /var/lib/amd-metrics-exporter ]; then
            ls -lhR /var/lib/amd-metrics-exporter > "${OUTPUT_DIR}/rpm-exporter/runtime_dir.txt" 2>/dev/null || true
            if [ -x /var/lib/amd-metrics-exporter/amdgpuhealth ]; then
                /var/lib/amd-metrics-exporter/amdgpuhealth > "${OUTPUT_DIR}/rpm-exporter/amdgpuhealth_output.txt" 2>/dev/null || log_warn "  Failed to run amdgpuhealth"
            fi
        fi

        if [ -d /var/log/amd-metrics-exporter ]; then
            cp -r /var/log/amd-metrics-exporter "${OUTPUT_DIR}/rpm-exporter/logs" 2>/dev/null || true
        fi
    fi
fi

# =============================================================================
# Create tarball
# =============================================================================
TARBALL="techsupport-${TIMESTAMP}.tgz"
log_info "Creating tarball: ${TARBALL}"

# Check if directory exists and is not empty
if [ ! -d "techsupport-${TIMESTAMP}" ]; then
    log_error "Directory techsupport-${TIMESTAMP} does not exist"
    exit 1
fi

if [ -z "$(ls -A "techsupport-${TIMESTAMP}")" ]; then
    log_warn "Directory techsupport-${TIMESTAMP} is empty, creating tarball anyway"
fi

tar -czf "${TARBALL}" -C "${PWD}" "techsupport-${TIMESTAMP}" 2>&1 || {
    log_error "Failed to create tarball: $?"
    exit 1
}

# Cleanup temporary directory
rm -rf "${OUTPUT_DIR}"

log_info "Tech-support collection complete: ${TARBALL}"
echo "${TARBALL}"
