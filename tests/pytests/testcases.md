# GPU-Operator Pytest Test Cases

This document provides a comprehensive overview of all test cases in the `tests/pytests` directory with detailed test steps and validation criteria.

## Test Cases Overview Table

| File Name                                  | Test Case Name                                 | Test Case Description                                                                                                    | Detailed Section                                         |
| ------------------------------------------ | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------- |
| `k8/test_gpu_operator.py`                  | `test_gpu_operator_install`                    | Tests the installation of GPU-Operator using Helm charts with various configurations and validates successful deployment | [Details](#test_gpu_operator_install)                    |
| `k8/test_gpu_operator.py`                  | `test_gpu_operator_upgrade`                    | Tests upgrading GPU-Operator from one version to another and verifies all components are updated correctly               | [Details](#test_gpu_operator_upgrade)                    |
| `k8/test_gpu_operator.py`                  | `test_gpu_operator_uninstall`                  | Tests the complete removal of GPU-Operator and validates cleanup of all resources                                        | [Details](#test_gpu_operator_uninstall)                  |
| `k8/test_config_manager.py`                | `test_config_manager_deployment`               | Tests deployment of device configuration manager and validates its functionality                                         | [Details](#test_config_manager_deployment)               |
| `k8/test_config_manager.py`                | `test_config_manager_config_update`            | Tests updating device configuration through config manager and verifies changes are applied                              | [Details](#test_config_manager_config_update)            |
| `k8/test_config_manager.py`                | `test_config_manager_rollback`                 | Tests rollback functionality of config manager when configuration updates fail                                           | [Details](#test_config_manager_rollback)                 |
| `k8/test_metrics_exporter.py`              | `test_metrics_exporter_deployment`             | Tests deployment of metrics exporter component and validates metric collection                                           | [Details](#test_metrics_exporter_deployment)             |
| `k8/test_metrics_exporter.py`              | `test_metrics_exporter_prometheus_integration` | Tests integration with Prometheus for GPU metrics scraping                                                               | [Details](#test_metrics_exporter_prometheus_integration) |
| `k8/test_metrics_exporter.py`              | `test_metrics_exporter_service_monitor`        | Tests ServiceMonitor creation for Prometheus operator integration                                                        | [Details](#test_metrics_exporter_service_monitor)        |
| `k8/test_node_labeller.py`                 | `test_node_labeller_gpu_detection`             | Tests automatic detection and labeling of GPU nodes in the cluster                                                       | [Details](#test_node_labeller_gpu_detection)             |
| `k8/test_node_labeller.py`                 | `test_node_labeller_label_accuracy`            | Tests accuracy of GPU-related node labels applied by the node labeller                                                   | [Details](#test_node_labeller_label_accuracy)            |
| `k8/test_node_labeller.py`                 | `test_node_labeller_multi_gpu_support`         | Tests node labelling for nodes with multiple GPU types                                                                   | [Details](#test_node_labeller_multi_gpu_support)         |
| `k8/test_driver_deviceplugin.py`           | `test_driver_installation`                     | Tests GPU driver installation on worker nodes                                                                            | [Details](#test_driver_installation)                     |
| `k8/test_driver_deviceplugin.py`           | `test_device_plugin_discovery`                 | Tests GPU device discovery by the Kubernetes device plugin                                                               | [Details](#test_device_plugin_discovery)                 |
| `k8/test_driver_deviceplugin.py`           | `test_device_plugin_allocation`                | Tests GPU resource allocation to pods through device plugin                                                              | [Details](#test_device_plugin_allocation)                |
| `k8/test_driver_deviceplugin.py`           | `test_driver_upgrade`                          | Tests GPU driver upgrade functionality and node rebooting                                                                | [Details](#test_driver_upgrade)                          |
| `k8/test_driver_deviceplugin.py`           | `test_multiple_gpu_allocation`                 | Tests allocation of multiple GPU resources to a single pod                                                               | [Details](#test_multiple_gpu_allocation)                 |
| `k8/test_test_runner.py`                   | `test_runner_deployment`                       | Tests deployment of test runner component for GPU validation                                                             | [Details](#test_runner_deployment)                       |
| `k8/test_test_runner.py`                   | `test_runner_gpu_validation`                   | Tests GPU functionality validation through test runner workloads                                                         | [Details](#test_runner_gpu_validation)                   |
| `k8/test_test_runner.py`                   | `test_runner_stress_testing`                   | Tests GPU stress testing capabilities through test runner                                                                | [Details](#test_runner_stress_testing)                   |
| `k8/test_test_runner.py`                   | `test_runner_benchmark_execution`              | Tests execution of GPU benchmarks through test runner                                                                    | [Details](#test_runner_benchmark_execution)              |
| `openshift/test_openshift_gpu_operator.py` | `test_openshift_operator_install`              | Tests GPU-Operator installation on OpenShift clusters                                                                    | [Details](#test_openshift_operator_install)              |
| `openshift/test_openshift_gpu_operator.py` | `test_openshift_security_context`              | Tests security context constraints for GPU workloads on OpenShift                                                        | [Details](#test_openshift_security_context)              |
| `openshift/test_openshift_gpu_operator.py` | `test_openshift_route_creation`                | Tests creation of OpenShift routes for GPU services                                                                      | [Details](#test_openshift_route_creation)                |
| `unit/test_spec_util.py`                   | `test_yaml_parsing`                            | Unit tests for YAML configuration parsing utilities                                                                      | [Details](#test_yaml_parsing)                            |
| `unit/test_spec_util.py`                   | `test_image_manifest_processing`               | Unit tests for image manifest processing and transformation                                                              | [Details](#test_image_manifest_processing)               |
| `unit/test_spec_util.py`                   | `test_helm_values_generation`                  | Unit tests for Helm values.yaml generation from manifests                                                                | [Details](#test_helm_values_generation)                  |
| `unit/test_k8_util.py`                     | `test_kubernetes_api_calls`                    | Unit tests for Kubernetes API interaction utilities                                                                      | [Details](#test_kubernetes_api_calls)                    |
| `unit/test_k8_util.py`                     | `test_kubectl_command_execution`               | Unit tests for kubectl command execution wrappers                                                                        | [Details](#test_kubectl_command_execution)               |
| `unit/test_k8_util.py`                     | `test_resource_validation`                     | Unit tests for Kubernetes resource validation functions                                                                  | [Details](#test_resource_validation)                     |
| `integration/test_end_to_end.py`           | `test_complete_gpu_workflow`                   | End-to-end test covering full GPU-Operator deployment and GPU workload execution                                         | [Details](#test_complete_gpu_workflow)                   |
| `integration/test_end_to_end.py`           | `test_multi_node_gpu_scheduling`               | Tests GPU workload scheduling across multiple nodes                                                                      | [Details](#test_multi_node_gpu_scheduling)               |
| `integration/test_end_to_end.py`           | `test_gpu_workload_lifecycle`                  | Tests complete lifecycle of GPU workloads from creation to cleanup                                                       | [Details](#test_gpu_workload_lifecycle)                  |

---

## Kubernetes Tests (`k8/`)

### GPU Operator Core Tests (`k8/test_gpu_operator.py`)

#### `test_gpu_operator_install`
**Description**: Tests the installation of GPU-Operator using Helm charts with various configurations and validates successful deployment

**Test Steps**:
1. Create target namespace if not exists
2. Apply registry secrets for image pulling
3. Generate Helm values.yaml from image manifest
4. Execute `helm install` with generated configuration
5. Wait for all operator pods to reach Running state
6. Verify CRD installation (DeviceConfig, etc.)
7. Check operator deployment status and readiness
8. Validate service accounts and RBAC permissions
9. Confirm operator logs show no errors

**Validation**:
- All operator components are deployed successfully
- CRDs are created and available
- Operator pods are in Running state
- No error messages in operator logs

#### `test_gpu_operator_upgrade`
**Description**: Tests upgrading GPU-Operator from one version to another and verifies all components are updated correctly

**Test Steps**:
1. Install initial version of GPU-Operator
2. Verify initial installation is healthy
3. Update Helm values with new image versions
4. Execute `helm upgrade` command
5. Monitor rolling update of operator components
6. Wait for all pods to reach Running state with new images
7. Verify CRD schema updates if applicable
8. Check backward compatibility with existing resources
9. Validate upgraded operator functionality

**Validation**:
- All components successfully upgraded to new version
- No data loss or corruption during upgrade
- Existing configurations remain intact
- New features/fixes are available

#### `test_gpu_operator_uninstall`
**Description**: Tests the complete removal of GPU-Operator and validates cleanup of all resources

**Test Steps**:
1. Execute `helm uninstall` command
2. Wait for all operator pods to terminate
3. Verify removal of deployments and services
4. Check that CRDs are handled according to policy
5. Validate cleanup of config maps and secrets
6. Ensure service accounts are removed
7. Check for any remaining resources or finalizers
8. Verify namespace can be deleted if empty

**Validation**:
- All operator resources are completely removed
- No orphaned resources remain in cluster
- CRDs follow configured retention policy
- Namespace is clean or safely deletable

### Configuration Manager Tests (`k8/test_config_manager.py`)

#### `test_config_manager_deployment`
**Description**: Tests deployment of device configuration manager and validates its functionality

**Test Steps**:
1. Deploy DeviceConfig custom resource
2. Wait for config manager pod deployment
3. Verify config manager discovers GPU nodes
4. Check configuration application to target nodes
5. Monitor config manager logs for success
6. Validate device configuration persistence
7. Test configuration validation and error handling

**Validation**:
- Config manager pod is running and healthy
- Device configurations are applied successfully
- Target nodes reflect configuration changes
- Error handling works correctly for invalid configs

#### `test_config_manager_config_update`
**Description**: Tests updating device configuration through config manager and verifies changes are applied

**Test Steps**:
1. Deploy initial DeviceConfig
2. Wait for configuration to be applied
3. Update DeviceConfig with new parameters
4. Monitor config manager for change detection
5. Verify rolling update of affected nodes
6. Check new configuration is active
7. Validate no service interruption
8. Test rollback capability if update fails

**Validation**:
- Configuration changes are detected and applied
- Node configurations updated without data loss
- Services remain available during update
- Rollback works if needed

#### `test_config_manager_rollback`
**Description**: Tests rollback functionality of config manager when configuration updates fail

**Test Steps**:
1. Deploy working DeviceConfig
2. Apply intentionally invalid configuration
3. Monitor config manager error detection
4. Verify automatic rollback to previous config
5. Check system stability after rollback
6. Test manual rollback procedures
7. Validate error reporting and logging

**Validation**:
- Failed configurations are detected
- Automatic rollback restores service
- System returns to stable state
- Error details are properly logged

### Metrics Exporter Tests (`k8/test_metrics_exporter.py`)

#### `test_metrics_exporter_deployment`
**Description**: Tests deployment of metrics exporter component and validates metric collection

**Test Steps**:
1. Enable metrics exporter in DeviceConfig
2. Wait for exporter pods on GPU nodes
3. Verify metric collection endpoints
4. Check GPU metric availability
5. Validate metric format and content
6. Test metric endpoint security
7. Monitor exporter performance impact

**Validation**:
- Exporter pods deployed on all GPU nodes
- Metrics endpoints are accessible
- GPU metrics are collected and formatted correctly
- Minimal performance impact on nodes

#### `test_metrics_exporter_prometheus_integration`
**Description**: Tests integration with Prometheus for GPU metrics scraping

**Test Steps**:
1. Deploy Prometheus server or use existing
2. Configure Prometheus to scrape GPU metrics
3. Verify service discovery of exporter endpoints
4. Check metric ingestion into Prometheus
5. Validate metric labels and metadata
6. Test metric retention and storage
7. Verify query functionality

**Validation**:
- Prometheus successfully discovers exporters
- GPU metrics are ingested and stored
- Queries return expected metric data
- Metric metadata is correct

#### `test_metrics_exporter_service_monitor`
**Description**: Tests ServiceMonitor creation for Prometheus operator integration

**Test Steps**:
1. Deploy Prometheus Operator
2. Create ServiceMonitor for GPU metrics
3. Verify operator picks up ServiceMonitor
4. Check automatic target discovery
5. Validate scraping configuration
6. Test metric collection through operator
7. Verify integration with existing monitoring

**Validation**:
- ServiceMonitor is created and recognized
- Prometheus Operator configures scraping
- Metrics are collected via operator
- Integration works with existing setup

### Node Labeller Tests (`k8/test_node_labeller.py`)

#### `test_node_labeller_gpu_detection`
**Description**: Tests automatic detection and labeling of GPU nodes in the cluster

**Test Steps**:
1. Deploy node labeller component
2. Wait for labeller pods on all nodes
3. Check GPU hardware detection
4. Verify node label application
5. Test detection of different GPU types
6. Validate label format and content
7. Monitor labelling performance

**Validation**:
- All GPU nodes are detected
- Appropriate labels are applied
- GPU types are correctly identified
- Labels follow expected format

#### `test_node_labeller_label_accuracy`
**Description**: Tests accuracy of GPU-related node labels applied by the node labeller

**Test Steps**:
1. Inventory actual GPU hardware on nodes
2. Deploy node labeller
3. Compare detected vs actual GPU specs
4. Verify label values match hardware
5. Test edge cases (no GPU, multiple GPUs)
6. Validate label updates on hardware changes
7. Check label persistence across reboots

**Validation**:
- Labels accurately reflect hardware
- All GPU properties are captured
- Edge cases handled correctly
- Labels persist appropriately

#### `test_node_labeller_multi_gpu_support`
**Description**: Tests node labelling for nodes with multiple GPU types

**Test Steps**:
1. Identify nodes with multiple GPU types
2. Deploy node labeller
3. Verify detection of all GPU types
4. Check label structure for multiple GPUs
5. Test scheduling based on multi-GPU labels
6. Validate resource reporting accuracy
7. Monitor performance with complex setups

**Validation**:
- All GPU types detected on multi-GPU nodes
- Labels correctly represent multiple devices
- Scheduling works with complex configurations
- Resource reporting is accurate

### Driver and Device Plugin Tests (`k8/test_driver_deviceplugin.py`)

#### `test_driver_installation`
**Description**: Tests GPU driver installation on worker nodes

**Test Steps**:
1. Enable driver installation in DeviceConfig
2. Monitor driver container deployment
3. Check driver compilation/installation process
4. Verify kernel module loading
5. Test driver functionality
6. Validate driver version and compatibility
7. Monitor installation logs for errors

**Validation**:
- Driver installs successfully on all nodes
- Kernel modules load correctly
- GPU devices are accessible
- Driver version matches requirements

#### `test_device_plugin_discovery`
**Description**: Tests GPU device discovery by the Kubernetes device plugin

**Test Steps**:
1. Deploy device plugin daemonset
2. Wait for plugin registration with kubelet
3. Check device discovery on each node
4. Verify resource advertisement to API server
5. Test plugin health checking
6. Validate device isolation
7. Monitor plugin logs

**Validation**:
- Device plugin registers with kubelet
- GPU resources are advertised correctly
- Device isolation works properly
- Plugin remains healthy

#### `test_device_plugin_allocation`
**Description**: Tests GPU resource allocation to pods through device plugin

**Test Steps**:
1. Create pod requesting GPU resources
2. Monitor scheduling to GPU-enabled node
3. Check device allocation by plugin
4. Verify GPU access from within pod
5. Test resource cleanup after pod termination
6. Validate concurrent allocations
7. Test allocation failures and error handling

**Validation**:
- Pods are scheduled to appropriate nodes
- GPU devices are allocated correctly
- Resources are accessible from pods
- Cleanup happens properly

#### `test_driver_upgrade`
**Description**: Tests GPU driver upgrade functionality and node rebooting

**Test Steps**:
1. Install initial driver version
2. Update DeviceConfig with new driver version
3. Monitor driver upgrade process
4. Check node drain and reboot procedures
5. Verify new driver installation
6. Test workload rescheduling
7. Validate upgrade rollback if needed

**Validation**:
- Driver upgrades complete successfully
- Node reboots happen safely
- Workloads are rescheduled appropriately
- System remains stable throughout process

#### `test_multiple_gpu_allocation`
**Description**: Tests allocation of multiple GPU resources to a single pod

**Test Steps**:
1. Create pod requesting multiple GPUs
2. Monitor scheduling constraints
3. Check allocation of multiple devices
4. Verify all GPUs accessible from pod
5. Test resource limits and requests
6. Validate device isolation between pods
7. Monitor resource utilization

**Validation**:
- Multiple GPUs allocated to single pod
- All devices are accessible
- Resource isolation maintained
- Performance is as expected

### Test Runner Tests (`k8/test_test_runner.py`)

#### `test_runner_deployment`
**Description**: Tests deployment of test runner component for GPU validation

**Test Steps**:
1. Enable test runner in DeviceConfig
2. Deploy test runner pods on GPU nodes
3. Check test runner initialization
4. Verify GPU access from test containers
5. Monitor test execution logs
6. Test various GPU workload types
7. Validate test result reporting

**Validation**:
- Test runner deploys successfully
- GPU access is working
- Tests execute and report results
- Test infrastructure is stable

#### `test_runner_gpu_validation`
**Description**: Tests GPU functionality validation through test runner workloads

**Test Steps**:
1. Deploy GPU validation workloads
2. Execute basic GPU compute tests
3. Run memory allocation tests
4. Test GPU-CPU communication
5. Validate performance benchmarks
6. Check error detection and reporting
7. Monitor resource utilization during tests

**Validation**:
- All GPU functionality tests pass
- Performance meets expectations
- Error detection works correctly
- Resource usage is within limits

#### `test_runner_stress_testing`
**Description**: Tests GPU stress testing capabilities through test runner

**Test Steps**:
1. Deploy stress testing workloads
2. Execute high-intensity GPU operations
3. Monitor thermal and power management
4. Test system stability under load
5. Validate error recovery mechanisms
6. Check performance degradation patterns
7. Test long-running stress scenarios

**Validation**:
- System remains stable under stress
- Thermal management works correctly
- Performance degradation is acceptable
- Error recovery mechanisms function

#### `test_runner_benchmark_execution`
**Description**: Tests execution of GPU benchmarks through test runner

**Test Steps**:
1. Deploy benchmark workloads
2. Execute standard GPU benchmarks
3. Collect performance metrics
4. Compare results against baselines
5. Validate benchmark repeatability
6. Test different benchmark types
7. Generate performance reports

**Validation**:
- Benchmarks execute successfully
- Results meet performance baselines
- Measurements are repeatable
- Reports are generated correctly

## OpenShift Tests (`openshift/`)

### OpenShift GPU Operator Tests (`openshift/test_openshift_gpu_operator.py`)

#### `test_openshift_operator_install`
**Description**: Tests GPU-Operator installation on OpenShift clusters

**Test Steps**:
1. Verify OpenShift cluster prerequisites
2. Create required SecurityContextConstraints
3. Deploy GPU Operator via OLM or Helm
4. Check operator pod deployment with OpenShift restrictions
5. Verify integration with OpenShift monitoring
6. Test operator permissions and RBAC
7. Validate OpenShift-specific configurations

**Validation**:
- Operator installs successfully on OpenShift
- SecurityContextConstraints are properly configured
- Integration with OpenShift ecosystem works
- All security requirements are met

#### `test_openshift_security_context`
**Description**: Tests security context constraints for GPU workloads on OpenShift

**Test Steps**:
1. Create SecurityContextConstraints for GPU workloads
2. Deploy GPU workload pods
3. Verify SCC enforcement and compliance
4. Test privileged container requirements
5. Check device access permissions
6. Validate security policies
7. Test workload isolation

**Validation**:
- SecurityContextConstraints allow GPU access
- Workloads run within security boundaries
- Device permissions are correctly granted
- Security isolation is maintained

#### `test_openshift_route_creation`
**Description**: Tests creation of OpenShift routes for GPU services

**Test Steps**:
1. Deploy GPU services requiring external access
2. Create OpenShift Routes for services
3. Test external connectivity through routes
4. Verify SSL/TLS termination
5. Check route security configurations
6. Test load balancing and failover
7. Validate route monitoring and logging

**Validation**:
- Routes are created successfully
- External access works through routes
- Security configurations are proper
- Monitoring and logging function correctly

## Unit Tests (`unit/`)

### Spec Utility Tests (`unit/test_spec_util.py`)

#### `test_yaml_parsing`
**Description**: Unit tests for YAML configuration parsing utilities

**Test Steps**:
1. Test parsing of valid YAML configurations
2. Verify handling of invalid YAML syntax
3. Check schema validation functionality
4. Test nested configuration parsing
5. Verify error reporting for malformed files
6. Test edge cases and boundary conditions
7. Validate performance with large files

**Validation**:
- Valid YAML files parse correctly
- Invalid syntax is detected and reported
- Schema validation works properly
- Performance is acceptable

#### `test_image_manifest_processing`
**Description**: Unit tests for image manifest processing and transformation

**Test Steps**:
1. Test parsing of image manifest files
2. Verify image URL transformation
3. Check registry substitution logic
4. Test secret association with images
5. Verify manifest validation
6. Test error handling for invalid manifests
7. Check output format consistency

**Validation**:
- Manifests are processed correctly
- Image transformations work as expected
- Error handling is robust
- Output formats are consistent

#### `test_helm_values_generation`
**Description**: Unit tests for Helm values.yaml generation from manifests

**Test Steps**:
1. Test values generation from valid manifests
2. Verify template substitution logic
3. Check default value handling
4. Test nested value structures
5. Verify output YAML validity
6. Test error handling for invalid inputs
7. Check values file completeness

**Validation**:
- Values files are generated correctly
- Template substitution works properly
- Generated YAML is valid
- All required values are present

### K8s Utility Tests (`unit/test_k8_util.py`)

#### `test_kubernetes_api_calls`
**Description**: Unit tests for Kubernetes API interaction utilities

**Test Steps**:
1. Test API client initialization
2. Verify resource creation operations
3. Check resource update functionality
4. Test resource deletion operations
5. Verify error handling for API failures
6. Test authentication and authorization
7. Check API version compatibility

**Validation**:
- API calls execute successfully
- Error handling is robust
- Authentication works correctly
- API compatibility is maintained

#### `test_kubectl_command_execution`
**Description**: Unit tests for kubectl command execution wrappers

**Test Steps**:
1. Test command construction logic
2. Verify command execution functionality
3. Check output parsing and formatting
4. Test error detection and reporting
5. Verify command timeout handling
6. Test authentication parameter passing
7. Check command logging and debugging

**Validation**:
- Commands are constructed correctly
- Execution works reliably
- Output is parsed properly
- Error handling is comprehensive

#### `test_resource_validation`
**Description**: Unit tests for Kubernetes resource validation functions

**Test Steps**:
1. Test resource schema validation
2. Verify field requirement checking
3. Check data type validation
4. Test constraint enforcement
5. Verify cross-field validation logic
6. Test custom validation rules
7. Check validation error reporting

**Validation**:
- Resource schemas are validated correctly
- Field requirements are enforced
- Data types are checked properly
- Error messages are clear and helpful

## Integration Tests (`integration/`)

### End-to-End Tests (`integration/test_end_to_end.py`)

#### `test_complete_gpu_workflow`
**Description**: End-to-end test covering full GPU-Operator deployment and GPU workload execution

**Test Steps**:
1. Deploy fresh Kubernetes cluster or use existing
2. Install GPU-Operator with all components
3. Wait for driver installation and node preparation
4. Deploy sample GPU workloads
5. Verify workload scheduling and execution
6. Monitor GPU resource utilization
7. Test workload scaling and lifecycle management
8. Perform cleanup and resource verification

**Validation**:
- Complete workflow executes successfully
- All components integrate properly
- GPU workloads run as expected
- Resource cleanup is complete

#### `test_multi_node_gpu_scheduling`
**Description**: Tests GPU workload scheduling across multiple nodes

**Test Steps**:
1. Identify multi-node cluster with GPUs
2. Deploy workloads with different GPU requirements
3. Verify scheduler places workloads appropriately
4. Test node affinity and anti-affinity rules
5. Check resource availability tracking
6. Test workload migration and rescheduling
7. Verify load balancing across nodes
8. Monitor cluster resource utilization

**Validation**:
- Workloads are scheduled correctly across nodes
- Resource constraints are respected
- Load balancing works effectively
- Migration and rescheduling function properly

#### `test_gpu_workload_lifecycle`
**Description**: Tests complete lifecycle of GPU workloads from creation to cleanup

**Test Steps**:
1. Create GPU workload with specific requirements
2. Monitor workload startup and initialization
3. Verify GPU resource allocation and access
4. Test workload execution and performance
5. Monitor workload health and status
6. Test workload scaling operations
7. Perform workload termination
8. Verify resource cleanup and deallocation

**Validation**:
- Workload lifecycle completes successfully
- Resources are allocated and deallocated properly
- Performance meets expectations
- Cleanup is thorough and complete

## Test Categories Summary

### Kubernetes Tests (`k8/`)
- **GPU Operator Core**: Installation, upgrade, and uninstall of the main operator
- **Configuration Management**: Device configuration and management testing  
- **Metrics & Monitoring**: GPU metrics collection and Prometheus integration
- **Node Management**: GPU node detection and labeling
- **Driver & Device Plugin**: GPU driver installation and device allocation
- **Test Runner**: GPU validation and benchmarking

### OpenShift Tests (`openshift/`)
- **OpenShift Integration**: OpenShift-specific GPU operator functionality
- **Security**: Security context constraints and permissions
- **Networking**: Route creation and service exposure

### Unit Tests (`unit/`)
- **Utilities**: Core utility functions for YAML processing, manifest handling
- **Kubernetes Integration**: API calls and kubectl command wrappers

### Integration Tests (`integration/`)
- **End-to-End Workflows**: Complete GPU deployment and workload testing
- **Multi-Node Scenarios**: Cross-node GPU scheduling and management

## Usage

Run specific test categories:
```bash
# All Kubernetes tests
pytest tests/pytests/k8/

# Specific component tests
pytest tests/pytests/k8/test_gpu_operator.py

# OpenShift tests
pytest tests/pytests/openshift/

# Unit tests
pytest tests/pytests/unit/

# Integration tests
pytest tests/pytests/integration/
```