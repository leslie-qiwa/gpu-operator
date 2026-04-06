# Auto Node Remediation (ANR) Utilities for OpenShift

This document describes the utility functions in `autoremediation_util.py` for testing ANR on OpenShift.

## Overview

The utilities provide Python K8s API-based functions for:

- Installing/uninstalling Argo Workflows on OpenShift
- Managing node conditions for ANR testing
- Controlling workflow execution
- Verification and cleanup helpers

## Key Differences from Vanilla Kubernetes

On OpenShift:

1. **CRD Installation**: Argo CRDs must be installed separately due to their size
2. **Installation Method**: Uses Helm + manual CRD installation (not GPU Operator's automatic installation)
3. **OpenShift AI**: May already have Argo installed via DataScienceCluster

## Installation Functions

### 1. Check if Argo is Already Installed

```python
import lib.autoremediation_util as anr_util

# Check if Argo is installed (e.g., via OpenShift AI)
ret_code, info, error = anr_util.check_argo_installation_openshift()
if ret_code == 0:
    print("Argo Workflows is already installed")
else:
    print(f"Argo not found: {error}")
```

### 2. Install Argo CRDs

```python
# Install CRDs using Python K8s API (no kubectl needed!)
ret_code, msg, error = anr_util.install_argo_crds(version="v3.6.5")
```

**How it works:**

- Downloads CRD YAML files from GitHub
- Applies them using `kubernetes.utils.create_from_dict`
- Equivalent to: `kubectl apply --server-side -k "https://github.com/argoproj/argo-workflows/manifests/base/crds/full?ref=v3.6.5"`

### 3. Install Argo Workflows via Helm

```python
# Full installation: CRDs + Controller
ret_code, stdout, stderr = anr_util.install_argo_workflows_helm(
    namespace="argo-workflow",
    version="v3.6.5",
    install_crds=True  # Set False if CRDs already exist
)
```

### 4. Uninstall Argo Workflows

```python
# Uninstall controller and optionally remove CRDs
ret_code, stdout, stderr = anr_util.uninstall_argo_workflows_helm(
    namespace="argo-workflow",
    remove_crds=True  # Set False to keep CRDs for reuse
)
```

## Node Condition Management

### Simulate GPU Errors

```python
# Trigger ANR by setting a node condition
ret_code, resp, error = anr_util.set_node_condition(
    node_name="worker-1",
    condition_type="AMDGPUHwsHang",
    status="True",
    reason="GPUHangDetected",
    message="Simulated GPU hang for testing"
)
```

### Clear Conditions

```python
# Clear the condition after test
ret_code, resp, error = anr_util.clear_node_condition(
    node_name="worker-1",
    condition_type="AMDGPUHwsHang"
)
```

### Check Conditions

```python
# Verify a condition exists
has_hang = anr_util.check_node_has_condition(
    node_name="worker-1",
    condition_type="AMDGPUHwsHang",
    expected_status="True"
)
```

## Workflow Control

### Resume Suspended Workflow

```python
# Resume a workflow paused for manual intervention
ret_code, msg, error = anr_util.resume_workflow_by_label(node_name="worker-1")
```

Uses label: `operator.amd.com/gpu-force-resume-workflow=true`

### Abort Workflow

```python
# Abort a workflow (keeps node tainted)
ret_code, msg, error = anr_util.abort_workflow_by_label(node_name="worker-1")
```

Uses label: `operator.amd.com/gpu-abort-workflow=true`

## Verification Functions

### Verify Workflow Controller

```python
# Check if workflow controller is healthy
is_healthy = anr_util.verify_workflow_controller_deployment(
    namespace="kube-amd-gpu",
    controller_name="amd-gpu-operator-workflow-controller"
)
```

### Verify ConfigMap

```python
# Check if remediation configmap exists
ret_code, exists, error = anr_util.verify_remediation_configmap(
    namespace="kube-amd-gpu",
    deviceconfig_name="default"
)
```

### Check Workflow Status

```python
# Get current workflow phase
ret_code, phase, error = anr_util.get_workflow_status(
    namespace="kube-amd-gpu",
    workflow_name="remediation-worker-1-abc123"
)
# phase: "Pending", "Running", "Succeeded", "Failed", "Error"
```

### Check Node Taints

```python
# Check if ANR taint is present
has_taint = anr_util.check_node_has_taint(
    node_name="worker-1",
    taint_key="amd-gpu-unhealthy"
)
```

## DeviceConfig Management

### Enable/Disable Remediation

```python
# Enable remediation in DeviceConfig
ret_code, stdout, stderr = anr_util.enable_remediation_in_deviceconfig(
    namespace="kube-amd-gpu",
    deviceconfig_name="default",
    enable=True
)

# Disable remediation
ret_code, stdout, stderr = anr_util.enable_remediation_in_deviceconfig(
    namespace="kube-amd-gpu",
    deviceconfig_name="default",
    enable=False
)
```

## Test Cleanup

### Comprehensive Node Cleanup

```python
# Clean up after ANR test
ret_code, msg, error = anr_util.cleanup_node_for_anr_test(
    node_name="worker-1",
    namespace="kube-amd-gpu"
)
```

**What it does:**

- Clears test node conditions (AMDGPUHwsHang, AMDGPUXgmi, etc.)
- Removes all taints from node
- Deletes workflows associated with the node

## Example Test Flow

```python
import lib.autoremediation_util as anr_util
import lib.k8_util as k8_util

# 1. Check if Argo is installed
ret_code, _, error = anr_util.check_argo_installation_openshift()
if ret_code != 0:
    # Install Argo if not present
    ret_code, _, _ = anr_util.install_argo_workflows_helm(
        namespace="argo-workflow",
        version="v3.6.5",
        install_crds=True
    )
    assert ret_code == 0, "Failed to install Argo"

# 2. Enable remediation in DeviceConfig
ret_code, _, _ = anr_util.enable_remediation_in_deviceconfig(
    namespace="kube-amd-gpu",
    deviceconfig_name="default",
    enable=True
)

# 3. Verify controller is running
assert anr_util.verify_workflow_controller_deployment("kube-amd-gpu")

# 4. Verify configmap exists
ret_code, exists, _ = anr_util.verify_remediation_configmap(
    namespace="kube-amd-gpu",
    deviceconfig_name="default"
)
assert exists, "Remediation configmap not found"

# 5. Trigger remediation by setting node condition
ret_code, _, _ = anr_util.set_node_condition(
    node_name="worker-1",
    condition_type="AMDGPUHwsHang",
    status="True"
)

# 6. Wait for workflow completion (use existing function from test_node_remediation.py)
from tests.pytests.k8.gpu-operator.test_node_remediation import wait_for_argo_workflow
ret_code, stdout, stderr = wait_for_argo_workflow(
    namespace="kube-amd-gpu",
    target_node_name="worker-1",
    timeout=300
)
assert ret_code == 0, f"Workflow failed: {stderr}"

# 7. Verify node is clean (no taints)
assert not anr_util.check_node_has_taint("worker-1", "amd-gpu-unhealthy")

# 8. Cleanup
anr_util.cleanup_node_for_anr_test("worker-1", "kube-amd-gpu")
```

## Notes

- All functions use Python K8s APIs (no kubectl/oc commands except Helm)
- Leverages existing functions from `k8_util.py` where possible
- Compatible with both OpenShift and vanilla Kubernetes
- Handles edge cases (CRDs already exist, namespace doesn't exist, etc.)
