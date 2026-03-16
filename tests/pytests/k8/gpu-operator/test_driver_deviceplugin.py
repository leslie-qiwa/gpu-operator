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
import re
import time
import json
import logging
import random
import lib.k8_util as k8_util
import lib.helm_util as helm_util
import lib.amdgpu as amdgpu
import lib.common as common
import lib.spec_util as spec_util
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_driver_deviceplugin")

@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment):
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
    K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, len(gpu_nodes) > 0, "No nodes with AMD/GPU found in the cluster")

    test_config = {
            'metadata.namespace' : environment.gpu_operator_namespace,
            'driver.enable' : True,
            'driver.blacklist' : True,
            'devicePlugin.enableNodeLabeller' : True,
            'metricsExporter.enable' : False,
            'testRunner.enable' : False,
        }

    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'device-plugin', environment.amdgpu_driver_spec)
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
        K8Helper.triage(environment, ret_code == 0, f"Failed to create deviceconfig, stderr: {ret_stderr}")
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

    # cleanup - remove any deviceconfigs and then gpu-operator helm-chart
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)
    return

def test_gpu_capacity_status(deviceconfig_install, environment):
    global Logger

    failed_nodes = {}
    for _ in range(3):
        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")
        for node in gpu_nodes:
            node_name = k8_util.k8_get_node_hostname(node)
            capacity = node['status']['capacity']
            if 'amd.com/gpu' in capacity:
                if capacity["amd.com/gpu"] == "0":
                    failed_nodes[node_name] = f"zero value for 'amd.com/gpu' in capacity"
            else:
                failed_nodes[node_name] = f"Missing 'amd.com/gpu' in capacity"

            allocatable = node['status']['allocatable']
            if 'amd.com/gpu' in allocatable:
                if allocatable["amd.com/gpu"] == "0":
                    failed_nodes[node_name] = f"zero value for 'amd.com/gpu' in allocatable"
            else:
                failed_nodes[node_name] = f"Missing 'amd.com/gpu' in allocatable"

            if len(failed_nodes) > 0:
                Logger.warn(f"Failed nodes for capacity/allocatable information : {failed_nodes} - retry after delay")
                failed_nodes.clear()
                time.sleep(10)
            else:
                break

    K8Helper.triage(environment, len(failed_nodes) == 0,
                    f"Some of the node(s) have incorrect capacity/allocatable info {failed_nodes}")

def test_deviceplugin_label(deviceconfig_install, environment, inbox_driver_skip):
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")

    devicecfg_pods = [
        common.PodInfo('device-plugin', 1, 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")

    '''
    Check for following label:
    beta.kmm.node.kubernetes.io/version-device-plugin.<namespace>.deviceconfig-clusterwide: 6.2.4
    '''
    label_missing = set()
    pattern = r"beta\.kmm\.node\.kubernetes\.io/version-device-plugin." + environment.gpu_operator_namespace + r"\.(.*?)"
    for _ in range(4):
        label_missing.clear()
        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
        K8Helper.triage(environment, len(gpu_nodes), "No nodes with AMD/GPU found in the cluster")
        for node in gpu_nodes:
            label_found = False
            for label, _ in node['metadata']['labels'].items():
                if re.match(pattern, label):
                    label_found = True
                    break
            if not label_found:
                label_missing.add(node['metadata']['name'])
        if len(label_missing) > 0:
            Logger.warn(f"Still waiting for device-plugin label for nodes : {label_missing}")
            time.sleep(30)

    K8Helper.triage(environment, len(label_missing) == 0,
                    f"One or more nodes missing kmm.version-device-plugin label : {label_missing}")

def test_node_driver_version(gpu_cluster, deviceconfig_install, environment, inbox_driver_skip):
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")

    # check the version in the deviceconfig
    config_version = environment.amdgpu_driver_spec["default-version"]
    driver_version = amdgpu.get_matching_driver_version(config_version)
    K8Helper.check_deviceconfig_driver_version(gpu_cluster, config_version, environment)
    K8Helper.check_node_driver_version(gpu_cluster, config_version, driver_version, environment)

def test_driver_blacklist_file_present(gpu_cluster, deviceconfig_install, environment, inbox_driver_skip):
    global Logger
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")

    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.blacklist'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR")

    # check the worker node blacklist
    filename = "blacklist-amdgpu.conf"
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        cmd = ["ls", "-1", "/etc/modprobe.d/"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"error getting dir listings from {node_name} {node_name}")
        K8Helper.triage(environment, resp_stdout != None, f"Error: Command output is None")
        Logger.debug(f"Cmd:{cmd}, Response:\n{resp_stdout}")
        amdgpu_blacklist_file = list(filter(lambda line: filename in line, resp_stdout.split("\n")))

        K8Helper.triage(environment, len(amdgpu_blacklist_file) == 1,
                        f"blacklist file not found on node:{node_name} when blacklist is enabled", expected_to_fail = True)

def test_driver_blacklist_file_absent(gpu_cluster, deviceconfig_install, environment, inbox_driver_skip):
    global Logger
    # check the worker node blacklist
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")

    # Create an empty file 
    filename = "blacklist-amdgpu.conf"
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        cmd = ["touch", os.path.join("/etc/modprobe.d/", filename)]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"error getting dir listings from {node_name} {node_name}")

    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.blacklist'] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR")

    # check the worker node blacklist
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        cmd = ["ls", "-1", "/etc/modprobe.d/"]
        ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd)
        K8Helper.triage(environment, ret_code == 0, f"error getting dir listings from {node_name} {node_name}")
        K8Helper.triage(environment, resp_stdout != None, f"Error: Command output is None")
        Logger.debug(f"Cmd:{cmd}, Response:\n{resp_stdout}")
        amdgpu_blacklist_file = list(filter(lambda line: filename in line, resp_stdout.split("\n")))

        K8Helper.triage(environment, len(amdgpu_blacklist_file) == 0,
                        f"blacklist file is found {node_name} when blacklist is disabled", expected_to_fail = True)
    # Restore
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.blacklist'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR")

# Generate testcases for each supported driver-version
def pytest_generate_tests(metafunc):
    global Logger
    if 'upgrade_version' in metafunc.fixturenames:
        if metafunc.config.option.amdgpu_driver_spec:
            with open(metafunc.config.option.amdgpu_driver_spec, "r") as fp:
                driver_spec = json.load(fp)
        if driver_spec["driver-deployment"] == "inbox":
            return
        current_version = driver_spec["default-version"]
        driver_versions = []
        for ver in driver_spec.get('alternative-versions', []):
            if ver != current_version:
                driver_versions.append(ver)
        metafunc.parametrize('upgrade_version', driver_versions)

    if 'limited_upgrade_version' in metafunc.fixturenames:
        if metafunc.config.option.amdgpu_driver_spec:
            with open(metafunc.config.option.amdgpu_driver_spec, "r") as fp:
                driver_spec = json.load(fp)
        if driver_spec["driver-deployment"] == "inbox":
            return
        current_version = driver_spec["default-version"]
        driver_versions = []
        for ver in driver_spec.get('alternative-versions', []):
            if ver != current_version:
                driver_versions.append(ver)
        metafunc.parametrize('limited_upgrade_version', random.sample(driver_versions, 2))


def test_driver_upgrade_cycle(request, gpu_cluster, deviceconfig_install, environment, upgrade_version, inbox_driver_skip):
    global Logger
    if environment.gpu_operator_version in ["v1.0.0", "v1.1.0"]:
        pytest.skip(f"Skipping driver-upgrade testcase for current version {environment.gpu_operator_version}")
    if gpu_cluster.is_mini_kube():
        pytest.skip("Using mini-kube cluster - skip driver upgrade testcases")

    current_version = environment.amdgpu_driver_spec["default-version"]
    Logger.info(f"Upgrading cluster/gpu-nodes from {current_version} => {upgrade_version}")
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")

    # Restore
    def _restore():
        Logger.info(f"Restoring cluster/gpu-nodes from {upgrade_version} => {current_version}")
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['driver.blacklist'] = True
            tcfg['driver.version'] = current_version
            tcfg['driver.upgradePolicy.enable'] = True
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
            K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR")

        # Check for reboot operation
        K8Helper.wait_for_upgrade_completion_status(environment, deviceconfig_install.devicecfg_list, gpu_nodes)

        # Check for corresponding deviceconfig updated
        K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
        for devcfg in deviceconfig_install.devicecfg_list:
            K8Helper.wait_kmm_worker_completion(environment, devcfg)

        driver_version = amdgpu.get_matching_driver_version(current_version)
        K8Helper.check_deviceconfig_driver_version(gpu_cluster, current_version, environment)
        K8Helper.check_node_driver_version(gpu_cluster, current_version, driver_version, environment)

    request.addfinalizer(_restore)

    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.blacklist'] = True
        tcfg['driver.version'] = upgrade_version
        tcfg['driver.upgradePolicy.enable'] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR")

    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, devcfg_info in devcfg_map.items():
        devcfg_driver_version = devcfg_info.get('spec').get('driver').get('version')
        Logger.info(f'Configured Version: {devcfg_driver_version}') 
        K8Helper.triage(environment, upgrade_version == devcfg_driver_version,
                        f"Expected {upgrade_version}, found {devcfg_driver_version}")

    # Enable upgradePolicy
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.upgradePolicy.enable'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR to enable upgradePolicy")

    K8Helper.wait_for_upgrade_completion_status(environment, deviceconfig_install.devicecfg_list, gpu_nodes)
    if environment.gpu_operator_version in ["v1.2.0", "v1.2.1", "v1.2.2"]:
        # For v1.2.0 and v1.2.1, manual reboot is required
        Logger.info(f"For {environment.gpu_operator_version}, manual reboot of nodes required post driver upgrade")
        for node in gpu_nodes:
            node_name = k8_util.k8_get_node_hostname(node)
            ret_code = k8_util.reboot_node(gpu_cluster, node_name)
            K8Helper.triage(environment, ret_code == 0, f"Failed to reboot node {node_name}")

    driver_version = amdgpu.get_matching_driver_version(upgrade_version)
    K8Helper.check_deviceconfig_driver_version(gpu_cluster, upgrade_version, environment)
    K8Helper.check_node_driver_version(gpu_cluster, upgrade_version, driver_version, environment)

def test_deviceplugin_create_delete_gpu_workload(request, deviceconfig_install, images, environment):
    global Logger
    '''
    create the first workload pod requesting one gpu
    Assumption: no other workload pod with gpu has been instantiated
    '''

    local_workload_ctxts = []
    def _cleanup_local_workloads():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    request.addfinalizer(_cleanup_local_workloads)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])
    
    # Take one node with gpu
    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    # check gpu capacity
    init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, init_cap >= 0 or init_alloc >= 0,
                    f'Error getting gpu capacity & allocatable values')

    # check if the node has allocatable gpus; if not fail
    K8Helper.triage(environment, int(init_cap) > 0 or int(init_alloc) > 0,
                    f'no gpu available for workload based testcases')

    # Create a workload
    params = {
        "node_name" : node_name,
        "num_gpu_reqd" : init_cap,
        "workload_selection" : "busybox-workload",
    }
    workload_ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    K8Helper.triage(environment, workload_ctxt['podStatus'] == K8Helper.PodStatus.RUNNING,
                    f"Workload failed to start {workload_ctxt}")
    local_workload_ctxts.append(workload_ctxt)

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

    # delete the workload
    Logger.info(f"Delete the first workload with gpu")
    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **workload_ctxt)

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

def test_deviceplugin_create_workload_with_max_gpu(request, deviceconfig_install, images, environment):
    global Logger
    '''
    Creates and deletes a workload with max number of gpus available on the node
    Get the capacity and create a workload with gpus equal to the capacity
    Check the gpu alloc status
    Delete the workload
    Check the gpu allow status again
    '''
    local_workload_ctxts = []
    def _cleanup_local_workloads():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    request.addfinalizer(_cleanup_local_workloads)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])
    
    # Take one node with gpu
    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    # check gpu capacity
    init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (init_cap >= 0 or init_alloc >= 0), f'Error getting gpu capacity & allocatable values')

    # check if the node has allocatable gpus; if not fail
    K8Helper.triage(environment, (int(init_cap) > 0 or int(init_alloc) > 0),
                    f'no gpu available for workload based testcases')

    # Create a workload requesting max-capacity
    params = {
        "node_name" : node_name,
        "num_gpu_reqd" : init_cap,
        "workload_selection" : "busybox-workload",
    }
    workload_ctxt = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(workload_ctxt)
    K8Helper.triage(environment, (workload_ctxt['podStatus'] == K8Helper.PodStatus.RUNNING),
                    f"Workload failed to start {workload_ctxt}")

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

    # delete the workload
    Logger.info(f"Delete the first workload with gpu")
    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **workload_ctxt)

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

def test_deviceplugin_create_workload_exceed_gpu_capacity(request, deviceconfig_install, images, environment):
    global Logger
    '''
    Create a workload requesting gpus > capacity
    Check if the pod is in unschedulable state
    Check gpu alloc status
    Delete the workload
    '''
    local_workload_ctxts = []
    def _cleanup_local_workloads():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    request.addfinalizer(_cleanup_local_workloads)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "gpu-operator failed to find amd/gpu nodes in the cluster")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])
    
    # Take one node with gpu
    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    # check gpu capacity
    init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (init_cap >= 0 or init_alloc >= 0),
                    f'Error getting gpu capacity & allocatable values')

    # check if the node has allocatable gpus; if not fail
    K8Helper.triage(environment, (int(init_cap) > 0 or int(init_alloc) > 0),
                    f'no gpu available for workload based testcases')

    # Create a workload
    params = {
        "node_name" : node_name,
        "num_gpu_reqd" : init_cap + 1,
        "workload_selection" : "busybox-workload",
    }
    workload_ctxt = K8Helper.workload_operation(environment, 
                                                K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(workload_ctxt)
    K8Helper.triage(environment, (workload_ctxt['podStatus'] == K8Helper.PodStatus.PENDING),
                    f"Workload started with more resources!!! {workload_ctxt}")

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

    # delete the workload
    Logger.info(f"Delete the first workload with gpu")
    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **workload_ctxt)

    new_cap, new_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (new_cap != -1 or new_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: capacity: {new_cap} allocatable: {new_alloc}')
    K8Helper.triage(environment, (new_cap == init_cap and new_alloc == init_alloc),
                    f'gpu status error: capacity, status initial/final: {init_cap},{init_alloc}/{new_cap},{new_alloc}')

def test_driver_deviceplugin_multiple_workloads_with_gpu(request, deviceconfig_install, images, environment):
    global Logger
    '''
    Create a workload wl1 with max gpu available
    Check pod status, alloc status
    Create a second workload wl2 with a gpu
    Check if the workload wl2 is unschedulable
    Delete the first workload wl1
    Second workload wl1 must be up and running now
    check pod status and alloc status
    '''
    local_workload_ctxts = []
    def _cleanup_local_workloads():
        for ctxt in local_workload_ctxts:
            K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **ctxt)
    request.addfinalizer(_cleanup_local_workloads)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "gpu-operator failed to find amd/gpu nodes in the cluster")
    K8Helper.delete_debug_pods(["default", environment.gpu_operator_namespace])
    
    # Take one node with gpu
    gpu_node = gpu_nodes[0]
    node_name = k8_util.k8_get_node_hostname(gpu_node)

    # check gpu capacity
    init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
    K8Helper.triage(environment, (init_cap != -1 or init_alloc != -1),
                    f'Err getting gpu capacity and allocatable values: init_cap: {init_cap} init_alloc: {init_alloc}')
    # check if the node has allocatable gpus; if not fail
    K8Helper.triage(environment, (int(init_cap) != 0 or int(init_alloc) != 0), f'no gpu available')

    # Create a workload requesting max-capacity
    params = {
        "node_name" : node_name,
        "num_gpu_reqd" : init_cap,
        "workload_selection" : "busybox-workload",
    }
    first_workload = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(first_workload)
    K8Helper.triage(environment, (first_workload['podStatus'] == K8Helper.PodStatus.RUNNING),
                    f"Workload failed to start {first_workload}")

    # launch another workload wl2 requesting one gpu; should be in unschedulable state
    # Create a workload requesting max-capacity
    params = {
        "node_name" : node_name,
        "num_gpu_reqd" : 1,
        "workload_selection" : "busybox-workload",
    }
    second_workload = K8Helper.workload_operation(environment, K8Helper.WorkloadOp.START_WORKLOAD, **params)
    local_workload_ctxts.append(second_workload)
    K8Helper.triage(environment, (second_workload['podStatus'] == K8Helper.PodStatus.PENDING),
                    f"Workload is running when it is expected to be pending")

    # delete workload wl1
    Logger.info(f"Delete the first workload with gpu")
    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **first_workload)
    time.sleep(30)

    # check workload wl2 status
    Logger.info(f"Check the status of the second workload with gpu")
    workload_pods = [
        common.PodInfo(second_workload['pod_name'], 1, 1),
    ]
    workload_status = None
    for _ in range(5):
        status_info = k8_util.k8_check_pod_status("default", workload_pods)
        workload_status = {status for name, (status, full_pod_info) in status_info.items()}
        if "Pending" in workload_status:
            time.sleep(30)
        else:
            break

    K8Helper.collect_unhealthy_pods(environment, workload_pods)
    K8Helper.triage(environment, (workload_status == {"Running"}),
                    f"Invalid workload-status: {workload_status} for pod: {second_workload['pod_name']}")

    # delete wl2
    Logger.info(f"Delete the second workload with gpu")
    K8Helper.workload_operation(environment, K8Helper.WorkloadOp.STOP_WORKLOAD, **second_workload)

def test_upgrade_driver_using_label(request, gpu_cluster, environment, deviceconfig_install, limited_upgrade_version, inbox_driver_skip):
    global Logger
    '''
    Upgrade driver using label update method
    '''
    if environment.gpu_operator_version in ["v1.0.0", "v1.1.0"]:
        pytest.skip(f"Skipping driver-upgrade testcase for current version {environment.gpu_operator_version}")
    if gpu_cluster.is_mini_kube():
        pytest.skip("Using mini-kube cluster - skip driver upgrade testcases")

    current_version = environment.amdgpu_driver_spec["default-version"]
    Logger.info(f"Upgrading cluster/gpu-nodes from {current_version} => {limited_upgrade_version}")
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "gpu-operator failed to find amd/gpu nodes in the cluster")
    # Restore
    def _restore():
        Logger.info(f"Restoring cluster/gpu-nodes from {limited_upgrade_version} => {current_version}")
        # Update deviceconfig with new driver version (disable upgradePolicy)
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['driver.blacklist'] = True
            tcfg['driver.version'] = current_version
            tcfg['driver.upgradePolicy.enable'] = True
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
            K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR to enable upgradePolicy")

        time.sleep(20)
        # Check for reboot operation
        K8Helper.wait_for_upgrade_completion_status(environment, deviceconfig_install.devicecfg_list, gpu_nodes)
        K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
        for devcfg in deviceconfig_install.devicecfg_list:
            K8Helper.wait_kmm_worker_completion(environment, devcfg)

        driver_version = amdgpu.get_matching_driver_version(current_version)
        K8Helper.check_deviceconfig_driver_version(gpu_cluster, current_version, environment)
        K8Helper.check_node_driver_version(gpu_cluster, current_version, driver_version, environment)

    request.addfinalizer(_restore)

    # Update deviceconfig with new driver version (disable upgradePolicy)
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['driver.blacklist'] = True
        tcfg['driver.version'] = limited_upgrade_version
        tcfg['driver.upgradePolicy.enable'] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, ret_code == 0, "Failed to modify deviceconfig CR to enable upgradePolicy")

    time.sleep(20)
    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    for devcfg in deviceconfig_install.devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    # Build labels
    labels = {}
    for devcfg in deviceconfig_install.devicecfg_list:
        labels[f"kmm.node.kubernetes.io/version-module.{environment.gpu_operator_namespace}.{devcfg}"] = limited_upgrade_version
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        K8Helper.triage(environment, (k8_util.k8_label_node(node_name, labels)), f"Failed to update label on node {node_name}")

    K8Helper.check_deviceconfig_status(environment, deviceconfig_install.devicecfg_list)
    for devcfg in deviceconfig_install.devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    driver_version = amdgpu.get_matching_driver_version(limited_upgrade_version)
    K8Helper.check_deviceconfig_driver_version(gpu_cluster, limited_upgrade_version, environment)
    K8Helper.check_node_driver_version(gpu_cluster, limited_upgrade_version, driver_version, environment)
    #K8Helper.wait_for_upgrade_completion_status(environment, deviceconfig_install.devicecfg_list, gpu_nodes)
    if environment.gpu_operator_version in ["v1.2.0", "v1.2.1", "v1.2.2"]:
        # For v1.2.0 and v1.2.1, manual reboot is required
        Logger.info(f"For {environment.gpu_operator_version}, manual reboot of nodes required post driver upgrade")
        for node in gpu_nodes:
            node_name = k8_util.k8_get_node_hostname(node)
            ret_code = k8_util.reboot_node(gpu_cluster, node_name)
            K8Helper.triage(environment, ret_code == 0, f"Failed to reboot node {node_name}")

    driver_version = amdgpu.get_matching_driver_version(limited_upgrade_version)
    K8Helper.check_node_driver_version(gpu_cluster, limited_upgrade_version, driver_version, environment)

