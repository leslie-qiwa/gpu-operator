# Pytest Framework Analysis and DRA Test Implementation

## Executive Summary

This document provides a comprehensive analysis of the pytest-based testing framework under `tests/pytests` and details the implementation of automation tests for the AMD GPU Kubernetes Driver for Dynamic Resource Allocation (DRA).

## Pytest Framework Analysis

### 1. Framework Architecture

#### Directory Structure
```
tests/pytests/
├── conftest.py              # Root-level fixtures (session scope)
├── pytest.ini               # Pytest configuration
├── requirements.txt         # Python dependencies
├── lib/                     # Utility libraries
│   ├── common.py           # Common classes and utilities
│   ├── k8_util.py          # Kubernetes API wrappers
│   ├── helm_util.py        # Helm command wrappers
│   ├── spec_util.py        # Spec/config generation
│   ├── amdgpu.py           # AMD GPU specific utilities
│   ├── dra_util.py         # DRA-specific utilities (NEW)
│   └── util.py             # Helper classes (K8Helper)
├── image-manifest/          # Image/version configurations
├── k8/                      # Kubernetes-specific tests
│   ├── conftest.py         # K8s session fixtures
│   ├── gpu-operator/       # GPU operator tests
│   ├── exporter/           # Metrics exporter tests
│   └── dra-driver/         # DRA driver tests (NEW)
└── openshift/              # OpenShift-specific tests
```

#### Key Design Patterns

**1. Fixture Hierarchy**
- **Session-scoped**: Cluster setup, image loading, environment configuration
- **Module-scoped**: Component installation (gpu-operator, exporter, DRA driver)
- **Function-scoped**: Test-specific resources (deviceconfigs, ResourceClaims)

**2. Environment Configuration**
```python
@pytest.fixture(scope="session")
def environment(request):
    # Loads:
    # - Deployment mode (k8, openshift, standalone)
    # - Image manifest
    # - Kube config
    # - Secrets
    # - Driver specs
```

**3. Image Management**
```python
@pytest.fixture(scope="session")
def images(request, gpu_cluster, environment):
    # Parses image manifest YAML
    # Downloads Helm charts, containers
    # Handles registry mirrors
    # Returns image configuration dict
```

**4. Helper Utilities**
```python
class K8Helper:
    @staticmethod
    def triage(environment, condition, message):
        # Centralized assertion handler
        # Collects tech-support on failure
        # Logs errors
        
    @staticmethod
    def wait_for_upgrade_completion_status(...)
    # Waits for async operations
```

### 2. Test Organization Patterns

#### Standard Test Structure
```python
@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    """Skip if wrong deployment mode"""
    if environment.deployment_mode != "k8":
        pytest.skip(...)

@pytest.fixture(scope="module")
def component_install(images, environment):
    """Install component under test"""
    # 1. Cleanup existing
    # 2. Install via Helm/kubectl
    # 3. Wait for ready state
    yield installed_info
    # 4. Cleanup
    
def test_component_feature(component_install, environment):
    """Test specific feature"""
    # 1. Setup test data
    # 2. Execute operation
    # 3. Verify results with K8Helper.triage()
    # 4. Cleanup test artifacts
```

#### Common Test Scenarios
1. **Installation Tests**: Verify Helm deployment, CRDs, pods
2. **Functional Tests**: Test component operations (device config, metrics, etc.)
3. **Integration Tests**: Test component interactions
4. **Workload Tests**: Run GPU workloads to verify functionality
5. **Upgrade/Uninstall Tests**: Test lifecycle operations

### 3. Utility Library Patterns

#### Kubernetes API Wrappers (`lib/k8_util.py`)
- Uses Python Kubernetes client
- Returns tuple: `(return_code, result, error)`
- Comprehensive logging
- Decorator pattern for argument logging

```python
@log_arguments
def k8_get_nodes() -> (int, K8Items):
    try:
        api = client.CoreV1Api()
        nodes = api.list_node().to_dict()
        return 0, nodes.get('items', None)
    except ApiException as ae:
        Logger.error(...)
        return -1, None
```

#### Helm Wrappers (`lib/helm_util.py`)
- Shell command execution via subprocess
- Handles chart repos, installation, upgrade, uninstall
- Values file generation from image manifest

#### Spec Generation (`lib/spec_util.py`)
- Generates Kubernetes YAML manifests
- Builds DeviceConfig CRs
- Creates Helm values.yaml files
- Template-based configuration

### 4. Image Manifest System

#### Format
```yaml
images:
  meta:
    registry:
      default: docker.io
      mirror:
        enable: yes
        url: registry.test.pensando.io:5000
  
  k8:
    component-name:
      key: helm.value.path        # Helm chart value path
      location: container://...   # Image location
      version: v1.2.3            # Version/tag
      kind: container|helm-chart  # Artifact type
```

#### Processing
1. Parse YAML with ruamel.yaml (preserves formatting)
2. Extract registry information
3. Download Helm charts if needed
4. Build image configuration dict
5. Replace `<registry>` placeholders

## DRA Test Implementation

### 1. Created Files

#### A. `lib/dra_util.py` - DRA Utility Library (518 lines)
**Purpose**: Kubernetes DRA API wrappers and helper functions

**Key Functions**:
- `get_dra_api_version()`: Auto-detect DRA API version (v1alpha3, v1beta1, v1)
- `create_resource_class()`: Create GPU ResourceClass
- `create_resource_claim()`: Create ResourceClaim with allocation modes
- `get_resource_claim()`: Retrieve claim details
- `wait_for_resource_claim_allocation()`: Poll for allocation
- `create_pod_with_resource_claim()`: Create Pod using DRA
- `list_resource_claims()`: List claims in namespace
- `cleanup_resource_claims()`: Cleanup utility
- `verify_dra_driver_crds()`: Verify DRA CRDs installed
- `generate_dra_driver_values()`: Generate Helm values

**API Design**: Follows existing pattern:
```python
def operation(...) -> Tuple[int, str, str]:
    """Operation description
    Args: ...
    Returns: (return_code, stdout, stderr)
    """
    try:
        # Kubernetes API call
        return 0, result, ""
    except ApiException as e:
        Logger.error(...)
        return -1, "", str(e)
```

#### B. `k8/dra-driver/conftest.py` - DRA Test Fixtures (143 lines)
**Fixtures**:
- `dra_driver_release_name`: Helm release name
- `dra_driver_namespace`: Namespace for DRA driver
- `init_dra_testbed`: Session-level setup/cleanup
- `dra_driver_install`: Module-level DRA driver installation

**Features**:
- K8s version check (requires 1.32+)
- Automatic cleanup of ResourceClaims
- Helm chart installation with custom values
- Pod readiness verification

#### C. `k8/dra-driver/test_dra_driver_install.py` - Installation Tests (224 lines)
**Test Cases**:
1. `test_dra_driver_install`: Verify Helm deployment
2. `test_dra_driver_check_pods`: Verify DaemonSet pods running
3. `test_dra_driver_check_crds`: Verify DRA CRDs (ResourceClass, ResourceClaim)
4. `test_dra_driver_check_resource_class`: Verify AMD GPU ResourceClass created
5. `test_dra_driver_logs_no_errors`: Check driver logs for errors
6. `test_dra_driver_gpu_node_labels`: Verify GPU node labels
7. `test_dra_driver_uninstall`: Test clean uninstallation

**Verification Points**:
- Helm release status
- Namespace creation
- Pod count and status
- CRD installation
- ResourceClass with `gpu.amd.com` driver
- Log error patterns
- Node labels for AMD GPU

#### D. `k8/dra-driver/test_dra_resource_allocation.py` - Allocation Tests (349 lines)
**Test Cases**:
1. `test_create_resource_claim`: Basic ResourceClaim creation
2. `test_resource_claim_allocation_immediate`: Immediate allocation mode
3. `test_resource_claim_allocation_wait_for_consumer`: WaitForFirstConsumer mode
4. `test_pod_with_resource_claim`: Pod using ResourceClaim
5. `test_multiple_resource_claims`: Multiple concurrent claims
6. `test_resource_claim_lifecycle`: Complete lifecycle (create→allocate→use→release→delete)
7. `test_list_resource_claims`: List claims in namespace

**Allocation Modes**:
- **Immediate**: GPU allocated when claim created
- **WaitForFirstConsumer**: GPU allocated when Pod references claim

**Verification Points**:
- ResourceClaim spec correctness
- Allocation status in claim
- Pod scheduling with claim
- Multiple GPU allocation
- Claim reusability after Pod deletion

#### E. `k8/dra-driver/test_dra_workloads.py` - Workload Tests (372 lines)
**Test Cases**:
1. `test_rocm_smi_workload`: Run rocm-smi via DRA
2. `test_pytorch_workload`: PyTorch GPU computation
3. `test_tensorflow_workload`: TensorFlow GPU computation
4. `test_concurrent_gpu_workloads`: Multiple simultaneous workloads
5. `test_gpu_memory_allocation`: GPU memory operations
6. `test_long_running_workload`: Long-running stability test

**Workload Patterns**:
```python
# 1. Create ResourceClaim
# 2. Wait for allocation
# 3. Create Pod with claim
# 4. Verify workload execution
# 5. Check logs for expected output
# 6. Cleanup Pod and claim
```

**Verification**:
- GPU detection (rocm-smi, PyTorch, TensorFlow)
- Computation correctness
- Concurrent execution
- Memory allocation/deallocation
- Long-running stability

#### F. `k8/dra-driver/README.md` - Documentation (180 lines)
**Contents**:
- Overview of DRA and test structure
- Detailed test descriptions
- Prerequisites and setup
- Running tests (commands and examples)
- DRA utility library reference
- Image manifest configuration
- Environment variables
- Key DRA concepts
- Troubleshooting guide
- References

### 2. Image Manifest Updates

**Modified**: `image-manifest/release_v1.4.1_internal_images.yaml`

**Added**:
```yaml
dra-driver:
    location    : repo://rocm.github.io/dra-driver:amd-gpu-dra-driver-charts
    version     : v0.1.0
    kind        : helm-chart

dra-driver-image:
    key         : image.repository
    location    : container://<registry>/rocm/amd-gpu-dra-driver
    version     : v0.1.0
    kind        : container
```

### 3. Test Coverage

#### Installation & Configuration
- ✅ Helm chart deployment
- ✅ CRD installation
- ✅ ResourceClass creation
- ✅ Pod deployment (DaemonSet)
- ✅ Node labeling
- ✅ Log validation
- ✅ Clean uninstallation

#### Resource Allocation
- ✅ ResourceClaim creation
- ✅ Immediate allocation
- ✅ WaitForFirstConsumer allocation
- ✅ Pod-claim binding
- ✅ Multiple concurrent claims
- ✅ Lifecycle management
- ✅ Claim listing

#### Workload Validation
- ✅ ROCm SMI execution
- ✅ PyTorch GPU computation
- ✅ TensorFlow GPU computation
- ✅ Concurrent workloads
- ✅ Memory management
- ✅ Long-running stability

### 4. Integration with Existing Framework

**Follows Established Patterns**:
1. ✅ Session/module/function fixture scoping
2. ✅ `K8Helper.triage()` for assertions
3. ✅ Image manifest integration
4. ✅ Logging with module-specific loggers
5. ✅ Cleanup with finalizers
6. ✅ Skip decorators for deployment modes
7. ✅ Return tuple pattern `(ret_code, stdout, stderr)`

**Reuses Existing Utilities**:
- `k8_util.k8_get_nodes()`, `k8_get_gpu_nodes()`
- `k8_util.k8_create_namespace()`, `k8_get_namespaces()`
- `k8_util.k8_get_pods()`, `k8_get_pod_logs()`
- `helm_util.helm_install()`, `helm_uninstall()`
- `K8Helper.triage()`, `K8Helper.delete_debug_pods()`

### 5. Running the Tests

#### Basic Execution
```bash
cd tests/pytests

# Run all DRA tests
pytest k8/dra-driver/ \
  --image-manifest=image-manifest/release_v1.4.1_internal_images.yaml \
  --deployment=k8

# Run specific test module
pytest k8/dra-driver/test_dra_driver_install.py \
  --image-manifest=image-manifest/release_v1.4.1_internal_images.yaml \
  --deployment=k8

# Run specific test
pytest k8/dra-driver/test_dra_workloads.py::test_pytorch_workload \
  --image-manifest=image-manifest/release_v1.4.1_internal_images.yaml \
  --deployment=k8 \
  -v -s
```

#### With HTML Report
```bash
pytest k8/dra-driver/ \
  --image-manifest=image-manifest/release_v1.4.1_internal_images.yaml \
  --deployment=k8 \
  --html=report.html \
  --self-contained-html
```

### 6. Next Steps / Recommendations

#### Immediate Actions
1. **Test Execution**: Run tests against a K8s 1.32+ cluster with AMD GPUs
2. **Image Verification**: Verify DRA driver Helm chart and container image URLs
3. **API Version**: Confirm DRA API version (v1alpha3, v1beta1, v1) in target cluster
4. **Dependencies**: Install any missing Python packages

#### Future Enhancements
1. **Additional Tests**:
   - GPU partitioning/MIG support (if applicable)
   - Resource limits and quotas
   - Node affinity with DRA
   - Pod eviction scenarios
   - Driver upgrade testing

2. **Advanced Workloads**:
   - Multi-GPU workloads
   - GPU sharing scenarios
   - Distributed training
   - Benchmark suites (ResNet, BERT, etc.)

3. **Performance Tests**:
   - Allocation latency measurements
   - Throughput testing
   - Resource cleanup timing

4. **Negative Tests**:
   - Invalid ResourceClaim configurations
   - Over-subscription scenarios
   - Driver failure recovery
   - Malformed Pod specs

5. **Integration Tests**:
   - DRA + Device Plugin coexistence
   - DRA + GPU Operator integration
   - DRA + Metrics Exporter

## Conclusion

The DRA test implementation provides comprehensive coverage of the AMD GPU Kubernetes Driver for Dynamic Resource Allocation, following established patterns and integrating seamlessly with the existing pytest framework. The tests cover installation, resource allocation, and workload validation scenarios, providing a solid foundation for validating DRA driver functionality.

### Summary of Created Files
- **lib/dra_util.py**: 518 lines - DRA utility library
- **k8/dra-driver/conftest.py**: 143 lines - Test fixtures
- **k8/dra-driver/test_dra_driver_install.py**: 224 lines - Installation tests
- **k8/dra-driver/test_dra_resource_allocation.py**: 349 lines - Allocation tests
- **k8/dra-driver/test_dra_workloads.py**: 372 lines - Workload tests
- **k8/dra-driver/README.md**: 180 lines - Documentation
- **Total**: ~1,786 lines of test code and documentation

### Framework Analysis Highlights
- Fixture-based architecture with clear scoping
- Image manifest system for version management
- Helper utilities for common operations
- Comprehensive logging and error handling
- Integration with CI/CD pipelines
- HTML report generation support
