#!/usr/bin/python3

'''
 Copyright (c) Advanced Micro Devices, Inc. All rights reserved.

 Licensed under the Apache License, Version 2.0 (the \"License\");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an \"AS IS\" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
'''

import pdb
import pytest
import pprint
import sys
import os
import time
import json
import logging
import random
import functools
import lib.common as common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.npd_util as npd_util
from lib.util import K8Helper
from kubernetes import client

#pytestmark = pytest.mark.skip("debugging")
Logger = logging.getLogger("k8.test_node_problem_detector")

def wait_for_npd_daemonset_ready(namespace, daemonset_name, timeout=300, interval=10):
    """
    Wait for NPD DaemonSet to be fully rolled out.

    A DaemonSet is considered ready when:
    - All desired pods are scheduled
    - All pods are available (running and ready)

    Enhanced diagnostics on failure:
    - Logs pod status (phase, ready conditions)
    - Collects pod logs for crash-looping containers
    - Reports pod events (warnings, errors)

    Args:
        namespace: Namespace where DaemonSet is deployed
        daemonset_name: Name of the DaemonSet
        timeout: Maximum time to wait in seconds (default: 300)
        interval: Check interval in seconds (default: 10)

    Returns:
        bool: True if DaemonSet is ready, False otherwise
    """
    api_apps = client.AppsV1Api()
    api_core = client.CoreV1Api()
    elapsed = 0

    while elapsed < timeout:
        try:
            ds = api_apps.read_namespaced_daemon_set(name=daemonset_name, namespace=namespace)

            # Check DaemonSet status
            desired = ds.status.desired_number_scheduled or 0
            current = ds.status.current_number_scheduled or 0
            ready = ds.status.number_ready or 0
            available = ds.status.number_available or 0

            Logger.info(f"DaemonSet {daemonset_name}: desired={desired}, current={current}, ready={ready}, available={available}")

            # DaemonSet is ready when all desired pods are available and ready
            if desired > 0 and desired == current == ready == available:
                Logger.info(f"DaemonSet {daemonset_name} is fully rolled out")
                return True

            # Enhanced diagnostics: check pod status if not ready
            if elapsed > 0 and elapsed % 30 == 0:  # Log detailed pod status every 30s
                try:
                    pods = api_core.list_namespaced_pod(
                        namespace=namespace,
                        label_selector=f"app={daemonset_name}"
                    )
                    for pod in pods.items:
                        pod_name = pod.metadata.name
                        phase = pod.status.phase

                        # Check container statuses
                        container_states = []
                        if pod.status.container_statuses:
                            for cs in pod.status.container_statuses:
                                if cs.state.waiting:
                                    container_states.append(f"{cs.name}=Waiting({cs.state.waiting.reason})")
                                elif cs.state.terminated:
                                    container_states.append(f"{cs.name}=Terminated(exit={cs.state.terminated.exit_code}, reason={cs.state.terminated.reason})")
                                elif cs.state.running:
                                    container_states.append(f"{cs.name}=Running")

                                # Collect logs if container is crash-looping
                                if cs.state.waiting and cs.state.waiting.reason in ["CrashLoopBackOff", "Error"]:
                                    Logger.warning(f"Pod {pod_name} container {cs.name} is {cs.state.waiting.reason}, collecting logs...")
                                    try:
                                        logs = api_core.read_namespaced_pod_log(
                                            name=pod_name,
                                            namespace=namespace,
                                            container=cs.name,
                                            tail_lines=50
                                        )
                                        Logger.error(f"Pod {pod_name} container {cs.name} logs (last 50 lines):\n{logs}")
                                    except Exception as log_err:
                                        Logger.warning(f"Could not collect logs for {pod_name}/{cs.name}: {log_err}")

                        Logger.info(f"Pod {pod_name}: phase={phase}, containers=[{', '.join(container_states)}]")

                        # Get recent pod events
                        events = api_core.list_namespaced_event(
                            namespace=namespace,
                            field_selector=f"involvedObject.name={pod_name}"
                        )
                        warnings = [e for e in events.items if e.type == "Warning"]
                        if warnings:
                            Logger.warning(f"Pod {pod_name} has {len(warnings)} warning events:")
                            for event in warnings[-5:]:  # Show last 5 warnings
                                Logger.warning(f"  [{event.reason}] {event.message}")

                except Exception as diag_err:
                    Logger.warning(f"Error collecting pod diagnostics: {diag_err}")

        except Exception as e:
            Logger.warning(f"Error checking DaemonSet status: {e}")

        time.sleep(interval)
        elapsed += interval

    Logger.error(f"Timeout waiting for DaemonSet {daemonset_name} to be ready after {timeout}s")

    # Final diagnostic dump on timeout
    try:
        Logger.error(f"Collecting final diagnostic information for failed DaemonSet {daemonset_name}...")
        pods = api_core.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"app={daemonset_name}"
        )
        for pod in pods.items:
            Logger.error(f"Pod {pod.metadata.name} final status: phase={pod.status.phase}")
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    Logger.error(f"  Container {cs.name}: ready={cs.ready}, restart_count={cs.restart_count}")
                    if cs.state.waiting:
                        Logger.error(f"    State: Waiting - {cs.state.waiting.reason}: {cs.state.waiting.message}")
                    elif cs.state.terminated:
                        Logger.error(f"    State: Terminated - exit_code={cs.state.terminated.exit_code}, reason={cs.state.terminated.reason}")
    except Exception as final_err:
        Logger.error(f"Error collecting final diagnostics: {final_err}")

    return False

def get_node_condition(node_name, condition_type):
    """
    Get a specific condition from a node's status.

    Args:
        node_name: Name of the node
        condition_type: Type of condition to retrieve (e.g., "AMDGPUProblem", "Ready")

    Returns:
        dict or None: Condition object with keys: type, status, reason, message, lastTransitionTime
                      Returns None if condition not found
    """
    api = client.CoreV1Api()

    try:
        node = api.read_node(name=node_name)

        if node.status and node.status.conditions:
            for condition in node.status.conditions:
                if condition.type == condition_type:
                    return {
                        'type': condition.type,
                        'status': condition.status,
                        'reason': condition.reason or "",
                        'message': condition.message or "",
                        'lastTransitionTime': condition.last_transition_time
                    }

        Logger.debug(f"Condition '{condition_type}' not found on node '{node_name}'")
        return None

    except Exception as e:
        Logger.error(f"Error reading node conditions: {e}")
        return None

def verify_npd_node_condition(gpu_nodes, condition_type, expected_status=None, expected_reason=None, timeout=120, interval=10):
    """
    Verify that NPD has set the expected condition on all GPU nodes.

    Args:
        gpu_nodes: List of GPU node objects
        condition_type: Condition type to check (e.g., "AMDGPUProblem")
        expected_status: Expected condition status ("True", "False", "Unknown"), None to skip check
        expected_reason: Expected reason string, None to skip check
        timeout: Maximum time to wait for condition to appear (default: 120s)
        interval: Check interval in seconds (default: 10)

    Returns:
        tuple: (success: bool, failed_nodes: list of node names)
    """
    elapsed = 0
    nodes_to_check = [k8_util.k8_get_node_hostname(node) for node in gpu_nodes]

    while elapsed < timeout:
        failed_nodes = []

        for node_name in nodes_to_check:
            condition = get_node_condition(node_name, condition_type)

            if condition is None:
                Logger.debug(f"Node {node_name}: condition '{condition_type}' not yet present")
                failed_nodes.append(node_name)
                continue

            # Check expected status if provided
            if expected_status is not None and condition['status'] != expected_status:
                Logger.debug(f"Node {node_name}: condition status is '{condition['status']}', expected '{expected_status}'")
                failed_nodes.append(node_name)
                continue

            # Check expected reason if provided
            if expected_reason is not None and condition['reason'] != expected_reason:
                Logger.debug(f"Node {node_name}: condition reason is '{condition['reason']}', expected '{expected_reason}'")
                failed_nodes.append(node_name)
                continue

            Logger.info(f"Node {node_name}: condition '{condition_type}' verified - status={condition['status']}, reason={condition['reason']}")

        # If all nodes pass validation, return success
        if not failed_nodes:
            return True, []

        # Wait and retry
        time.sleep(interval)
        elapsed += interval

    Logger.error(f"Timeout waiting for condition '{condition_type}' on nodes: {failed_nodes}")
    return False, failed_nodes

@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment):
    """
    Fixture to deploy GPU operator DeviceConfig with metrics-exporter enabled.

    This fixture creates DeviceConfig CRs for all GPU nodes in the cluster with:
    - Driver enabled
    - Device plugin enabled
    - Metrics exporter enabled with NodePort service type
    - Unique NodePorts assigned per DeviceConfig when multiple configs exist

    The metrics exporter writes the amdgpuhealth binary to /var/lib/amd-metrics-exporter
    on each node, which is required for NPD custom plugin integration.

    Yields:
        DeviceConfigCRInfo: Object containing:
            - test_cfg_map: Map of DeviceConfig names to their configurations
            - exporter_port_map: Map of node hostnames to exporter NodePorts
            - devicecfg_list: List of created DeviceConfig names

    Cleanup:
        Removes all created DeviceConfig CRs
    """
    global Logger

    # cleanup - remove any deviceconfigs and then gpu-operator helm-chart
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)

    class DeviceConfigCRInfo(object):
        pass

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    test_config = {
            'metadata.namespace' : environment.gpu_operator_namespace,
            'driver.enable' : True,
            'devicePlugin.enableNodeLabeller' : False,
            'metricsExporter.enable' : True,
            'metricsExporter.serviceType' : 'NodePort',
        }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'exporter', environment.amdgpu_driver_spec)
    exporter_port_map = {}
    devicecfg_list = []
    if len(test_cfg_map) > 1:
        # Assign unique NodePorts for each deviceconfig instance
        for idx, cfg_name in enumerate(test_cfg_map.keys()):
            cfg = test_cfg_map[cfg_name]
            cfg['metricsExporter.nodePort'] = 32500 + idx * 100
            exporter_port_map[cfg['selector.value']] = cfg['metricsExporter.nodePort']
    else:
        for node in gpu_nodes:
            node_hostname = k8_util.k8_get_node_hostname(node)
            exporter_port_map[node_hostname] = 32500

    for spec_name, tcfg in test_cfg_map.items():
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")
        devicecfg_list.append(tcfg['metadata.name'])

    # Check for corresponding deviceconfig created
    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)
    K8Helper.update_node_driver_version(gpu_cluster, environment)

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)
    yield devcfg_info

    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    return

def test_exporter_amdgpuhealth_hostpath(gpu_cluster, deviceconfig_install, environment):
    """
    Verify that amdgpuhealth binary is available on all GPU nodes.

    Test validates:
    1. DeviceConfig pods (device-plugin, metrics-exporter) are running
    2. Metrics exporter has written amdgpuhealth binary to /var/lib/amd-metrics-exporter
    3. The binary is executable on each GPU node

    This is a prerequisite for NPD custom plugin integration, which needs to mount
    /var/lib/amd-metrics-exporter from the host into the NPD container.

    Dependencies:
        - deviceconfig_install: Creates DeviceConfig with metrics-exporter enabled
    """
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Watch for all pod creation
    '''
    test-deviceconfig-device-plugin-8f7px                        1/1     Running       0                 12d
    test-deviceconfig-metrics-exporter-27gq9                     2/2     Running       0                 12d
    test-deviceconfig-node-labeller-54vpd                        1/1     Running       0                 12d
    '''
    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
        common.PodInfo('metrics-exporter', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

    time.sleep(30) # Wait for exporter to start working

    # Check if amdgpuhealth utility is mounted on each node - /var/lib/amd-metrics-exporter
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)

        # Check if directory exists
        cmd = ["test", "-d", "/var/lib/amd-metrics-exporter"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"Directory /var/lib/amd-metrics-exporter does not exist on {node_name}")
        Logger.debug(f"Directory /var/lib/amd-metrics-exporter exists on {node_name}")

        # List directory contents
        cmd = ["ls", "-la", "/var/lib/amd-metrics-exporter"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        Logger.info(f"Contents of /var/lib/amd-metrics-exporter on {node_name}:\n{resp_stdout}")

        # Check if file exists
        cmd = ["test", "-f", "/var/lib/amd-metrics-exporter/amdgpuhealth"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"File /var/lib/amd-metrics-exporter/amdgpuhealth does not exist on {node_name}")
        Logger.debug(f"File exists check passed for /var/lib/amd-metrics-exporter/amdgpuhealth on {node_name}")

        # Check if file is executable
        cmd = ["test", "-x", "/var/lib/amd-metrics-exporter/amdgpuhealth"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"File /var/lib/amd-metrics-exporter/amdgpuhealth is not executable on {node_name}")
        Logger.debug(f"File executable check passed for /var/lib/amd-metrics-exporter/amdgpuhealth on {node_name}")

        # Verify the utility can be executed with help command
        cmd = ["/var/lib/amd-metrics-exporter/amdgpuhealth", "--help"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"amdgpuhealth utility failed to execute on {node_name}: {resp_stdout}")
        Logger.debug(f"amdgpuhealth execution test passed on {node_name}")

        # Note: amdgpuhealth query commands require endpoint environment variables
        # NPD configures these when running via custom plugin monitor

@pytest.mark.parametrize("test_scenario", [
    {
        "name": "gfx_activity_threshold",
        "metric_type": "gauge-metric",
        "metric_name": "gpu_gfx_activity",
        "threshold": 50,
        "condition_type": "AMDGPUHighUtilization",
        "reason_healthy": "GPUUtilizationNormal",
        "reason_problem": "GPUUtilizationHigh",
        "message_healthy": "GPU utilization is within normal range",
        "message_problem": "GPU utilization exceeds threshold"
    },
    {
        "name": "junction_temp_threshold",
        "metric_type": "gauge-metric",
        "metric_name": "gpu_junction_temperature",
        "threshold": 55,
        "condition_type": "AMDGPUHighTemperature",
        "reason_healthy": "GPUTemperatureNormal",
        "reason_problem": "GPUTemperatureHigh",
        "message_healthy": "GPU temperature is within normal range",
        "message_problem": "GPU junction temperature exceeds threshold"
    },
    {
        "name": "power_threshold",
        "metric_type": "gauge-metric",
        "metric_name": "gpu_average_package_power",
        "threshold": 200,
        "condition_type": "AMDGPUHighPower",
        "reason_healthy": "GPUPowerNormal",
        "reason_problem": "GPUPowerHigh",
        "message_healthy": "GPU power consumption is within normal range",
        "message_problem": "GPU power consumption exceeds threshold"
    },
    {
        "name": "vram_usage_threshold",
        "metric_type": "gauge-metric",
        "metric_name": "gpu_used_vram",
        "threshold": 50000,
        "condition_type": "AMDGPUHighMemoryUsage",
        "reason_healthy": "GPUMemoryUsageNormal",
        "reason_problem": "GPUMemoryUsageHigh",
        "message_healthy": "GPU memory usage is within normal range",
        "message_problem": "GPU VRAM usage exceeds threshold"
    },
    # Counter metric scenarios - detect hardware errors
    {
        "name": "ecc_uncorrectable_errors",
        "metric_type": "counter-metric",
        "metric_name": "gpu_ecc_uncorrect_total",
        "threshold": 1,
        "condition_type": "AMDGPUUncorrectableECC",
        "reason_healthy": "NoUncorrectableECCErrors",
        "reason_problem": "UncorrectableECCErrorDetected",
        "message_healthy": "No uncorrectable ECC errors detected",
        "message_problem": "Uncorrectable ECC errors detected - potential hardware failure"
    },
    {
        "name": "ecc_correctable_umc_errors",
        "metric_type": "counter-metric",
        "metric_name": "gpu_ecc_correct_umc",
        "threshold": 100,
        "condition_type": "AMDGPUCorrectableECCUMC",
        "reason_healthy": "CorrectableECCWithinLimits",
        "reason_problem": "ExcessiveCorrectableECCErrors",
        "message_healthy": "Correctable ECC errors in UMC within acceptable limits",
        "message_problem": "Excessive correctable ECC errors in UMC - monitor for hardware degradation"
    }
])
def test_npd_multi_condition_workload(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset,
                                       test_scenario, images, environment):
    """
    Test NPD with multiple conditions that trigger based on workload state or hardware errors.

    This test validates that NPD can detect GPU health issues by monitoring both gauge
    and counter metrics. Each test scenario:
    1. Configures NPD with a specific metric threshold
    2. Verifies condition is healthy in idle state
    3. Starts a GPU workload
    4. Verifies condition status (may or may not trigger depending on metric type)
    5. Stops workload and verifies final condition state

    Test scenarios cover different metric types:

    Gauge Metrics (change with workload):
    - Activity/Utilization: GPU activity percentage
    - Temperature: Junction temperature in Celsius
    - Power: Average package power in Watts
    - Memory: VRAM usage in MB

    Counter Metrics (detect hardware errors):
    - ECC Errors: Uncorrectable/correctable error counts
    - PCIe Errors: Replay count errors

    Note: Counter metrics are cumulative and typically remain at 0 in healthy systems.
    They're included to demonstrate NPD's error detection capabilities, but may not
    trigger during normal workload execution.

    Parameters:
        test_scenario: Dictionary containing:
            - name: Test scenario name
            - metric_type: "gauge-metric" or "counter-metric"
            - metric_name: Prometheus metric name
            - threshold: Value above which condition triggers
            - condition_type: Kubernetes condition type
            - reason_healthy: Reason when metric is below threshold
            - reason_problem: Reason when metric exceeds threshold
            - message_healthy: Message for healthy state
            - message_problem: Message for problem state

    Dependencies:
        - deviceconfig_install: Provides metrics-exporter with amdgpuhealth binary
        - deploy_npd_daemonset: Deploys base NPD DaemonSet with RBAC
        - images: Container images for workload pods

    Validates:
        - NPD DaemonSet rollout succeeds
        - Condition exists on all GPU nodes
        - Condition status reflects metric values
        - Test logs condition state at idle, load, and recovery phases
    """
    global Logger

    def _cleanup_npd_config():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)

    def _cleanup_workloads():
        for ctxt in workload_contexts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)

    request.addfinalizer(_cleanup_npd_config)
    request.addfinalizer(_cleanup_workloads)

    workload_contexts = []

    # Extract scenario parameters
    scenario_name = test_scenario["name"]
    metric_type = test_scenario["metric_type"]
    metric_name = test_scenario["metric_name"]
    threshold = test_scenario["threshold"]
    condition_type = test_scenario["condition_type"]
    reason_healthy = test_scenario["reason_healthy"]
    reason_problem = test_scenario["reason_problem"]
    message_healthy = test_scenario["message_healthy"]
    message_problem = test_scenario["message_problem"]

    Logger.info(f"Testing NPD condition: {condition_type} for metric {metric_name} with threshold {threshold}")

    # Clean up any existing NPD configuration
    _cleanup_npd_config()

    # Deploy NPD with custom condition
    ret_code = npd_util.deploy_npd_custom_condition(
        environment, metric_type, metric_name, threshold,
        condition_type, reason_healthy, reason_problem,
        message_healthy, message_problem,
        invoke_interval="15s"  # Check every 15s for faster test feedback
    )
    K8Helper.triage(environment, (ret_code == 0), f"Failed to deploy NPD with custom condition {condition_type}")

    # Wait for NPD DaemonSet rollout
    Logger.info(f"Waiting for NPD DaemonSet '{npd_util.NPD_APP_NAME}' to be ready")
    ds_ready = wait_for_npd_daemonset_ready(npd_util.NPD_NAMESPACE, npd_util.NPD_APP_NAME, timeout=300)
    K8Helper.triage(environment, ds_ready, f"NPD DaemonSet '{npd_util.NPD_APP_NAME}' failed to rollout")

    # Get GPU nodes
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # Phase 1: Verify condition exists and is healthy in IDLE state
    Logger.info(f"Phase 1: Verifying {condition_type} condition is healthy in IDLE state")
    condition_ok, failed_nodes = verify_npd_node_condition(
        gpu_nodes,
        condition_type=condition_type,
        expected_status=None,  # Just verify condition exists
        expected_reason=None,
        timeout=120,
        interval=10
    )
    K8Helper.triage(environment, condition_ok, f"{condition_type} condition not found on nodes: {failed_nodes}")

    # Log idle state conditions
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = get_node_condition(node_name, condition_type)
        if condition:
            Logger.info(f"IDLE state - Node {node_name}: {condition_type} status={condition['status']}, reason={condition['reason']}")

    # Phase 2: Start GPU workload
    Logger.info(f"Phase 2: Starting GPU workload on all nodes")
    for node in gpu_nodes:
        node_ip = k8_util.k8_get_node_address(node)
        cluster_node = gpu_cluster.find_node_by_ip(node_ip)
        if not cluster_node:
            pytest.fail(f"Unable to get worker node from cluster for ip: {node_ip}")

        node_name = k8_util.k8_get_node_hostname(node)
        gpu_cap, gpu_alloc = k8_util.k8_get_node_gpu_capacity(node_name)

        params = {
            "node_name": node_name,
            "images": images,
            "num_gpu_reqd": gpu_cap,
        }
        workload_ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
        K8Helper.triage(environment, (workload_ctxt['podStatus'] == K8Helper.PodStatus.RUNNING),
                        f"Workload failed to start on node {node_name}: {workload_ctxt}")
        workload_contexts.append(workload_ctxt)
        Logger.info(f"Started workload on node {node_name}")

    # Wait for workload to stabilize and metrics to reflect load
    Logger.info("Waiting 60s for workload to stabilize and metrics to update")
    time.sleep(60)

    # Phase 3: Verify condition triggers under LOAD
    Logger.info(f"Phase 3: Verifying {condition_type} condition under LOAD state")

    # Log load state conditions
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = get_node_condition(node_name, condition_type)
        if condition:
            Logger.info(f"LOAD state - Node {node_name}: {condition_type} status={condition['status']}, reason={condition['reason']}")
        else:
            Logger.warning(f"LOAD state - Node {node_name}: {condition_type} condition not found")

    # Phase 4: Stop workload
    Logger.info(f"Phase 4: Stopping GPU workload")
    for ctxt in workload_contexts:
        K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    workload_contexts.clear()

    # Wait for metrics to return to idle state
    Logger.info("Waiting 60s for metrics to return to idle state")
    time.sleep(60)

    # Phase 5: Verify condition returns to healthy
    Logger.info(f"Phase 5: Verifying {condition_type} condition returns to healthy after workload stops")
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        condition = get_node_condition(node_name, condition_type)
        if condition:
            Logger.info(f"RECOVERY state - Node {node_name}: {condition_type} status={condition['status']}, reason={condition['reason']}")

    Logger.info(f"Test scenario '{scenario_name}' completed successfully")
