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

#pytestmark = pytest.mark.skip("debugging")
Logger = logging.getLogger("k8.test_node_problem_detector")

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
        cmd = ["/var/lib/amd-metrics-exporter/amdgpuhealth"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"Unable to run /var/lib/amd-metrics-exporter/amdgpuhealth on {node_name}")
        K8Helper.triage(environment, resp_stdout != None, f"Error: Command output is None")
        Logger.debug(f"Cmd:{cmd}, Response:\n{resp_stdout}")

@pytest.mark.parametrize("metric_to_test, threshold", [
    ("amd_gpu_violation_gfx_clock_below_host_limit_power_percentage", 100)
])
def test_exporter_amdgpuhealth_counter(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset, metric_to_test, threshold, environment):
    """
    Test NPD custom plugin integration with amdgpuhealth for counter metrics.

    Test flow:
    1. Clean up any existing NPD custom plugin configuration
    2. Deploy NPD with amdgpuhealth custom plugin configured for counter-metric query
    3. Verify deployment succeeds

    The custom plugin configures NPD to periodically run:
        amdgpuhealth query counter-metric -m=<metric_to_test> -t=<threshold>

    Counter metrics track cumulative values (e.g., error counts, violations).
    If the metric value exceeds the threshold, NPD sets node condition to indicate a problem.

    Parameters:
        metric_to_test: Counter metric name to query (e.g., violation counts)
        threshold: Value above which the metric indicates a problem

    Dependencies:
        - deviceconfig_install: Provides metrics-exporter with amdgpuhealth binary
        - deploy_npd_daemonset: Deploys base NPD DaemonSet with RBAC

    TODO: Add validation for:
        - NPD DaemonSet rollout status
        - Node condition updates when threshold exceeded

    Cleanup:
        Removes custom plugin configuration from NPD
    """
    global Logger

    def _cleanup_npd_config():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)

    request.addfinalizer(_cleanup_npd_config)

    _cleanup_npd_config()

    ret_code = npd_util.deploy_npd_amdgpuhealth_plugin(environment, "counter-metric", metric_to_test, threshold)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to setup amdgpuhealth custom-plugin for npd")
    # TODO: Check for daemon-set rollout, node-condition

@pytest.mark.parametrize("metric_to_test, threshold", [
    ("amd_gpu_ecc_correct_athub", 1)
])
def test_exporter_amdgpuhealth_gauge(request, gpu_cluster, deviceconfig_install, deploy_npd_daemonset, metric_to_test, threshold, environment):
    """
    Test NPD custom plugin integration with amdgpuhealth for gauge metrics.

    Test flow:
    1. Clean up any existing NPD custom plugin configuration
    2. Deploy NPD with amdgpuhealth custom plugin configured for gauge-metric query
    3. Verify deployment succeeds

    The custom plugin configures NPD to periodically run:
        amdgpuhealth query gauge-metric -m=<metric_to_test> -t=<threshold>

    Gauge metrics represent instantaneous values (e.g., temperature, ECC errors).
    If the metric value exceeds the threshold, NPD sets node condition to indicate a problem.
    For time-series analysis over a duration, Prometheus integration is required.

    Parameters:
        metric_to_test: Gauge metric name to query (e.g., ECC error counts)
        threshold: Value above which the metric indicates a problem

    Dependencies:
        - deviceconfig_install: Provides metrics-exporter with amdgpuhealth binary
        - deploy_npd_daemonset: Deploys base NPD DaemonSet with RBAC

    TODO: Add validation for:
        - NPD DaemonSet rollout status
        - Node condition updates when threshold exceeded

    Cleanup:
        Removes custom plugin configuration from NPD
    """
    global Logger

    def _cleanup_npd_config():
        npd_util.remove_npd_amdgpuhealth_plugin(environment)

    request.addfinalizer(_cleanup_npd_config)

    _cleanup_npd_config()
    ret_code = npd_util.deploy_npd_amdgpuhealth_plugin(environment, "gauge-metric", metric_to_test, threshold)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to setup amdgpuhealth custom-plugin for npd")
    # TODO: Check for daemon-set rollout, node-condition
