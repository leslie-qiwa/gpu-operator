# AMD GPU Driver Installation Script

Automated installation script for AMD GPU drivers via the AMD GPU Operator on Kubernetes clusters.

Based on the official AMD GPU Operator documentation: https://instinct.docs.amd.com/projects/gpu-operator/en/release-v1.4.1

---

## 🚀 Quick Start

### TL;DR - Get Started in 30 Seconds

```bash
# Basic installation with inbox driver
./install_gpu_driver.sh --inbox-driver

# OR install with out-of-tree driver
./install_gpu_driver.sh \
  --driver-version 30.20.1 \
  --driver-image docker.io/rocm/amdgpu-driver
```

---

## 📋 Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Quick Start Examples](#quick-start-examples)
- [Usage](#usage)
- [Installation Process](#installation-process)
- [Post-Installation](#post-installation)
- [Troubleshooting](#troubleshooting)
- [Advanced Configuration](#advanced-configuration)
- [Components](#components)
- [Additional Resources](#additional-resources)

---

## Overview

The `install_gpu_driver.sh` script provides a simple, automated way to:
- ✅ Install prerequisites (cert-manager)
- ✅ Deploy the AMD GPU Operator via Helm
- ✅ Configure and install GPU drivers (inbox or out-of-tree)
- ✅ Verify the installation
- ✅ Display post-installation guidance

### What Gets Installed

**Default Installation:**
- AMD GPU Operator Controller
- Node Feature Discovery (NFD) - Auto-detects AMD GPUs
- Kernel Module Management (KMM) - Manages driver lifecycle
- Device Plugin - GPU resource allocation
- Metrics Exporter - Prometheus metrics collection
- Auto Node Remediation - Automated GPU failure recovery

---

## Prerequisites

Before running the script, ensure you have:

- **Kubernetes cluster** v1.29.0 or higher
- **kubectl** CLI tool configured with cluster access
- **Helm** v3.2.0 or higher
- **AMD Instinct GPUs** on worker nodes:
  - MI355X, MI350X, MI325X, MI300X, MI250, MI210
- **Cluster admin privileges**

The script will automatically verify these requirements before installation.

---

## Quick Start Examples

### 1. First Time Setup (Recommended)

```bash
./install_gpu_driver.sh \
  --driver-version 30.20.1 \
  --driver-image docker.io/rocm/amdgpu-driver
```

**What this does:**
- Installs AMD GPU Operator with all default components
- Configures out-of-tree driver version 30.20.1
- Enables device plugin for GPU allocation
- Enables metrics collection

### 2. Production Setup with Private Registry

```bash
# Step 1: Create registry secret
kubectl create secret docker-registry my-registry \
  -n kube-amd-gpu \
  --docker-server=my-registry.io \
  --docker-username=myuser \
  --docker-password=mypassword

# Step 2: Install with secret
./install_gpu_driver.sh \
  --driver-version 30.20.1 \
  --driver-image my-registry.io/amdgpu/driver \
  --registry-secret my-registry
```

### 3. Use Pre-installed Driver (Inbox)

```bash
./install_gpu_driver.sh --inbox-driver
```

**Use this when:**
- Driver is already installed on host OS
- Using distribution-provided driver packages
- Don't want operator to manage driver installation

### 4. Enable DRA (Kubernetes 1.32+)

```bash
./install_gpu_driver.sh \
  --enable-dra \
  --driver-version 30.20.1 \
  --driver-image docker.io/rocm/amdgpu-driver
```

**DRA provides:**
- Advanced GPU scheduling capabilities
- Fine-grained resource constraints
- Better multi-GPU workload support

### 5. Minimal Installation

```bash
./install_gpu_driver.sh \
  --inbox-driver \
  --skip-remediation \
  --skip-nfd
```

Installs only essential components (smaller footprint).

### 6. Dry Run (Preview Only)

```bash
./install_gpu_driver.sh \
  --driver-version 30.20.1 \
  --driver-image docker.io/rocm/amdgpu-driver \
  --dry-run
```

Shows what would be installed without making any changes.

### 7. Uninstall Everything

```bash
./install_gpu_driver.sh --uninstall
```

Removes GPU operator, DeviceConfig, and all components.

---

## Usage

```bash
./install_gpu_driver.sh [OPTIONS]
```

### Command-Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `--namespace <ns>` | Namespace for GPU operator | `kube-amd-gpu` |
| `--driver-version <ver>` | Driver version to install | `30.20.1` |
| `--driver-image <image>` | Driver image repository (without tag) | - |
| `--registry-secret <name>` | Docker registry secret name | - |
| `--skip-nfd` | Skip Node Feature Discovery installation | `false` |
| `--skip-kmm` | Skip Kernel Module Management installation | `false` |
| `--skip-remediation` | Skip Auto Node Remediation installation | `false` |
| `--enable-dra` | Enable DRA driver instead of device plugin | `false` |
| `--inbox-driver` | Use inbox driver (skip out-of-tree driver) | `false` |
| `--dry-run` | Show what would be installed without installing | `false` |
| `--uninstall` | Uninstall GPU operator and driver | `false` |
| `-h, --help` | Show help message | - |

### Additional Examples

#### Install in Custom Namespace

```bash
./install_gpu_driver.sh \
  --namespace my-gpu-namespace \
  --driver-version 30.20.1 \
  --driver-image docker.io/rocm/amdgpu-driver
```

#### Skip Optional Components

```bash
./install_gpu_driver.sh \
  --driver-version 30.20.1 \
  --driver-image docker.io/rocm/amdgpu-driver \
  --skip-nfd \
  --skip-remediation
```

---

## Installation Process

### What the Script Does

#### 1. Prerequisites Check
- ✓ Verifies kubectl is installed and configured
- ✓ Verifies Helm is installed (≥ v3.2.0)
- ✓ Checks Kubernetes cluster connectivity
- ✓ Validates Kubernetes version (≥ v1.29.0)

#### 2. Install cert-manager
- Adds the Jetstack Helm repository
- Installs cert-manager v1.15.1 if not already present
- Required for webhook certificate management

#### 3. Add AMD GPU Operator Helm Repository
- Adds `rocm` Helm repository
- Updates repository cache

#### 4. Verify Registry Secrets (if specified)
- Checks for existence of registry secret
- Prompts user if secret is missing

#### 5. Install AMD GPU Operator
- Deploys operator via Helm with specified configuration
- Creates default DeviceConfig with driver settings
- Configures device plugin or DRA driver
- Enables optional components (metrics, remediation)

#### 6. Verification
- Waits for operator deployment to be ready
- Checks DeviceConfig creation
- Lists GPU nodes (if labeled)
- Shows operand pod status

#### 7. Post-Installation Guidance
- Displays next steps
- Shows verification commands
- Provides troubleshooting tips

---

## Post-Installation

### Verify Installation

#### Check All Pods are Running

```bash
kubectl get pods -n kube-amd-gpu
```

**Expected pods:**
```
NAME                                               READY   STATUS
amd-gpu-operator-controller-manager-xxx            2/2     Running
device-plugin-daemonset-xxx                        1/1     Running
node-labeller-daemonset-xxx                        1/1     Running
metrics-exporter-daemonset-xxx                     1/1     Running
kmm-operator-controller-xxx                        1/1     Running
```

If using out-of-tree driver:
```
amdgpu-driver-xxx                                  1/1     Running
```

#### Check DeviceConfig

```bash
# List DeviceConfigs
kubectl get deviceconfig -n kube-amd-gpu

# Detailed view
kubectl describe deviceconfig -n kube-amd-gpu default
```

#### Check GPU Nodes

```bash
# For physical GPUs
kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true

# For virtual GPUs (in VMs)
kubectl get nodes -l feature.node.kubernetes.io/amd-vgpu=true

# Show all labels
kubectl get nodes --show-labels | grep amd
```

#### Check GPU Resource Allocation

```bash
# Replace <gpu-node-name> with actual node name
kubectl describe node <gpu-node-name> | grep amd.com/gpu

# Expected output:
#  amd.com/gpu:     8  (number of GPUs available)
```

#### View Driver Status (Out-of-Tree Driver)

```bash
# Check KMM Module resource
kubectl get modules -A

# Check driver pods
kubectl get pods -n kube-amd-gpu -l app.kubernetes.io/component=amdgpu-driver

# View driver logs
kubectl logs -n kube-amd-gpu -l app.kubernetes.io/component=amdgpu-driver
```

### Test GPU Allocation

Create a test pod to verify GPU allocation works:

```bash
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: gpu-test
spec:
  containers:
  - name: gpu-container
    image: rocm/tensorflow:latest
    command: ["/bin/bash", "-c", "rocm-smi && sleep 3600"]
    resources:
      limits:
        amd.com/gpu: 1
  restartPolicy: Never
EOF

# Check if GPU is allocated
kubectl logs gpu-test

# Should show GPU information from rocm-smi

# Clean up
kubectl delete pod gpu-test
```

### Monitor Operator Logs

```bash
# Operator controller logs
kubectl logs -n kube-amd-gpu -l control-plane=controller-manager -f

# KMM operator logs
kubectl logs -n kube-amd-gpu -l control-plane=controller-manager --tail=100

# Device plugin logs
kubectl logs -n kube-amd-gpu -l app.kubernetes.io/component=device-plugin
```

---

## Troubleshooting

### Common Issues and Quick Fixes

#### Issue 1: Operand Pods Stuck in Init:0/1 State

**Symptoms:**
- Device plugin or metrics exporter pods show `Init:0/1`
- Pods never become ready

**Cause:** No GPU driver loaded on worker nodes.

**Solution:**

If using **inbox driver**, verify driver is installed on host:
```bash
# SSH to worker node
lsmod | grep amdgpu
rocm-smi
```

If using **out-of-tree driver**, check Module and driver pod status:
```bash
kubectl get modules -A
kubectl logs -n kube-amd-gpu -l app.kubernetes.io/component=amdgpu-driver
```

Enable out-of-tree driver in DeviceConfig:
```bash
kubectl edit deviceconfig -n kube-amd-gpu default

# Set the following:
spec:
  driver:
    enable: true
    version: "30.20.1"
    image: "docker.io/rocm/amdgpu-driver"
```

#### Issue 2: No Operand Pods Appear

**Symptoms:**
- No device-plugin or other operand pods are created
- Only operator controller is running

**Cause:** Node selector in DeviceConfig doesn't match any nodes.

**Solution:**

Check node labels:
```bash
kubectl get nodes --show-labels | grep amd
```

If nodes have `amd-gpu` label, verify DeviceConfig selector:
```bash
kubectl get deviceconfig -n kube-amd-gpu default -o yaml | grep -A2 selector
```

For **virtual GPUs (VMs)**, update DeviceConfig selector:
```bash
kubectl edit deviceconfig -n kube-amd-gpu default

# Change selector to:
spec:
  selector:
    feature.node.kubernetes.io/amd-vgpu: "true"
```

For **custom node labels**, update to match your environment:
```bash
spec:
  selector:
    your-custom-label: "true"
```

#### Issue 3: Driver Installation Fails

**Symptoms:**
- Driver pods fail to start
- Module shows errors or stays in pending state
- KMM operator reports build failures

**Cause:** Various (image pull issues, build failures, kernel incompatibility)

**Diagnosis:**

Check Module status:
```bash
kubectl get modules -A
kubectl describe module <module-name> -n <namespace>
```

Check KMM operator logs:
```bash
kubectl logs -n kube-amd-gpu -l control-plane=controller-manager --tail=100
```

Check driver pod logs:
```bash
kubectl logs -n kube-amd-gpu -l app.kubernetes.io/component=amdgpu-driver
```

**Solutions:**

1. **Image pull errors**: Verify image exists and is accessible
   ```bash
   # Test pulling the image
   docker pull docker.io/rocm/amdgpu-driver:30.20.1
   ```

2. **Kernel incompatibility**: Check kernel version compatibility
   ```bash
   # On worker node
   uname -r
   # Verify driver version supports this kernel
   ```

3. **Build failures**: Check build logs in KMM worker pods
   ```bash
   kubectl logs -n kube-amd-gpu -l kmm.node.kubernetes.io/module.name=amdgpu
   ```

#### Issue 4: Registry Authentication Errors

**Symptoms:**
- `ImagePullBackOff` errors
- "unauthorized" or "authentication required" messages

**Cause:** Missing or incorrect registry credentials.

**Solution:**

Create registry secret in the correct namespace:
```bash
kubectl create secret docker-registry my-secret \
  -n kube-amd-gpu \
  --docker-server=my-registry.io \
  --docker-username=myuser \
  --docker-password=mypassword \
  --docker-email=myemail@example.com
```

Update DeviceConfig to reference the secret:
```bash
kubectl edit deviceconfig -n kube-amd-gpu default

# Add under spec.driver:
spec:
  driver:
    imageRegistrySecret:
      name: my-secret
```

For KMM images, also update:
```bash
# Via Helm upgrade
helm upgrade amd-gpu-operator rocm/gpu-operator-charts \
  -n kube-amd-gpu \
  --set kmm.controller.manager.env.relatedImageWorkerPullSecret=my-secret
```

#### Issue 5: cert-manager Installation Fails

**Symptoms:**
- cert-manager pods not starting
- Webhook certificate errors

**Cause:** cert-manager CRDs or resources already exist with conflicts

**Solution:**

Check existing cert-manager installation:
```bash
kubectl get pods -n cert-manager
kubectl get crds | grep cert-manager
```

If cert-manager exists but is broken, clean up and reinstall:
```bash
helm uninstall cert-manager -n cert-manager
kubectl delete namespace cert-manager
# Wait for namespace deletion
./install_gpu_driver.sh --dry-run  # Preview installation
```

#### Issue 6: Helm Installation Fails

**Symptoms:**
- Helm install command fails
- Resource conflicts or admission webhook errors

**Cause:** Previous installation remnants or CRD conflicts

**Solution:**

Clean up existing resources:
```bash
# Uninstall operator
./install_gpu_driver.sh --uninstall

# Manually clean up if needed
kubectl delete deviceconfig --all -n kube-amd-gpu
kubectl delete namespace kube-amd-gpu

# Remove Helm release
helm uninstall amd-gpu-operator -n kube-amd-gpu

# Reinstall
./install_gpu_driver.sh <your-options>
```

### Getting More Help

View operator logs for detailed error messages:
```bash
kubectl logs -n kube-amd-gpu deployment/amd-gpu-operator-controller-manager -c manager -f
```

Check events:
```bash
kubectl get events -n kube-amd-gpu --sort-by='.lastTimestamp'
```

---

## Advanced Configuration

### Driver Versions

The script defaults to driver version `30.20.1` (ROCm 6.2.x compatible).

**Version Information:**
- **30.20.1** - ROCm 6.2.x series
- Check ROCm compatibility: https://rocm.docs.amd.com/en/latest/compatibility/compatibility-matrix.html

**Upgrading Driver Version:**

```bash
kubectl edit deviceconfig -n kube-amd-gpu default

# Update spec.driver.version
spec:
  driver:
    version: "30.20.2"  # New version
```

The operator will automatically handle the rolling upgrade.

### Custom DeviceConfig

After installation, customize DeviceConfig for advanced features:

```bash
kubectl edit deviceconfig -n kube-amd-gpu default
```

**Example configurations:**

**Enable GPU Partitioning:**
```yaml
spec:
  configManager:
    enable: true
    image: rocm/device-config-manager:v1.4.1
    config:
      name: partition-config  # ConfigMap with partition settings
```

**Enable Test Runner:**
```yaml
spec:
  testRunner:
    enable: true
    image: rocm/test-runner:v1.4.1
    logsLocation:
      mountPath: "/var/log/amd-test-runner"
      hostPath: "/var/log/amd-test-runner"
```

**Configure Metrics with RBAC:**
```yaml
spec:
  metricsExporter:
    enable: true
    serviceType: "NodePort"
    nodePort: 32500
    rbacConfig:
      enable: true
      image: quay.io/brancz/kube-rbac-proxy:v0.18.1
```

**Enable Prometheus ServiceMonitor:**
```yaml
spec:
  metricsExporter:
    prometheus:
      serviceMonitor:
        enable: true
        interval: "30s"
        honorLabels: true
```

### Multi-DeviceConfig Setup

For different GPU configurations on different node groups:

```bash
# Create custom DeviceConfig for specific nodes
cat <<EOF | kubectl apply -f -
apiVersion: amd.com/v1alpha1
kind: DeviceConfig
metadata:
  name: mi300x-config
  namespace: kube-amd-gpu
spec:
  selector:
    gpu-type: mi300x
  driver:
    enable: true
    version: "30.20.1"
    image: docker.io/rocm/amdgpu-driver
  devicePlugin:
    enableDevicePlugin: true
    devicePluginImage: rocm/k8s-device-plugin:latest
EOF
```

---

## Components

### Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Kubernetes Cluster                    │
├─────────────────────────────────────────────────────────┤
│  Control Plane                                           │
│  ┌──────────────────────────┐                           │
│  │ AMD GPU Operator         │                           │
│  │ Controller Manager       │                           │
│  └──────────────────────────┘                           │
│  ┌──────────────────────────┐  ┌─────────────────────┐ │
│  │ KMM Operator             │  │ NFD Operator        │ │
│  └──────────────────────────┘  └─────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│  GPU Worker Nodes                                        │
│  ┌───────────────────────────────────────────────────┐ │
│  │ ┌──────────┐ ┌──────────┐ ┌───────────────────┐  │ │
│  │ │ Driver   │ │ Device   │ │ Metrics Exporter  │  │ │
│  │ │ Pod      │ │ Plugin   │ │                   │  │ │
│  │ └──────────┘ └──────────┘ └───────────────────┘  │ │
│  │                                                    │ │
│  │ ┌────────────────────────────────────────────┐   │ │
│  │ │         AMD Instinct GPU(s)                │   │ │
│  │ └────────────────────────────────────────────┘   │ │
│  └───────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### Core Components (Always Installed)

1. **AMD GPU Operator Controller**
   - Main reconciliation loop for DeviceConfig CRs
   - Manages operand lifecycle
   - Coordinates driver installation and upgrades

2. **Device Plugin** OR **DRA Driver**
   - Device Plugin: Traditional GPU resource allocation
   - DRA Driver: Advanced resource scheduling (K8s 1.32+)

3. **Node Labeller**
   - Labels nodes with GPU capabilities
   - Runs as init container with device plugin

### Optional Components (Installed by Default)

4. **Node Feature Discovery (NFD)**
   - Auto-detects AMD GPUs via PCI vendor ID
   - Labels nodes with `feature.node.kubernetes.io/amd-gpu=true`
   - Skip with `--skip-nfd`

5. **Kernel Module Management (KMM)**
   - Manages out-of-tree driver lifecycle
   - Handles driver building, signing, loading
   - Skip with `--skip-kmm` (only if using inbox driver)

6. **Metrics Exporter**
   - Collects GPU metrics (utilization, temperature, power)
   - Exposes Prometheus endpoints
   - Enabled by default in DeviceConfig

7. **Auto Node Remediation**
   - Monitors GPU health via node conditions
   - Triggers Argo Workflows for failure recovery
   - Skip with `--skip-remediation`

### Optional Components (Disabled by Default)

8. **Config Manager**
   - GPU partitioning and configuration
   - MIG-like GPU sharing
   - Enable in DeviceConfig

9. **Test Runner**
   - GPU validation and health checks
   - Automated testing workflows
   - Enable in DeviceConfig

---

## Additional Resources

### Documentation

- **Official Documentation**: https://instinct.docs.amd.com/projects/gpu-operator
- **GitHub Repository**: https://github.com/ROCm/gpu-operator
- **ROCm Documentation**: https://rocm.docs.amd.com/
- **Issue Tracker**: https://github.com/ROCm/gpu-operator/issues

### Configuration Examples

Local examples in this repository:
- `config/samples/amd.com_deviceconfigs.yaml` - Sample DeviceConfig
- `example/deviceconfig_example.yaml` - Comprehensive example with all options
- `example/metricsExporter/` - Metrics exporter configurations

### Related Tools

- **Helm**: https://helm.sh/docs/
- **cert-manager**: https://cert-manager.io/docs/
- **Node Feature Discovery**: https://kubernetes-sigs.github.io/node-feature-discovery/
- **Kernel Module Management**: https://github.com/rh-ecosystem-edge/kernel-module-management

### What's Next?

After successful installation:

1. **Deploy GPU Workloads**
   - Machine learning frameworks (TensorFlow, PyTorch)
   - AI inference applications
   - HPC workloads

2. **Set Up Monitoring**
   - Configure Prometheus scraping
   - Import Grafana dashboards
   - Set up alerting rules

3. **Configure GPU Partitioning**
   - Enable Device Config Manager
   - Create partition profiles
   - Test workload isolation

4. **Set Up Auto-Remediation**
   - Define remediation workflows
   - Configure node conditions
   - Test failure recovery

5. **Performance Tuning**
   - Optimize driver settings
   - Configure power management
   - Tune workload scheduling

---

## License

This script is part of the AMD GPU Operator project, licensed under Apache License 2.0.

---

## Support

For issues and questions:
- **Script issues**: Check this README and try `--dry-run` mode
- **Operator issues**: See [GitHub Issues](https://github.com/ROCm/gpu-operator/issues)
- **General help**: Run `./install_gpu_driver.sh --help`
