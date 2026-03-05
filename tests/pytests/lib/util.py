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
import re
import json
import pprint
import copy
import subprocess
import shutil
from enum import Enum
from datetime import datetime

import lib.common as common
import lib.k8_util as k8_util
import lib.spec_util as spec_util

Logger = logging.getLogger("k8.helper")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)

class K8Helper:

    class PodStatus(Enum):
        NA          = 0
        PENDING     = 1
        RUNNING     = 2
        FAILED      = 3
        SUCCEEDED   = 4
        UNKNOWN     = 5
        STOPPED     = 6

    class WorkloadOp(Enum):
        UNKNOWN         = 0
        START_WORKLOAD  = 1
        STOP_WORKLOAD   = 2

    @staticmethod
    def get_amd_smi_path(environment):
        return "/opt/rocm/bin/amd-smi"

    @staticmethod
    def wait_for_upgrade_completion_status(environment, devicecfg_list, gpu_nodes):
        # Check for kmm-worker-{gpu-node-name}-test-deviceconfig PODs to be started and completed
        global Logger
        if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
            Logger.info("Using inbox amdgpu driver - skip kmm verification")
            return

        time.sleep(20)
        upgrade_complete = False
        for _ in range(10):
            pending_nodes = set(map(lambda x: x['metadata']['name'], gpu_nodes))
            for devcfg in devicecfg_list:
                devcfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, devcfg)
                K8Helper.triage(environment, devcfg_info != None and devcfg in devcfg_info,
                                f"Failed to collect status of deviceconfig {devcfg}")

                if devcfg_info[devcfg].get('status', None):
                    nodeStatusMap = devcfg_info[devcfg]['status'].get('nodeModuleStatus', None)

                    if nodeStatusMap:
                        for node in gpu_nodes:
                            node_name = node['metadata']['name']
                            if node_name in nodeStatusMap:
                                if 'status' in nodeStatusMap[node_name]:
                                    if nodeStatusMap[node_name]['status'] == 'Upgrade-Complete':
                                        if node_name in pending_nodes:
                                            pending_nodes.remove(node_name)
                                    else:
                                        Logger.info(f"Node: {node_name} upgrade status pending : {nodeStatusMap[node_name]}")
                                else:
                                    Logger.warn(f"DeviceConfig nodeModuleStatus does not have status information")
            if len(pending_nodes) > 0:
                Logger.info(f"Waiting for {pending_nodes} to complete upgrade process")
                time.sleep(120)
            else:
                upgrade_complete = True

        if not upgrade_complete:
            Logger.error("Failed to complete upgrade-process for all nodes")
            for devcfg in devicecfg_list:
                devcfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, devcfg)
                K8Helper.triage(environment, devcfg_info != None and devcfg in devcfg_info,
                                f"Failed to collect status of deviceconfig {devcfg}")

                nodeStatus = devcfg_info[devcfg]['status']['nodeModuleStatus']
                Logger.debug(nodeStatus)
            K8Helper.triage(environment, upgrade_complete == True, "upgrade failed")
            return
        Logger.info("Upgrade complete for all nodes")

    @staticmethod
    def wait_for_upgrade_completion_label(environment, devicecfg_list):
        # Check for kmm-worker-{gpu-node-name}-test-deviceconfig PODs to be started and completed
        global Logger
        if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
            Logger.info("Using inbox amdgpu driver - skip kmm verification")
            return

        time.sleep(20)
        upgrade_complete = False
        for _ in range(10):
            # TODO: Check for label change for each node
            # ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
            pass

        if not upgrade_complete:
            Logger.error("Failed to complete upgrade-process for all nodes")
            for devcfg in devicecfg_list:
                devcfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, devcfg)
                K8Helper.triage(environment, devcfg_info != None and devcfg in devcfg_info,
                                f"Failed to collect status of deviceconfig {devcfg}")

                nodeStatus = devcfg_info[devcfg]['status']['nodeModuleStatus']
                Logger.debug(nodeStatus)
            K8Helper.triage(environment, upgrade_complete == True, "upgrade failed")
            return
        Logger.info("Upgrade complete for all nodes")

    @staticmethod
    def wait_kmm_worker_completion(environment, devcfg_name):
        # Check for kmm-worker-{gpu-node-name}-test-deviceconfig PODs to be started and completed
        global Logger
        if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
            Logger.info("Using inbox amdgpu driver - skip kmm verification")
            return

        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
        K8Helper.triage(environment, len(gpu_nodes), "No nodes with AMD/GPU found in the cluster")

        # Check for build pods
        build_pods = []
        build_pods.append(common.PodInfo(f"{devcfg_name}-build", 1, 1))

        build_pod_status = set()
        time.sleep(20)
        for _ in range(10):
            build_pod_status.clear()
            status_info = k8_util.k8_check_pod_status(environment.gpu_operator_namespace, build_pods)
            Logger.debug(f"build pod status: {status_info}")

            for pod_name, (status, full_pod_info) in status_info.items():
                if status == 'Running':
                    build_pod_status.add(K8Helper.PodStatus.RUNNING)
                elif status == 'Pending':
                    build_pod_status.add(K8Helper.PodStatus.PENDING)
                elif status == 'Failed':
                    build_pod_status.add(K8Helper.PodStatus.FAILED)
                else:
                    Logger.warn(f"build pod status unknown, pod-name: {pod_name}")
                    build_pod_status.add(K8Helper.PodStatus.UNKNOWN)

            if K8Helper.PodStatus.PENDING in build_pod_status or K8Helper.PodStatus.RUNNING in build_pod_status:
                Logger.debug("Wait for 120-sec as some of the build pods are in Running/Pending status")
                time.sleep(120)
            else:
                break

        K8Helper.triage(environment, K8Helper.PodStatus.PENDING not in build_pod_status, "build pod still pending")
        K8Helper.triage(environment, K8Helper.PodStatus.RUNNING not in build_pod_status, "build pod still running")
        K8Helper.triage(environment, K8Helper.PodStatus.FAILED not in build_pod_status, "build pod failed")

        # Check for kmm pods
        kmm_worker_pods = []
        for node in gpu_nodes:
            node_name = node['metadata']['name']
            K8Helper.triage(environment, node_name != None, "Missing node-name for the gpu node in the node-info JSON")
            kmm_worker_pods.append(common.PodInfo(f"kmm-worker-{node_name}-", 1, 1))

        kmm_pod_status = set()
        time.sleep(20)
        for _ in range(5):
            kmm_pod_status.clear()
            status_info = k8_util.k8_check_pod_status(environment.gpu_operator_namespace, kmm_worker_pods)
            Logger.debug(f"kmm-worker status: {status_info}")

            for pod_name, (status, full_pod_info) in status_info.items():
                if status == 'Running':
                    kmm_pod_status.add(K8Helper.PodStatus.RUNNING)
                elif status == 'Pending':
                    kmm_pod_status.add(K8Helper.PodStatus.PENDING)
                elif status == 'Failed':
                    kmm_pod_status.add(K8Helper.PodStatus.FAILED)
                else:
                    Logger.warn(f"kmm pod status unknown, pod-name: {pod_name}")
                    kmm_pod_status.add(K8Helper.PodStatus.UNKNOWN)

            if K8Helper.PodStatus.PENDING in kmm_pod_status or K8Helper.PodStatus.RUNNING in kmm_pod_status:
                Logger.debug("Wait for 120-sec as some of the kmm-worker pods are in Running/Pending status")
                time.sleep(120)
            else:
                break

        K8Helper.triage(environment, K8Helper.PodStatus.PENDING not in kmm_pod_status, "kmm-worker pod still pending")
        K8Helper.triage(environment, K8Helper.PodStatus.RUNNING not in kmm_pod_status, "kmm-worker pod still running")
        K8Helper.triage(environment, K8Helper.PodStatus.FAILED not in kmm_pod_status, "kmm-worker pod failed")

        # Finally check for labels
        label_missing = set()
        for _ in range(5):
            label_missing.clear()
            ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
            K8Helper.triage(environment, ret_code == 0, "Error while getting gpu-nodes from k8-cluster")
            K8Helper.triage(environment, len(gpu_nodes), "No nodes with AMD/GPU found in the cluster")

            pattern = r"kmm\.node\.kubernetes\.io/" + environment.gpu_operator_namespace + r"\.(.*?)\.ready"
            for node in gpu_nodes:
                label_found = False
                for label, _ in node['metadata']['labels'].items():
                    if re.match(pattern, label):
                        label_found = True
                        break
                if not label_found:
                    label_missing.add(node['metadata']['name'])
            if len(label_missing) > 0:
                Logger.warn(f"Missing kmm.ready label for {label_missing}")
                time.sleep(120)
        K8Helper.triage(environment, label_found, f"One or more nodes missing kmm.ready label : {label_missing}")
        return

    # Check for corresponding deviceconfig created
    @staticmethod
    def check_deviceconfig_status(environment, devicecfg_list):
        for devcfg in devicecfg_list:
            devcfg_info = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace, devcfg)
            K8Helper.triage(environment, devcfg_info != None and devcfg in devcfg_info,
                            f"Failed to collect status of deviceconfig {devcfg}")
            #status_info = devcfg_info[devcfg].get('status')
            #if environment.gpu_operator_version > "v1.1.0":
            #    conditions = status_info.get('conditions', [])
            #    K8Helper.triage(environment, len(conditions) > 0, f"deviceconfig status.conditions is empty for {devcfg}")
            #    K8Helper.triage(environment, conditions[0].get('status') == 'True', f"deviceconfig {devcfg} status is not True")
            #    K8Helper.triage(environment, conditions[0].get('type') == 'Ready', f"deviceconfig {devcfg} type is not Ready")
        return

    @staticmethod
    def check_deviceconfig_driver_version(gpu_cluster, config_version, environment):
        global Logger
        global LogPrettyPrinter
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
        for devcfg_name, devcfg_info in devcfg_map.items():
            devcfg_driver_version = devcfg_info.get('spec').get('driver').get('version')
            Logger.info(f'Configured Version: {devcfg_driver_version}') 
            K8Helper.triage(environment, config_version == devcfg_driver_version,
                            f"Expected config_version: {config_version}, device_config: {devcfg_driver_version}")
    @staticmethod
    def update_node_driver_version(gpu_cluster, environment):
        # collect currently applied deviceconfigs
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)

        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        for node in gpu_nodes:
            node_ip = k8_util.k8_get_node_address(node)
            cluster_node = gpu_cluster.find_node_by_ip(node_ip)
            # Match to one of the deviceconfigs
            for devcfg_name, _ in devcfg_map.items():
                version_module_label = f"kmm.node.kubernetes.io/version-module.{environment.gpu_operator_namespace}.{devcfg_name}"
                node_driver_version = node['metadata']['labels'].get(version_module_label, None)
                if node_driver_version:
                    cluster_node.amdgpu_driver_version = node_driver_version
                    break
        return

    @staticmethod
    def check_node_driver_version(gpu_cluster, config_version, rocm_version, environment):
        global Logger
        global LogPrettyPrinter

        # collect currently applied deviceconfigs
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)

        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        for node in gpu_nodes:
            node_name = k8_util.k8_get_node_hostname(node)
            label_found = False
            # Match to one of the deviceconfigs
            for devcfg_name, _ in devcfg_map.items():
                version_module_label = f"kmm.node.kubernetes.io/version-module.{environment.gpu_operator_namespace}.{devcfg_name}"
                node_driver_version = node['metadata']['labels'].get(version_module_label, None)
                if node_driver_version:
                    K8Helper.triage(environment, config_version == node_driver_version,
                                    f"failed for node {node_name}: {node_driver_version}")
                    label_found = True
                    break
            K8Helper.triage(environment, (label_found),
                            f"Missing label kmm.node.kubernetes.io/version-module.{environment.gpu_operator_namespace}.<devcfg_name> for node {node_name}")

            cmd = ["dmesg", "-T"]
            #cmd = ["sudo", "dmesg", "-T", "|", "grep", "'amdgpu version'", "|", "tail", "-1"]
            ret_code, resp_stdout = k8_util.run_command_on_node(gpu_cluster, node_name, cmd, skip_chroot = True)
            K8Helper.triage(environment, ret_code == 0, f"error getting dmesg from {node_name} {node_name}")
            K8Helper.triage(environment, resp_stdout != None, f"Error: Command output is None")
            Logger.debug(f"Cmd:{cmd}, Response:\n{LogPrettyPrinter.pformat(resp_stdout)}")
            amdgpu_lines = list(filter(lambda line: 'amdgpu version' in line, resp_stdout.split("\n")))
            K8Helper.triage(environment, len(amdgpu_lines) > 0, "No dmesg-lines with 'amdgpu version' information")
            matching_lines = [line for line in amdgpu_lines if rocm_version in line]
            K8Helper.triage(environment, len(matching_lines) > 0, f"can't find {rocm_version} in {amdgpu_lines}")

        # TODO: Enhance to check amd-smi output

    @staticmethod
    def update_test_runner_configmap(recipe, worker, config_map=dict(), framework="RVS", trigger="AUTO_UNHEALTHY_GPU_WATCH"):
        testcase = {
                       "Recipe": recipe,
                       "Iterations": 1,
                       "StopOnFailure": True,
                       "TimeoutSeconds": 2400
                   }
        if framework == "AGFHC":
            testcase["Framework"] = framework
            testcase["Arguments"] = "--ignore-dmesg,--disable-sysmon"
            #TODO try all supported arguments

        trigger_dict = {
                           trigger: {
                               "TestCases": [
                                   testcase
                               ]
                           }
                       }
        if trigger != "AUTO_UNHEALTHY_GPU_WATCH":
            trigger_dict.update(
                       {
                           "AUTO_UNHEALTHY_GPU_WATCH": {
                               "TestCases": [
                                   testcase
                               ]
                           }
                       }
            )
        config_map.update(
            {"TestConfig": {
                "GPU_HEALTH_CHECK": {
                    "TestLocationTrigger": {
                        worker: {
                            "TestParameters": trigger_dict
                        }
                    }
                }
            }}
        )

    @staticmethod
    def triage(environment, condition : bool, message : str, expected_to_fail : bool = False):
        global Logger
        global LogPrettyPrinter
        if condition:
            # Test/condition passed
            return
        # Test/Condition Failed - triage
        ctx = getattr(environment, 'context', None)
        if ctx and hasattr(ctx, 'current_tc_name') and hasattr(ctx, 'unhealthy_pods'):
            Logger.info(f"Adding context about testcase with unhealthy pods information")
            context_file = os.path.join(environment.logdir, f"context_{environment.context.current_tc_name}.json")
            with open(context_file, "w") as fp:
                fp.write(json.dumps(environment.context.unhealthy_pods, indent=4, default= str))
        if expected_to_fail:
            pytest.xfail(message)
        else:
            if environment.tech_support_tool:
                Logger.info(f"Running tech-support tool {environment.tech_support_tool}")
                cmd = [environment.tech_support_tool["tool"], *(environment.tech_support_tool.get("args", []))]
                cmd_resp = subprocess.run(cmd, check=False,
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE,
                                          encoding='utf-8')
                Logger.info(f"tech-support tool returncode: {cmd_resp.returncode}")
                Logger.debug(f"tech-support tool stdout:\n{LogPrettyPrinter.pformat(cmd_resp.stdout)}")
                Logger.debug(f"tech-support tool stderr:\n{LogPrettyPrinter.pformat(cmd_resp.stderr)}")
                if cmd_resp.returncode == 0:
                    for file_name in os.listdir(os.getcwd()):
                        if "techsupport-" in file_name:
                            if ctx and hasattr(ctx, 'current_tc_name'):
                                shutil.move(file_name, 
                                            os.path.join(environment.logdir, "tech-support", f"tech_support_{environment.context.current_tc_name}.tgz"))
                            else:
                                shutil.move(file_name, environment.logdir)
                            break
                else:
                    Logger.warn(f"Failed to generate/collect techsupport file")
            else:
                Logger.warn(f"Missing tech-support-tool information, no additional logs collected")
            pytest.fail(message)

    @staticmethod
    def _rocm_workload_handler(environment, wl_template, op_code, **kwargs):
        """
        create the workload pod requesting gpu(s)
        """

        workload_config = copy.deepcopy(kwargs)
        node_name = workload_config.get("node_name", None)

        if node_name is None:
            # Take one node with gpu
            ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
            K8Helper.triage(environment, ret_code == 0, "gpu-operator failed to find amd/gpu nodes in the cluster")
            gpu_node = gpu_nodes[0]
            node_name = k8_util.k8_get_node_hostname(gpu_node)

        if op_code == K8Helper.WorkloadOp.START_WORKLOAD:
            # check gpu capacity
            init_cap, init_alloc = k8_util.k8_get_node_gpu_capacity(node_name)
            K8Helper.triage(environment, int(init_cap) != -1 or int(init_alloc) != -1,
                            f'Err getting gpu capacity and allocatable values: capacity: {init_cap} allocatable: {init_alloc}')

            # check if the node has allocatable gpus; if not fail
            K8Helper.triage(environment, init_cap != 0 or init_alloc != 0, f'no gpu available')

            # create a workload requesting one gpu
            pod_name = f"gpu-workload-{node_name}-{common.generate_8byte_sha(node_name)}"

            #launch
            Logger.info(f"Create the workload with gpu")
            workload_config.update(
                {
                    'pod_name' : pod_name,
                    'podStatus' : K8Helper.PodStatus.NA,
                    'nodeSelector' : node_name,
                })
            wl_file = os.path.join(environment.logdir, f"{pod_name}.yaml")
            Logger.debug(f"New workload specification : {workload_config}")
            cr_spec = spec_util.generate_k8_workload_template(wl_template['spec'], workload_config, wl_file)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_apply_cr(cr_spec, wl_file)

            workload_pods = [
                common.PodInfo(pod_name, 1, 1),
            ]
            if workload_config.get("no_look", False):
                return None
            for _ in range(20):
                status_info = k8_util.k8_check_pod_status(cr_spec['metadata']['namespace'], workload_pods)
                Logger.debug(f"workload pod status: {status_info}")
                for pod_name, (status, full_pod_info) in status_info.items():
                    if pod_name == workload_config['pod_name']:
                        if status == 'Running':
                            workload_config['podStatus'] = K8Helper.PodStatus.RUNNING
                            break
                        elif status == 'Pending':
                            workload_config['podStatus'] = K8Helper.PodStatus.PENDING
                        elif status == 'Failed':
                            workload_config['podStatus'] = K8Helper.PodStatus.FAILED
                            break
                        else:
                            Logger.warn(f"workload pod status unknown, pod-name: {pod_name}")
                            workload_config['podStatus'] = K8Helper.PodStatus.UNKNOWN
                    time.sleep(30) # give time for image download
            workload_config['spec'] = cr_spec
            return workload_config
        elif op_code == K8Helper.WorkloadOp.STOP_WORKLOAD:
            # delete the workload
            Logger.info(f"Delete the workload with config: {workload_config}")
            ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_cr(workload_config['spec'], None)
            if ret_code != 0:
                Logger.warn(f"Failed to delete workload : {workload_config}")
                workload_config['podStatus'] = K8Helper.PodStatus.UNKNOWN
            else:
                workload_config['podStatus'] = K8Helper.PodStatus.STOPPED
            return workload_config
        return None

    @staticmethod
    def _test_runner_job_handler(environment, op_code, **kwargs):
        """
        create the test-runner job on gpu(s)
        """
        workload_config = copy.deepcopy(kwargs)
        if op_code == K8Helper.WorkloadOp.START_WORKLOAD:
            # TODO: recipe selection based on gpu_series
            #if gpu_series and 'MI2' in gpu_series and recipe == "iet_stress":
                #recipe = "iet_single"
            recipe = "gst_single"
            trigger = "MANUAL"
            Logger.info(f"Start test-runner job: {workload_config}")
            node_name = workload_config.get("node_name", None)

            images = workload_config.get("images", None)
            assert images != None, f"Running test-runner as way to load GPU need images/fixture"
            job_name = f"test-runner-manual-trigger-{common.generate_8byte_sha(node_name)}"
            namespace = environment.gpu_operator_namespace

            configmap = {}
            K8Helper.update_test_runner_configmap(recipe, worker, configmap, framework, trigger)
            create_configmap(request, deviceconfig_install, environment, framework, configmap)

            namespace = environment.gpu_operator_namespace
            sa_name = "test-run"
            cluster_role_name = "test-run-cluster-role"
            crb_name = 'test-run-rb'

            # Create ServiceAccount
            ret_code, ret_stdout, ret_stderr = k8_util.k8_create_service_account(sa_name, namespace)
            debug_on_failure(environment, (ret_code == 0),
                             f"Failed to create service-account, error:{ret_stderr}")

            # Define ClusterRole: verb=get

            #rules = k8_util.k8_create_rules_from_endpoint_list([("/test_runner", "get")])
            rules = list()
            rules.append(
                k8_util.k8_create_rules_from_verbs(
                    resources=["events"],
                    verbs=["get", "list", "watch", "create", "update"],
                    api_groups=[""]
                )
            )
            rules.append(
                k8_util.k8_create_rules_from_verbs(
                    resources=["nodes"],
                    verbs=["patch"],
                    api_groups=[""]
                )
            )
            # Define ClusterRole: verb=get
            ret_code, ret_stdout, ret_stderr = k8_util.k8_create_cluster_role(cluster_role_name, rules)
            debug_on_failure(environment, (ret_code == 0),
                             f"Failed to create test_runner clusterrole with GET, error:{ret_stderr}")

            ret_code, ret_stdout, ret_stderr = k8_util.k8_create_role_binding(crb_name, namespace, cluster_role_name, sa_name)
            debug_on_failure(environment, (ret_code == 0),
                                      f"Failed to create test_runner clusterrole with verbs, error:{ret_stderr}")
            # Create token for ServiceAccount
            token = k8_util.k8_create_token(namespace, sa_name, "1h")
            debug_on_failure(environment, token != None,
                             f"Failed to create token for the service-account : {sa_name}")
            Logger.info(f"TOKEN={token}")

            # Create Job
            k8_util.k8_create_test_runner_job(namespace,
                                              images,
                                              node_name,
                                              sa_name,
                                              job_name,
                                              True,
                                              False, None)

            sa_name = "test-run"
            cluster_role_name = "test-run-cluster-role"
            crb_name = 'test-run-rb'

            workload_config.update(
                {
                    'pod_name' : job_name,
                    'podStatus' : K8Helper.PodStatus.NA,
                })

            for _ in range(5):
                status_info = k8_util.k8_check_pod_status(namespace, workload_pods)
                Logger.debug(f"test-runner pod status: {status_info}")
                for pod_name, (status, full_pod_info) in status_info.items():
                    if workload_config['pod_name'] in pod_name:
                        if status == 'Running':
                            workload_config['podStatus'] = K8Helper.PodStatus.RUNNING
                            break
                        elif status == 'Pending':
                            workload_config['podStatus'] = K8Helper.PodStatus.PENDING
                        elif status == 'Failed':
                            workload_config['podStatus'] = K8Helper.PodStatus.FAILED
                            break
                        elif status == 'Completed':
                            workload_config['podStatus'] = K8Helper.PodStatus.SUCCEEDED
                            break
                        else:
                            Logger.warn(f"workload pod status unknown, pod-name: {pod_name}")
                            workload_config['podStatus'] = K8Helper.PodStatus.UNKNOWN
                    time.sleep(5) # give time for image download
            return workload_config
        elif op_code == K8Helper.WorkloadOp.STOP_WORKLOAD:
            Logger.info(f"Delete test-runner job: {workload_config}")
            k8_util.k8_delete_job(environment.gpu_operator_namespace, workload_config['pod_name'])
            workload_config['podStatus'] = K8Helper.PodStatus.UNKNOWN
            return workload_config

    @staticmethod
    def workload_operation(environment, op_code, **kwargs):
        global Logger
        """
        create the workload pod
        """
        workload_selection = kwargs.get("workload_selection", environment.default_workload)
        if workload_selection == "test-runner-manual-job":
            return K8Helper._test_runner_job_handler(environment, op_code, **kwargs)
        else:
            with open("lib/files/workload-specs.json", "r") as fp:
                wl_templates = json.load(fp)
            tmp_list = list(filter(lambda x: x['workload-type'] == workload_selection, wl_templates['workload-specs']))
            assert len(tmp_list) == 1, f"No workload found with workload-type={workload_selection}"
            return K8Helper._rocm_workload_handler(environment, tmp_list[0], op_code, **kwargs)

    @staticmethod
    def delete_debug_pods(namespaces) -> None:
        for namespace in namespaces:
            k8_util.k8_delete_all_pods_with_name_pattern(namespace, "node-debug-")
            k8_util.k8_delete_all_pods_with_name_pattern(namespace, "curl-cmd-pod-")
            k8_util.k8_delete_all_pods_with_name_pattern(namespace, "gpu-workload-")
            k8_util.k8_delete_all_pods_with_name_pattern(namespace, "techsupport-")
            k8_util.k8_delete_all_pods_with_name_pattern(namespace, "test-runner-manual-trigger-")
        return

    @staticmethod
    def watch_for_daemon_rollout(environment, namespace, gpu_nodes_count):
        # Check for daemon rollout to complete and PODs to be started and completed
        global Logger
        ds_status = k8_util.k8_watch_daemon_set_rollout(namespace) 
        
        if all(ds_status.values()) : 
            return 

        Logger.warning(f"Daemonset rollout did not complete in namespace '{namespace}'. Checking pod statuses...")
        ds_pods = [common.PodInfo(ds_name, gpu_nodes_count, 1) for ds_name in ds_status]
        unhealthy_pods = []
        status_summary = {}

        status_info = k8_util.k8_check_pod_status(namespace, ds_pods)
        for name, (phase, full_pod_info) in status_info.items():
            status_summary[name] = phase
            if  K8Helper.is_unhealthy_pod(full_pod_info):
                unhealthy_pods.append(full_pod_info)

        if unhealthy_pods and hasattr(environment, 'context') :
            setattr(environment.context, 'unhealthy_pods', unhealthy_pods )
            K8Helper.triage(environment, False, f"Daemonset rollout failed in '{namespace}': {len(unhealthy_pods)} pods are unhealthy.")

    @staticmethod
    def is_unhealthy_pod(pod: dict) -> bool:
        metadata = pod.get('metadata', {})
        status = pod.get("status", {})
        phase = status.get('phase', 'Unknown')
        name = metadata.get('name', '')

        if metadata.get('deletionTimestamp'):
            Logger.warning(f"Pod {name} is being deleted.")
            return True
        if phase in {'Failed', 'Unknown'}:
            Logger.error(f"Pod {name} is in critical phase: {phase}")
            return True

        bad_reasons = {'CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError', 
                    'CreateContainerError', 'RunContainerError', 'ContainerCreating'}
        
        container_statuses = status.get('initContainerStatuses', []) + status.get('containerStatuses', [])
        for cs in container_statuses:
            waiting = cs.get('state', {}).get('waiting', {})
            reason = waiting.get('reason')
            if reason in bad_reasons:
                Logger.error(f"Pod {name} container '{cs.get('name')}' error: {reason}")
                return True
        if phase == 'Pending':
            msg = next((c.get('message') for c in status.get('conditions', []) if c.get('type') == 'PodScheduled' and c.get('status') == 'False'), 
                    "Waiting for resources/init")
            Logger.error(f"Pod {name} is Pending: {msg}")
            return True
        if phase == 'Running':
            if not any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions', [])):
                Logger.error(f"Pod {name} is Running but not Ready.")
                return True
        return False

    @staticmethod
    def collect_unhealthy_pods(environment, pods):
        unhealthy_pods = []
        status_info = k8_util.k8_check_pod_status("default", pods)
        for name, (phase, full_pod_info) in status_info.items():
            if  K8Helper.is_unhealthy_pod(full_pod_info):
                unhealthy_pods.append(full_pod_info)

        if unhealthy_pods:
            setattr(environment.context, 'unhealthy_pods', unhealthy_pods)
        return unhealthy_pods

