# AMD GPU DRA Driver Tests

This directory contains pytest-based automation tests for the AMD GPU Kubernetes Driver for Dynamic Resource Allocation (DRA).

## Overview

Dynamic Resource Allocation (DRA) is a Kubernetes feature (beta in 1.32+) that provides a more flexible way to manage hardware resources like GPUs. The AMD GPU DRA driver implements this API for AMD GPUs.

## Test Structure

### Test Files

1. **`conftest.py`** - Test fixtures and session setup
   - `dra_driver_install`: Installs DRA driver via Helm
   - `dra_driver_namespace`: DRA driver namespace fixture
   - `init_dra_testbed`: Initializes and cleans up test environment

2. **`test_dra_driver_install.py`** - DRA driver installation and health tests
   - `test_dra_driver_install`: Verify Helm chart deployment
   - `test_dra_driver_check_pods`: Verify DRA driver pods are running
   - `test_dra_driver_check_crds`: Verify DRA CRDs are installed
   - `test_dra_driver_check_resource_class`: Verify AMD GPU ResourceClass creation
   - `test_dra_driver_logs_no_errors`: Check for errors in driver logs
   - `test_dra_driver_gpu_node_labels`: Verify GPU node labels
   - `test_dra_driver_uninstall`: Test clean uninstallation

3. **`test_dra_resource_allocation.py`** - ResourceClaim allocation tests
   - `test_create_resource_claim`: Basic ResourceClaim creation
   - `test_resource_claim_allocation_immediate`: Test Immediate allocation mode
   - `test_resource_claim_allocation_wait_for_consumer`: Test WaitForFirstConsumer mode
   - `test_pod_with_resource_claim`: Test Pod using ResourceClaim
   - `test_multiple_resource_claims`: Test multiple concurrent claims
   - `test_resource_claim_lifecycle`: Test complete claim lifecycle
   - `test_list_resource_claims`: Test listing claims in namespace

4. **`test_dra_workloads.py`** - GPU workload tests using DRA
   - `test_rocm_smi_workload`: Test rocm-smi with DRA
   - `test_pytorch_workload`: Test PyTorch GPU computation
   - `test_tensorflow_workload`: Test TensorFlow GPU computation
   - `test_concurrent_gpu_workloads`: Test multiple concurrent GPU workloads
   - `test_gpu_memory_allocation`: Test GPU memory operations
   - `test_long_running_workload`: Test long-running GPU workload stability

## Prerequisites

- Kubernetes cluster version 1.32 or higher (DRA beta support)
- AMD GPU nodes in the cluster
- DRA API enabled in the cluster
- AMD GPU DRA driver Helm chart available

## Running Tests

### Run all DRA tests:
```bash
cd tests/pytests
pytest k8/dra-driver/ \
  --image-manifest=image-manifest/release_v1.4.1_internal_images.yaml \
  --deployment=k8
```

### Run specific test file:
```bash
pytest k8/dra-driver/test_dra_driver_install.py \
  --image-manifest=image-manifest/release_v1.4.1_internal_images.yaml \
  --deployment=k8
```

### Run specific test:
```bash
pytest k8/dra-driver/test_dra_resource_allocation.py::test_create_resource_claim \
  --image-manifest=image-manifest/release_v1.4.1_internal_images.yaml \
  --deployment=k8
```

### Run with verbose output:
```bash
pytest k8/dra-driver/ -v -s \
  --image-manifest=image-manifest/release_v1.4.1_internal_images.yaml \
  --deployment=k8
```

## DRA Utility Library

The `lib/dra_util.py` module provides helper functions for DRA testing:

### ResourceClass Management
- `create_resource_class()`: Create a ResourceClass
- `delete_resource_class()`: Delete a ResourceClass
- `verify_dra_driver_crds()`: Verify DRA CRDs are installed

### ResourceClaim Management
- `create_resource_claim()`: Create a ResourceClaim
- `get_resource_claim()`: Get ResourceClaim details
- `delete_resource_claim()`: Delete a ResourceClaim
- `list_resource_claims()`: List ResourceClaims in namespace
- `wait_for_resource_claim_allocation()`: Wait for claim to be allocated

### Pod Management
- `create_pod_with_resource_claim()`: Create a Pod that uses a ResourceClaim

### Utilities
- `get_dra_api_version()`: Determine DRA API version in cluster
- `get_dra_device_allocations()`: Get GPU allocations from claims
- `cleanup_resource_claims()`: Clean up all ResourceClaims in namespace
- `generate_dra_driver_values()`: Generate Helm values.yaml for DRA driver

## Image Manifest Configuration

The DRA driver image should be configured in the image manifest file:

```yaml
images:
  k8:
    dra-driver:
      location: repo://rocm.github.io/dra-driver:amd-gpu-dra-driver-charts
      version: v0.1.0
      kind: helm-chart
    
    dra-driver-image:
      key: image.repository
      location: container://<registry>/rocm/amd-gpu-dra-driver
      version: v0.1.0
      kind: container
```

## Environment Variables

- `DRA_DRIVER_NAMESPACE`: Namespace for DRA driver (default: `kube-amd-gpu-dra`)

## Key Concepts

### ResourceClass
A ResourceClass defines a type of resource (e.g., AMD GPU) and associates it with a driver. The DRA driver automatically creates a ResourceClass for `gpu.amd.com`.

### ResourceClaim
A ResourceClaim is a request for resources. It references a ResourceClass and can be:
- **Immediate**: Allocated immediately when created
- **WaitForFirstConsumer**: Allocated when a Pod references it

### Pod Integration
Pods use ResourceClaims through the `resourceClaims` and `resources.claims` fields in the Pod spec.

## Troubleshooting

### DRA CRDs not found
Ensure Kubernetes version is 1.32+ and DRA feature gate is enabled.

### ResourceClaim not allocated
- Check DRA driver pod logs
- Verify GPU nodes have available capacity
- Check ResourceClass exists and has correct driver name

### Pod not scheduling
- Verify ResourceClaim is allocated
- Check Pod events for scheduling errors
- Ensure node has required GPU resources

## References

- [Kubernetes DRA Documentation](https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/)
- [AMD GPU DRA Driver Repository](https://github.com/ROCm/k8s-gpu-dra-driver)
- [DRA API Reference](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/resource-claim-v1alpha3/)
