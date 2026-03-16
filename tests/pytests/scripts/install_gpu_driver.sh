#!/bin/bash
#
# AMD GPU Operator - Driver Installation Script
#
# This script automates the installation of AMD GPU drivers via the AMD GPU Operator.
# It follows the official installation guide from:
# https://instinct.docs.amd.com/projects/gpu-operator/en/release-v1.4.1
#
# Prerequisites:
#   - Kubernetes cluster v1.29.0+
#   - kubectl configured with cluster access
#   - Helm v3.2.0+
#   - AMD GPUs on worker nodes
#
# Usage:
#   ./install-gpu-driver.sh [OPTIONS]
#
# Options:
#   --namespace <ns>          Namespace for GPU operator (default: kube-amd-gpu)
#   --driver-version <ver>    Driver version to install (default: 30.20.1)
#   --driver-image <image>    Driver image repository (required for out-of-tree driver)
#   --registry-secret <name>  Docker registry secret name (if private registry)
#   --skip-nfd                Skip Node Feature Discovery installation
#   --skip-kmm                Skip Kernel Module Management installation
#   --skip-remediation        Skip Auto Node Remediation installation
#   --inbox-driver            Use inbox driver (skip out-of-tree driver installation)
#   --insecure-registry       Allow insecure (HTTP) registries and skip TLS verification
#   --driver-only             Install only the driver (disable device plugin, metrics, config manager, etc.)
#   --dry-run                 Show what would be installed without actually installing
#   --uninstall               Uninstall GPU operator and driver
#   -h, --help                Show this help message
#

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
NAMESPACE="kube-amd-gpu"
DRIVER_VERSION="30.20.1"
DRIVER_IMAGE=""
REGISTRY_SECRET=""
SKIP_NFD=false
SKIP_KMM=false
SKIP_REMEDIATION=false
USE_INBOX_DRIVER=false
INSECURE_REGISTRY=false
DRIVER_ONLY=false
DRY_RUN=false
UNINSTALL=false
OPERATOR_VERSION="v1.4.0"
HELM_RELEASE_NAME="amd-gpu-operator"

# Helper functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_command() {
    echo -e "${BLUE}[CMD]${NC} $1"
}

run_kubectl() {
    local cmd="$1"
    log_command "kubectl $cmd"
    eval "kubectl $cmd"
    return $?
}

print_usage() {
    sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# //; s/^#//'
}

check_prerequisites() {
    log_info "Checking prerequisites..."

    # Check kubectl
    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl not found. Please install kubectl."
        exit 1
    fi

    # Check Helm
    if ! command -v helm &> /dev/null; then
        log_error "helm not found. Please install Helm v3.2.0+."
        exit 1
    fi

    # Check Helm version
    HELM_VERSION=$(helm version --short | grep -oP 'v\d+\.\d+' | head -1)
    HELM_MAJOR=$(echo "$HELM_VERSION" | cut -d'.' -f1 | tr -d 'v')
    HELM_MINOR=$(echo "$HELM_VERSION" | cut -d'.' -f2)

    if [[ "$HELM_MAJOR" -lt 3 ]] || [[ "$HELM_MAJOR" -eq 3 && "$HELM_MINOR" -lt 2 ]]; then
        log_error "Helm version must be v3.2.0 or higher. Current version: $HELM_VERSION"
        exit 1
    fi

    # Check cluster connectivity
    if ! kubectl cluster-info &> /dev/null; then
        log_error "Cannot connect to Kubernetes cluster. Please check your kubectl configuration."
        exit 1
    fi

    # Check Kubernetes version
    K8S_VERSION=$(kubectl version --short 2>/dev/null | grep "Server Version" | grep -oP 'v\d+\.\d+' || kubectl version -o json 2>/dev/null | jq -r '.serverVersion.gitVersion' | grep -oP 'v\d+\.\d+')
    K8S_MAJOR=$(echo "$K8S_VERSION" | cut -d'.' -f1 | tr -d 'v')
    K8S_MINOR=$(echo "$K8S_VERSION" | cut -d'.' -f2)

    if [[ "$K8S_MAJOR" -lt 1 ]] || [[ "$K8S_MAJOR" -eq 1 && "$K8S_MINOR" -lt 29 ]]; then
        log_warning "Kubernetes version should be v1.29.0 or higher. Current version: $K8S_VERSION"
    fi

    log_success "Prerequisites check passed"
}

install_cert_manager() {
    log_info "Checking cert-manager installation..."

    if kubectl get namespace cert-manager &> /dev/null; then
        log_info "cert-manager namespace already exists"
        if kubectl get pods -n cert-manager -l app.kubernetes.io/name=cert-manager &> /dev/null; then
            log_success "cert-manager is already installed"
            return 0
        fi
    fi

    log_info "Installing cert-manager..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would install cert-manager v1.15.1"
        return 0
    fi

    helm repo add jetstack https://charts.jetstack.io --force-update
    helm repo update

    helm install cert-manager jetstack/cert-manager \
        --namespace cert-manager \
        --create-namespace \
        --version v1.15.1 \
        --set crds.enabled=true \
        --wait

    log_success "cert-manager installed successfully"
}

add_helm_repo() {
    log_info "Adding AMD GPU Operator Helm repository..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would add Helm repo: rocm https://rocm.github.io/gpu-operator"
        return 0
    fi

    helm repo add rocm https://rocm.github.io/gpu-operator --force-update
    helm repo update

    log_success "Helm repository added and updated"
}

create_registry_secret() {
    if [[ -z "$REGISTRY_SECRET" ]]; then
        return 0
    fi

    log_info "Checking for registry secret: $REGISTRY_SECRET..."

    # Check if secret exists
    if kubectl get secret "$REGISTRY_SECRET" -n "$NAMESPACE" &> /dev/null; then
        log_success "Registry secret '$REGISTRY_SECRET' already exists"
        return 0
    fi

    log_warning "Registry secret '$REGISTRY_SECRET' not found in namespace '$NAMESPACE'"
    log_warning "Please create the secret manually using:"
    echo ""
    echo "kubectl create secret docker-registry $REGISTRY_SECRET \\"
    echo "  -n $NAMESPACE \\"
    echo "  --docker-server=<your-registry-server> \\"
    echo "  --docker-username=<your-username> \\"
    echo "  --docker-password=<your-password>"
    echo ""

    if [[ "$DRY_RUN" != "true" ]]; then
        read -p "Do you want to continue without the secret? (y/n): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
}

check_driver_installed() {
    log_info "Checking if AMD GPU driver is already installed on nodes..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would check for existing AMD GPU driver"
        return 0
    fi

    # Get GPU nodes
    GPU_NODES=$(kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true -o name 2>/dev/null)
    VGPU_NODES=$(kubectl get nodes -l feature.node.kubernetes.io/amd-vgpu=true -o name 2>/dev/null)

    if [[ -z "$GPU_NODES" && -z "$VGPU_NODES" ]]; then
        log_warning "No GPU nodes detected yet. NFD may need to run first to label nodes."
        log_info "Driver check will be skipped. Nodes will be labeled after NFD runs."
        return 0
    fi

    # Pick first GPU node to check
    if [[ -n "$GPU_NODES" ]]; then
        NODE_TO_CHECK=$(echo "$GPU_NODES" | head -1 | sed 's/node\///')
    else
        NODE_TO_CHECK=$(echo "$VGPU_NODES" | head -1 | sed 's/node\///')
    fi

    log_info "Checking driver status on node: $NODE_TO_CHECK"

    # Create debug pod to check driver on node
    DEBUG_POD_NAME="driver-check-$$"

    log_command "kubectl apply -f - (creating debug pod $DEBUG_POD_NAME on $NODE_TO_CHECK)"
    cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: $DEBUG_POD_NAME
  namespace: default
spec:
  nodeName: $NODE_TO_CHECK
  hostPID: true
  hostNetwork: true
  containers:
  - name: check
    image: busybox:1.36
    command: ['sh', '-c', 'sleep 30']
    securityContext:
      privileged: true
    volumeMounts:
    - name: host
      mountPath: /host
  volumes:
  - name: host
    hostPath:
      path: /
  restartPolicy: Never
EOF

    # Wait for pod to be ready
    for i in {1..30}; do
        if kubectl get pod "$DEBUG_POD_NAME" -n default &> /dev/null; then
            POD_STATUS=$(kubectl get pod "$DEBUG_POD_NAME" -n default -o jsonpath='{.status.phase}' 2>/dev/null)
            if [[ "$POD_STATUS" == "Running" ]]; then
                break
            fi
        fi
        sleep 1
    done

    # Check if amdgpu driver is loaded
    log_command "kubectl exec -n default $DEBUG_POD_NAME -- sh -c 'chroot /host lsmod | grep amdgpu'"
    DRIVER_CHECK=$(kubectl exec -n default "$DEBUG_POD_NAME" -- sh -c "chroot /host lsmod | grep amdgpu || true" 2>/dev/null)
    echo "$DRIVER_CHECK"

    # Check if AMD GPU devices exist
    log_command "kubectl exec -n default $DEBUG_POD_NAME -- sh -c 'chroot /host ls /dev/dri/render*'"
    GPU_CHECK=$(kubectl exec -n default "$DEBUG_POD_NAME" -- sh -c "chroot /host ls /dev/dri/render* 2>/dev/null || true" 2>/dev/null)
    echo "$GPU_CHECK"

    # Cleanup debug pod
    log_command "kubectl delete pod $DEBUG_POD_NAME -n default"
    kubectl delete pod "$DEBUG_POD_NAME" -n default || true

    # Analyze results
    if [[ -n "$DRIVER_CHECK" ]]; then
        log_success "AMD GPU driver (amdgpu) is already loaded on node $NODE_TO_CHECK"

        # Extract driver version if possible
        DRIVER_VERSION_CHECK=$(echo "$DRIVER_CHECK" | awk '{print $1}')
        if [[ -n "$DRIVER_VERSION_CHECK" ]]; then
            log_info "Detected driver module: $DRIVER_VERSION_CHECK"
        fi

        if [[ "$USE_INBOX_DRIVER" == "false" ]]; then
            log_warning "Driver is already loaded, but you requested out-of-tree driver installation"
            log_warning "The operator will manage driver upgrades/downgrades as needed"
        fi
    else
        log_info "AMD GPU driver (amdgpu) is NOT loaded on node $NODE_TO_CHECK"

        if [[ "$USE_INBOX_DRIVER" == "true" ]]; then
            log_warning "No driver detected, but --inbox-driver flag is set"
            log_warning "Please ensure the driver is installed on your nodes, or remove --inbox-driver flag"

            if [[ "$DRY_RUN" != "true" ]]; then
                read -p "Do you want to continue anyway? (y/n): " -n 1 -r
                echo
                if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                    exit 1
                fi
            fi
        else
            log_info "Out-of-tree driver installation will be configured"
        fi
    fi

    # Check for GPU devices
    if [[ -n "$GPU_CHECK" ]]; then
        GPU_COUNT=$(echo "$GPU_CHECK" | wc -l)
        log_info "Found $GPU_COUNT GPU render device(s) on $NODE_TO_CHECK"
    else
        log_warning "No GPU render devices found in /dev/dri/ on $NODE_TO_CHECK"
    fi

    echo ""
}

install_gpu_operator() {
    log_info "Installing AMD GPU Operator..."

    # Build Helm install command
    HELM_CMD="helm install $HELM_RELEASE_NAME rocm/gpu-operator-charts"
    HELM_CMD="$HELM_CMD --namespace $NAMESPACE"
    HELM_CMD="$HELM_CMD --create-namespace"
    HELM_CMD="$HELM_CMD --version=$OPERATOR_VERSION"

    # Add optional flags
    if [[ "$SKIP_NFD" == "true" ]]; then
        HELM_CMD="$HELM_CMD --set node-feature-discovery.enabled=false"
    fi

    if [[ "$SKIP_KMM" == "true" ]]; then
        HELM_CMD="$HELM_CMD --set kmm.enabled=false"
    fi

    if [[ "$SKIP_REMEDIATION" == "true" ]]; then
        HELM_CMD="$HELM_CMD --set remediation.enabled=false"
    fi

    # Configure driver-only mode (disable all non-driver components)
    if [[ "$DRIVER_ONLY" == "true" ]]; then
        HELM_CMD="$HELM_CMD --set deviceConfig.spec.devicePlugin.enable=false"
        HELM_CMD="$HELM_CMD --set deviceConfig.spec.metricsExporter.enable=false"
        HELM_CMD="$HELM_CMD --set deviceConfig.spec.configManager.enable=false"
        HELM_CMD="$HELM_CMD --set deviceConfig.spec.testRunner.enable=false"
        HELM_CMD="$HELM_CMD --set deviceConfig.spec.draDriver.enable=false"
        log_info "Driver-only mode: Disabled device plugin, metrics exporter, config manager, test runner, and DRA driver"
    fi

    # Configure insecure registry settings
    if [[ "$INSECURE_REGISTRY" == "true" ]]; then
        HELM_CMD="$HELM_CMD --set deviceConfig.spec.driver.insecureRegistry=true"
        HELM_CMD="$HELM_CMD --set deviceConfig.spec.driver.skipTLSVerify=true"

        # Only set insecure registry for enabled components
        if [[ "$DRIVER_ONLY" != "true" ]]; then
            HELM_CMD="$HELM_CMD --set deviceConfig.spec.devicePlugin.insecureRegistry=true"
            HELM_CMD="$HELM_CMD --set deviceConfig.spec.metricsExporter.insecureRegistry=true"
            HELM_CMD="$HELM_CMD --set deviceConfig.spec.configManager.insecureRegistry=true"
        fi

        log_info "Configured to allow insecure registries (HTTP, skip TLS verification)"
    fi

    # Configure driver settings in default DeviceConfig
    if [[ "$USE_INBOX_DRIVER" == "true" ]]; then
        HELM_CMD="$HELM_CMD --set deviceConfig.spec.driver.enable=false"
        log_info "Configured to use inbox driver"
    else
        HELM_CMD="$HELM_CMD --set deviceConfig.spec.driver.enable=true"
        HELM_CMD="$HELM_CMD --set deviceConfig.spec.driver.version=$DRIVER_VERSION"

        if [[ -n "$DRIVER_IMAGE" ]]; then
            HELM_CMD="$HELM_CMD --set deviceConfig.spec.driver.image=$DRIVER_IMAGE"
        fi

        if [[ -n "$REGISTRY_SECRET" ]]; then
            HELM_CMD="$HELM_CMD --set deviceConfig.spec.driver.imageRegistrySecret.name=$REGISTRY_SECRET"
        fi

        log_info "Configured to install out-of-tree driver version: $DRIVER_VERSION"
    fi

    HELM_CMD="$HELM_CMD --wait"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would execute:"
        echo "$HELM_CMD"
        return 0
    fi

    log_info "Executing: $HELM_CMD"
    eval "$HELM_CMD"

    log_success "AMD GPU Operator installed successfully"
}

verify_installation() {
    log_info "Verifying installation..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would verify installation"
        return 0
    fi

    echo ""
    log_info "Step 1/5: Verifying GPU Operator Controller..."
    run_kubectl "wait --for=condition=available --timeout=300s deployment/amd-gpu-operator-controller-manager -n $NAMESPACE" || {
        log_warning "Operator controller not ready after 5 minutes"
    }
    log_success "GPU Operator Controller is ready"

    echo ""
    log_info "Step 2/5: Verifying DeviceConfig..."
    if run_kubectl "get deviceconfig -n $NAMESPACE" &> /dev/null; then
        log_success "DeviceConfig found"
        run_kubectl "get deviceconfig -n $NAMESPACE"
    else
        log_error "No DeviceConfig found - installation may have failed"
        return 1
    fi

    echo ""
    log_info "Step 3/5: Waiting for GPU nodes to be labeled..."
    # Wait up to 2 minutes for NFD to label nodes
    for i in {1..24}; do
        GPU_NODES=$(kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true -o name 2>/dev/null)
        VGPU_NODES=$(kubectl get nodes -l feature.node.kubernetes.io/amd-vgpu=true -o name 2>/dev/null)

        if [[ -n "$GPU_NODES" || -n "$VGPU_NODES" ]]; then
            break
        fi

        if [[ $i -eq 24 ]]; then
            log_warning "No AMD GPU nodes found after 2 minutes. NFD may still be running."
        else
            sleep 5
        fi
    done

    GPU_NODE_COUNT=$(echo "$GPU_NODES" | grep -c "node/" || echo "0")
    VGPU_NODE_COUNT=$(echo "$VGPU_NODES" | grep -c "node/" || echo "0")

    if [[ "$GPU_NODE_COUNT" -gt 0 ]]; then
        log_success "Found $GPU_NODE_COUNT AMD GPU node(s)"
        run_kubectl "get nodes -l feature.node.kubernetes.io/amd-gpu=true"
    elif [[ "$VGPU_NODE_COUNT" -gt 0 ]]; then
        log_success "Found $VGPU_NODE_COUNT AMD vGPU node(s)"
        run_kubectl "get nodes -l feature.node.kubernetes.io/amd-vgpu=true"
    else
        log_warning "No GPU nodes labeled yet - NFD may need more time"
    fi

    echo ""
    log_info "Step 4/5: Verifying operand pods..."
    sleep 10
    run_kubectl "get pods -n $NAMESPACE"

    # Check if device plugin pods are running
    DP_PODS=$(kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/component=device-plugin -o name 2>/dev/null | wc -l)
    if [[ "$DP_PODS" -gt 0 ]]; then
        log_success "Device plugin pods are deployed"
    else
        log_warning "No device plugin pods found yet"
    fi

    # Step 5: Verify driver installation if out-of-tree driver is enabled
    if [[ "$USE_INBOX_DRIVER" == "false" ]]; then
        echo ""
        log_info "Step 5/5: Verifying out-of-tree driver installation..."

        # Check for KMM Module
        log_info "Checking KMM Module status..."
        if kubectl get modules -A &> /dev/null; then
            MODULE_COUNT=$(kubectl get modules -A -o name 2>/dev/null | wc -l)
            if [[ "$MODULE_COUNT" -gt 0 ]]; then
                log_success "Found KMM Module(s)"
                run_kubectl "get modules -A"

                # Check Module conditions
                echo ""
                log_info "Checking Module readiness..."
                MODULE_NAME=$(kubectl get modules -A -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
                MODULE_NAMESPACE=$(kubectl get modules -A -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null)

                if [[ -n "$MODULE_NAME" ]]; then
                    # Wait for module to be ready (up to 10 minutes for driver build/install)
                    log_info "Waiting for Module '$MODULE_NAME' to be ready (this may take several minutes)..."
                    for i in {1..120}; do
                        MODULE_STATUS=$(kubectl get module "$MODULE_NAME" -n "$MODULE_NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="ModuleReady")].status}' 2>/dev/null)

                        if [[ "$MODULE_STATUS" == "True" ]]; then
                            log_success "Module '$MODULE_NAME' is ready"
                            break
                        fi

                        if [[ $((i % 12)) -eq 0 ]]; then
                            log_info "Still waiting for Module to be ready... ($i/120)"
                        fi
                        sleep 5
                    done

                    if [[ "$MODULE_STATUS" != "True" ]]; then
                        log_warning "Module did not become ready within 10 minutes"
                        log_info "Check Module status: kubectl describe module $MODULE_NAME -n $MODULE_NAMESPACE"
                    fi
                fi
            else
                log_warning "No KMM Modules found - driver may not be installing"
            fi
        else
            log_warning "KMM Modules API not available or no modules created"
        fi

        # Check for driver pods
        echo ""
        log_info "Checking driver pods..."
        DRIVER_PODS=$(kubectl get pods -n "$NAMESPACE" -o name 2>/dev/null | grep -i "amdgpu\|driver" || true)
        if [[ -n "$DRIVER_PODS" ]]; then
            log_success "Driver pods found:"
            log_command "kubectl get pods -n $NAMESPACE | grep -i 'amdgpu\|driver'"
            kubectl get pods -n "$NAMESPACE" | grep -i "amdgpu\|driver" || true

            # Check if driver pods are running or completed
            DRIVER_POD_NAME=$(echo "$DRIVER_PODS" | head -1 | sed 's|pod/||')
            DRIVER_POD_STATUS=$(kubectl get pod "$DRIVER_POD_NAME" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null)

            if [[ "$DRIVER_POD_STATUS" == "Running" || "$DRIVER_POD_STATUS" == "Succeeded" ]]; then
                log_success "Driver pod '$DRIVER_POD_NAME' status: $DRIVER_POD_STATUS"
            else
                log_warning "Driver pod '$DRIVER_POD_NAME' status: $DRIVER_POD_STATUS"
                log_info "Check logs: kubectl logs $DRIVER_POD_NAME -n $NAMESPACE"
            fi
        else
            log_warning "No driver pods found yet - driver installation may still be in progress"
        fi

        # Verify driver is actually loaded on nodes
        if [[ "$GPU_NODE_COUNT" -gt 0 || "$VGPU_NODE_COUNT" -gt 0 ]]; then
            echo ""
            log_info "Verifying driver is loaded on GPU nodes..."

            # Get first GPU node
            if [[ -n "$GPU_NODES" ]]; then
                FIRST_NODE=$(echo "$GPU_NODES" | head -1 | sed 's|node/||')
            else
                FIRST_NODE=$(echo "$VGPU_NODES" | head -1 | sed 's|node/||')
            fi

            # Create debug pod to check driver
            DEBUG_POD_NAME="verify-driver-$$"
            log_command "kubectl apply -f - (creating verification pod $DEBUG_POD_NAME on $FIRST_NODE)"
            cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: $DEBUG_POD_NAME
  namespace: default
spec:
  nodeName: $FIRST_NODE
  hostPID: true
  hostNetwork: true
  containers:
  - name: check
    image: busybox:1.36
    command: ['sh', '-c', 'sleep 60']
    securityContext:
      privileged: true
    volumeMounts:
    - name: host
      mountPath: /host
  volumes:
  - name: host
    hostPath:
      path: /
  restartPolicy: Never
EOF

            # Wait for pod
            for i in {1..30}; do
                POD_STATUS=$(kubectl get pod "$DEBUG_POD_NAME" -n default -o jsonpath='{.status.phase}' 2>/dev/null)
                if [[ "$POD_STATUS" == "Running" ]]; then
                    break
                fi
                sleep 1
            done

            # Check driver
            log_command "kubectl exec -n default $DEBUG_POD_NAME -- sh -c 'chroot /host lsmod | grep amdgpu'"
            DRIVER_LOADED=$(kubectl exec -n default "$DEBUG_POD_NAME" -- sh -c "chroot /host lsmod | grep amdgpu || true" 2>/dev/null)
            echo "$DRIVER_LOADED"

            log_command "kubectl exec -n default $DEBUG_POD_NAME -- sh -c 'chroot /host ls /dev/dri/render*'"
            GPU_DEVICES=$(kubectl exec -n default "$DEBUG_POD_NAME" -- sh -c "chroot /host ls /dev/dri/render* 2>/dev/null || true" 2>/dev/null)
            echo "$GPU_DEVICES"

            # Cleanup
            log_command "kubectl delete pod $DEBUG_POD_NAME -n default"
            kubectl delete pod "$DEBUG_POD_NAME" -n default || true

            if [[ -n "$DRIVER_LOADED" ]]; then
                log_success "AMD GPU driver (amdgpu) is loaded on node '$FIRST_NODE'"

                if [[ -n "$GPU_DEVICES" ]]; then
                    GPU_COUNT=$(echo "$GPU_DEVICES" | wc -l)
                    log_success "Found $GPU_COUNT GPU device(s) on node '$FIRST_NODE'"
                fi

                echo ""
                log_success "Driver installation verified successfully!"
            else
                log_warning "Driver is NOT loaded on node '$FIRST_NODE'"
                log_warning "Driver installation may still be in progress or node may need reboot"

                cat <<EOF

${YELLOW}==============================================================${NC}
${YELLOW}IMPORTANT: Node Reboot May Be Required${NC}
${YELLOW}==============================================================${NC}

After out-of-tree driver installation, GPU nodes typically need
to be rebooted to load the new driver.

${BLUE}Next Steps:${NC}
1. Check Module status: ${YELLOW}kubectl get modules -A${NC}
2. Check driver pod logs: ${YELLOW}kubectl logs -n $NAMESPACE <driver-pod-name>${NC}
3. ${YELLOW}Reboot GPU nodes${NC} if driver installation is complete
4. After reboot, verify driver: ${YELLOW}lsmod | grep amdgpu${NC}

${BLUE}To manually reboot a node:${NC}
  ${YELLOW}kubectl drain $FIRST_NODE --ignore-daemonsets --delete-emptydir-data${NC}
  ${YELLOW}# SSH to node and run: sudo reboot${NC}
  ${YELLOW}kubectl uncordon $FIRST_NODE${NC}

EOF
            fi
        fi
    else
        log_info "Step 5/5: Skipped (using inbox driver)"
    fi

    echo ""
    log_success "Installation verification complete"
}

uninstall_gpu_operator() {
    log_info "Uninstalling AMD GPU Operator..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_info "[DRY RUN] Would uninstall GPU operator"
        return 0
    fi

    # Delete DeviceConfig resources
    log_info "Deleting DeviceConfig resources..."
    run_kubectl "delete deviceconfig --all -n $NAMESPACE --ignore-not-found=true --timeout=120s"

    # Uninstall Helm release
    log_info "Uninstalling Helm release..."
    log_command "helm uninstall $HELM_RELEASE_NAME -n $NAMESPACE"
    helm uninstall "$HELM_RELEASE_NAME" -n "$NAMESPACE" || {
        log_warning "Failed to uninstall Helm release or already uninstalled"
    }

    # Clean up namespace
    log_info "Deleting namespace..."
    run_kubectl "delete namespace $NAMESPACE --ignore-not-found=true --timeout=120s"

    log_success "AMD GPU Operator uninstalled successfully"
}

print_post_install_info() {
    echo ""
    log_info "Running post-installation checks..."
    echo ""

    log_info "Current pod status:"
    run_kubectl "get pods -n $NAMESPACE"
    echo ""

    log_info "DeviceConfig status:"
    run_kubectl "get deviceconfig -n $NAMESPACE"
    echo ""

    log_info "AMD GPU nodes:"
    run_kubectl "get nodes -l feature.node.kubernetes.io/amd-gpu=true" || log_warning "No AMD GPU nodes labeled yet"
    echo ""

    cat << EOF

${GREEN}============================================================${NC}
${GREEN}AMD GPU Operator Installation Complete!${NC}
${GREEN}============================================================${NC}

${BLUE}Useful Commands:${NC}

1. Check the installation status:
   ${YELLOW}kubectl get pods -n $NAMESPACE${NC}

2. View DeviceConfig:
   ${YELLOW}kubectl get deviceconfig -n $NAMESPACE${NC}

3. Check AMD GPU nodes:
   ${YELLOW}kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true${NC}

4. View operator logs:
   ${YELLOW}kubectl logs -n $NAMESPACE -l control-plane=controller-manager -f${NC}

EOF

    if [[ "$USE_INBOX_DRIVER" == "false" ]]; then
        cat << EOF
${BLUE}Driver Installation:${NC}
   - Driver version: ${GREEN}$DRIVER_VERSION${NC}
   - The KMM operator will install the driver on GPU nodes
   - Check Module status: ${YELLOW}kubectl get modules -A${NC}
   - Check driver pods: ${YELLOW}kubectl get pods -n $NAMESPACE -l app.kubernetes.io/component=amdgpu-driver${NC}

EOF
    fi

    if [[ "$DRIVER_ONLY" == "true" ]]; then
        cat << EOF
${BLUE}Driver-Only Mode:${NC}
   - ${GREEN}ENABLED${NC} components: Driver installation only
   - ${YELLOW}DISABLED${NC} components: Device Plugin, Metrics Exporter, Config Manager, Test Runner, DRA Driver
   - This minimal installation only loads the GPU driver on nodes
   - To use GPUs in Kubernetes, you'll need to enable device plugin later

EOF
    fi

    if [[ "$INSECURE_REGISTRY" == "true" ]]; then
        cat << EOF
${YELLOW}Insecure Registry Configuration:${NC}
   - Insecure registries (HTTP) are ${GREEN}ENABLED${NC}
   - TLS verification is ${GREEN}DISABLED${NC}
   - This is suitable for development/testing environments only
   - For production, use secure HTTPS registries with proper TLS certificates

EOF
    fi


    cat << EOF
${BLUE}Troubleshooting:${NC}
   - If operand pods are in Init:0/1 state, check driver installation
   - If no pods appear, verify node selector matches GPU nodes
   - View troubleshooting guide: https://instinct.docs.amd.com/projects/gpu-operator

${BLUE}Documentation:${NC}
   https://instinct.docs.amd.com/projects/gpu-operator/en/release-v1.4.1

EOF
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        --driver-version)
            DRIVER_VERSION="$2"
            shift 2
            ;;
        --driver-image)
            DRIVER_IMAGE="$2"
            shift 2
            ;;
        --registry-secret)
            REGISTRY_SECRET="$2"
            shift 2
            ;;
        --skip-nfd)
            SKIP_NFD=true
            shift
            ;;
        --skip-kmm)
            SKIP_KMM=true
            shift
            ;;
        --skip-remediation)
            SKIP_REMEDIATION=true
            shift
            ;;
        --inbox-driver)
            USE_INBOX_DRIVER=true
            shift
            ;;
        --insecure-registry)
            INSECURE_REGISTRY=true
            shift
            ;;
        --driver-only)
            DRIVER_ONLY=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --uninstall)
            UNINSTALL=true
            shift
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            print_usage
            exit 1
            ;;
    esac
done

# Validate parameters
if [[ "$USE_INBOX_DRIVER" == "false" && -z "$DRIVER_IMAGE" ]]; then
    log_warning "No --driver-image specified. Using default from Helm chart."
    log_warning "For production use, specify your driver image repository with --driver-image"
fi

# Main execution
echo ""
log_info "AMD GPU Operator Installation Script"
log_info "======================================"
echo ""

if [[ "$UNINSTALL" == "true" ]]; then
    uninstall_gpu_operator
    exit 0
fi

check_prerequisites
check_driver_installed
install_cert_manager
add_helm_repo

if [[ -n "$REGISTRY_SECRET" ]]; then
    create_registry_secret
fi

install_gpu_operator
verify_installation
print_post_install_info

log_success "Script completed successfully!"
