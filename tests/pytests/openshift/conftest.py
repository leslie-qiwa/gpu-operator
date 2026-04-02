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
from lib import common
import lib.k8_util as k8_util
import lib.olm_util as olm_util
import lib.npd_util as npd_util
from lib.util import K8Helper

Logger = logging.getLogger("k8.conftest")

@pytest.fixture(scope="session")
def inbox_driver_skip(environment):
    if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
        pytest.skip("Using inbox amdgpu driver - skip")
    return

@pytest.fixture(scope="session")
def gpu_operator_release_name(environment):
    return "amd-gpu-operator"

@pytest.fixture(scope="session", autouse=True)
def init_testbed(request, gpu_cluster, gpu_operator_release_name, environment):
    global Logger
    all_namespaces = []
    if hasattr(environment, "gpu_operator_namespace"):
        all_namespaces.append(environment.gpu_operator_namespace)

    def _cleanup_steps():
        # cleanup
        K8Helper.delete_debug_pods(all_namespaces + ["default"])

        # remove gpu-operator helm-chart
        if hasattr(environment, "gpu_operator_namespace"):
            # remove any deviceconfig instances
            device_cfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, None)
            for devcfg_name, _ in device_cfg_info.items():
                k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)

            # Check for subscriptions - nfd, kernel-module-management
            ret_code, subscriptions, ret_stderr = k8_util.k8_list_subscriptions()
            K8Helper.triage(environment, (ret_code == 0),
                            f"Failed to collect subscriptions from openshift-cluster, error {ret_stderr}")
            mandatory_subscriptions = ["nfd", "kernel-module-management"]
            for item in mandatory_subscriptions:
                found = next((sub for sub in subscriptions if sub['spec']['name'] == item), None)
                K8Helper.triage(environment, (found != None), f"Failed to find subscription {item}")

            gpu_op_sub = next((sub for sub in subscriptions if sub['spec']['name'] == gpu_operator_release_name), None)

            # remove existing gpu-operator olm-bundle
            if gpu_op_sub:
                Logger.debug(f"Found active subscription for {gpu_operator_release_name}, uninstalling")
                ret_code, ret_stdout, ret_stderr = olm_util.olm_cleanup(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)
                K8Helper.triage(environment, (ret_code == 0), f"Failed to uninstall {gpu_operator_release_name}", expected_to_fail = True)
                time.sleep(10)
            else:
                Logger.debug(f"No active subscription for {gpu_operator_release_name} found")

            # Check for catalogsources
            ret_code, catalogsources, ret_stderr = k8_util.k8_list_catalogsources()
            K8Helper.triage(environment, (ret_code == 0),
                            f"Failed to collect catalogsources from openshift-cluster, error {ret_stderr}")
            gpu_op_catalog_name = f"{gpu_operator_release_name}-catalog"
            gpu_op_catalog = next((catalog for catalog in catalogsources if catalog['metadata']['name'] == gpu_op_catalog_name), None)
            if gpu_op_catalog:
                ret_code, _, ret_stderr = k8_util.k8_delete_custom_resource("operators.coreos.com", "v1alpha1", "catalogsources",
                                                                            environment.gpu_operator_namespace, gpu_op_catalog_name)
                K8Helper.triage(environment, (ret_code == 0),
                                f"Failed to delete catalogsources from openshift-cluster, error {ret_stderr}", expected_to_fail=True)
            else:
                Logger.debug(f"No gpu-operator catalogsources found in the cluster")

            # Check for clusterserviceversions
            ret_code, csv_list, ret_stderr = k8_util.k8_list_clusterserviceversions()
            K8Helper.triage(environment, (ret_code == 0),
                            f"Failed to collect clusterserviceversions from openshift-cluster, error {ret_stderr}")
            gpu_op_csv_name = f"{gpu_operator_release_name}"
            gpu_op_csv = next((csv for csv in csv_list if gpu_operator_release_name in csv['metadata']['name']), None)
            if gpu_op_csv:
                ret_code, _, ret_stderr = k8_util.k8_delete_custom_resource("operators.coreos.com", "v1alpha1", "clusterserviceversions",
                                                                            environment.gpu_operator_namespace, gpu_op_csv['metadata']['name'])
                K8Helper.triage(environment, (ret_code == 0),
                                f"Failed to delete clusterserviceversions from openshift-cluster, error {ret_stderr}", expected_to_fail=True)
            else:
                Logger.debug(f"No gpu-operator csv found in the cluster")

    Logger.info("Cleanup before starting test session")
    _cleanup_steps()

    # Init k8 cluster
    k8_util.k8_init_cluster(gpu_cluster, all_namespaces)
    blacklist_enable = True
    if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
        Logger.info("Using inbox driver - remove blacklist file")
        blacklist_enable = False

    ret_code, ret_stdout, ret_stderr = olm_util.olm_manage_amdgpu_driver_blacklist(enable = blacklist_enable,
                                                                                   is_mini_kube_cluster = gpu_cluster.is_mini_kube())
    K8Helper.triage(environment, (ret_code == 0),
                    f"Failed to {'enable' if blacklist_enable else 'disable'} amdgpu_driver_blacklist, {ret_stdout}, error: {ret_stderr}")
    status = k8_util.k8_wait_for_cluster_ready(gpu_cluster.is_mini_kube())
    K8Helper.triage(environment, (status == 0),
                        f"Cluster is not Ready after amdgpu blacklist is {'enable' if blacklist_enable else 'disable'}")

    yield
    Logger.info("Cleanup after starting test session")
    _cleanup_steps()
    return

@pytest.fixture(scope="module")
def gpu_operator_install(gpu_cluster, gpu_operator_release_name, images, environment):
    global Logger

    # cleanup
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)

    # manage gpu-operator using operator-sdk
    
    # Check for subscriptions - nfd, kernel-module-management
    ret_code, subscriptions, ret_stderr = k8_util.k8_list_subscriptions()
    K8Helper.triage(environment, (ret_code == 0), f"Failed to collect subscriptions from openshift-cluster, error {ret_stderr}")
    gpu_op_sub = next((sub for sub in subscriptions if sub['spec']['name'] == gpu_operator_release_name), None)

    # remove existing gpu-operator olm-bundle
    if gpu_op_sub:
        Logger.debug(f"Found active subscription for {gpu_operator_release_name}, uninstalling")
        ret_code, ret_stdout, ret_stderr = olm_util.olm_cleanup(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to uninstall {gpu_operator_release_name}", expected_to_fail = True)
        time.sleep(10)
    else:
        Logger.debug(f"No active subscription for {gpu_operator_release_name} found")

    opts = {}
    url = images['gpu-operator.olm-bundle']
    if images.get("gpu-operator.olm-bundle.secret", None):
        opts["pull-secret-name"] = images["gpu-operator.olm-bundle.secret"]

    ret_code, ret_stdout, ret_stderr = olm_util.olm_install(gpu_cluster, url, environment.gpu_operator_namespace, **opts)
    time.sleep(30)
    if ret_code != 0:
        Logger.debug(f"Expected failure to install {gpu_operator_release_name}, stdout: {ret_stdout}, error: {ret_stderr}")

        # Patch default service-account for secret defined
        ret_code, ret_stdout, ret_stderr = olm_util.patch_secrets(gpu_cluster, environment.gpu_operator_namespace)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to update Openshift serviceaccount with supplied secrets")
        ret_code = k8_util.k8_delete_all_pods_with_name_pattern(environment.gpu_operator_namespace,
                                                                "amd-gpu-operator-controller-manager")
        K8Helper.triage(environment, (ret_code == 0), f"Failed to restart amd-gpu-operator-controller-manager after patch secret")
        time.sleep(30)

    ret_code, subscriptions, ret_stderr = k8_util.k8_list_subscriptions()
    mandatory_subscriptions = ["nfd", "kernel-module-management", gpu_operator_release_name]
    for item in mandatory_subscriptions:
        found = next((sub for sub in subscriptions if sub['spec']['name'] == item), None)
        K8Helper.triage(environment, (found != None), f"Failed to find subscription {item}")

    # Check for PODs
    gpu_operator_pods = [
        common.PodInfo('amd-gpu-operator-controller-manager', 1, 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(environment.gpu_operator_namespace, gpu_operator_pods)
    K8Helper.triage(environment, not failed_pods, f"One or more pods are not ready - {failed_pods}")
    yield
    # cleanup - remove any deviceconfigs and then gpu-operator helm-chart
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)

    ret_code, ret_stdout, ret_stderr = olm_util.olm_cleanup(gpu_cluster, gpu_operator_release_name, environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to uninstall {gpu_operator_release_name}, error : {ret_stderr}")
    return

@pytest.fixture(scope="module")
def deploy_npd_daemonset(gpu_cluster, environment):
    global Logger
    Logger.info("Deploy node-problem-detector with default configuration")
    npd_util.init_npd_oc(gpu_cluster, environment)
    yield
    Logger.info("Cleanup node-problem-detector")
    npd_util.fini_npd_oc(gpu_cluster, environment)
    return

