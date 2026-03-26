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
import os
import logging
import time
import json
from lib import common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.amdgpu as amdgpu_util
from lib.util import K8Helper

Logger = logging.getLogger("standalone.conftest")

@pytest.fixture(scope="session")
def inbox_driver_skip(environment):
    if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
        pytest.skip("Using inbox amdgpu driver - skip")
    return

@pytest.fixture(scope="session")
def gpu_operator_release_name(environment):
    return "gpu-operator"

@pytest.fixture(scope="session")
def exporter_release_name():
    return "device-metrics-exporter"

@pytest.fixture(scope="session", autouse=True)
def init_testbed(request, gpu_cluster, gpu_operator_release_name, exporter_release_name, environment):
    global Logger
    all_namespaces = []
    if hasattr(environment, "gpu_operator_namespace"):
        all_namespaces.append(environment.gpu_operator_namespace)
    if hasattr(environment, "exporter_namespace"):
        all_namespaces.append(environment.exporter_namespace)

    def _cleanup_steps():
        # cleanup
        K8Helper.delete_debug_pods(all_namespaces + ["default"])

        # remove gpu-operator helm-chart
        if hasattr(environment, "gpu_operator_namespace"):
            # remove any deviceconfig instances
            device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
            for devcfg_name, _ in device_cfg_info.items():
                k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)

            if helm_util.is_helm_chart_deployed(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace):
                Logger.warn(f"helm {gpu_operator_release_name} is already deployed - cleanup")
                ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, gpu_operator_release_name,
                                                                          environment.gpu_operator_namespace)
                if ret_code != 0:
                    helm_util.helm_cleanup(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)

        if hasattr(environment, "exporter_namespace"):
            if helm_util.is_helm_chart_deployed(gpu_cluster, exporter_release_name, environment.exporter_namespace):
                Logger.warn(f"helm {exporter_release_name} is already deployed - cleanup")
                ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, exporter_release_name,
                                                                          environment.exporter_namespace)
                if ret_code != 0:
                    helm_util.helm_cleanup(gpu_cluster, exporter_release_name, environment.exporter_namespace)

    Logger.info("Cleanup before starting test session")
    _cleanup_steps()

    # Init k8 cluster
    k8_util.k8_init_cluster(gpu_cluster, all_namespaces)

    # Load access credentials from testbed.json
    if request.config.option.testbed and os.path.exists(request.config.option.testbed):
        with open(request.config.option.testbed, "r") as fp:
            testbed = json.load(fp)
        for entry in testbed.get("instances", []):
            node = gpu_cluster.find_node_by_ip(entry["ip"])
            node.user_name = entry["username"]
            node.password = entry["password"]
    else:
        pytest.fail(f"Missing testbed.json file - cannot run testcases")
    yield
    Logger.info("Cleanup after starting test session")
    _cleanup_steps()
    return

@pytest.fixture(scope="session")
def gpu_operator_install(gpu_cluster, gpu_operator_release_name, images, environment):
    global Logger

    # cleanup
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)

    if helm_util.is_helm_chart_deployed(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace):
        Logger.warn(f"helm {gpu_operator_release_name} is already deployed - cleanup")
        ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, gpu_operator_release_name,
                                                                    environment.gpu_operator_namespace)
        if ret_code != 0:
            helm_util.helm_cleanup(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)
        #k8_util.k8_delete_namespace(environment.gpu_operator_namespace)

    if images.get("gpu-operator.repo", None):
        helm_util.helm_add_repo(gpu_cluster, images.get("gpu-operator.repo-name"), images.get("gpu-operator.repo"))

    secret_list = []
    for entry in gpu_cluster.k8_secrets["secrets"]:
        secret_list.append(entry["name"])
    values_yaml = os.path.join(environment.logdir, f"values_{environment.gpu_operator_version}.yaml")
    if spec_util.generate_helmchart_deployment_config(environment.gpu_operator_version, images, secret_list, values_yaml):
        Logger.debug(f"Generated values.yaml for helm-chart install command, {values_yaml}")
    else:
        values_yaml = None

    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(gpu_cluster, gpu_operator_release_name,
                                                              environment.gpu_operator_namespace,
                                                              images.get('gpu-operator.helm-chart', None),
                                                              environment.gpu_operator_version, values_yaml)
    if ret_code != 0:
        Logger.error(f"Failed to install helm chart for {gpu_operator_release_name}")
        Logger.error(f"Stdout: {ret_stdout}")
        Logger.error(f"Stderr: {ret_stderr}")
    K8Helper.triage(environment, (ret_code == 0), f"Failed to install {gpu_operator_release_name}")
    time.sleep(30)
    yield
    # cleanup - remove any deviceconfigs and then gpu-operator helm-chart
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)

    ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to uninstall {gpu_operator_release_name} helm-chart, error: {ret_stderr}")
    return

@pytest.fixture(scope="session")
def amdgpu_driver_install(gpu_cluster, images, gpu_operator_install, environment):
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
            'metricsExporter.enable' : False,
            'testRunner.enable' : False,
            'configManager.enable' : False,
        }
    test_config.update(images)
    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'driver-mgmt', environment.amdgpu_driver_spec)
    exporter_port_map = {}
    devicecfg_list = []

    for spec_name, tcfg in test_cfg_map.items():
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")
        devicecfg_list.append(tcfg['metadata.name'])

    # Check for corresponding deviceconfig created
    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)

    devicecfg_pods = [
        common.PodInfo('device-plugin', len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, devicecfg_pods, sleep_time = 20)
    K8Helper.triage(environment, (not failed_pods), f"One or more pods are not ready - {failed_pods}")

    yield devcfg_info

    device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
    return

