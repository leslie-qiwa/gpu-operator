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
import time
import logging
import pprint
import lib.k8_util as k8_util
import lib.spec_util as spec_util
from lib.util import K8Helper
from datetime import datetime
from kubernetes import client, watch

Logger = logging.getLogger("k8.test_auto_node_remediation")
LogPrettyPrinter = pprint.PrettyPrinter(indent=2)
    
@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    if environment.deployment_mode == "openshift":
        pytest.skip(f"Skipping ANR deployment testcases for {environment.deployment_mode}")
    return

@pytest.fixture(scope="module")
def deviceconfig_install(gpu_cluster, images, gpu_operator_install, environment, request):
    global Logger

    # cleanup - remove any deviceconfigs
    def _deviceconfig_cleanup():
        devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
        for devcfg_name, _ in devcfg_map.items():
            ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(environment.gpu_operator_namespace, devcfg_name)
            if ret_code != 0:
                Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
        time.sleep(10)

    _deviceconfig_cleanup()
    request.addfinalizer(_deviceconfig_cleanup)

    class DeviceConfigCRInfo(object):
        pass

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    test_config = {
    'metadata.namespace' : environment.gpu_operator_namespace,
    'driver.enable' : True,
    }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(test_config, gpu_nodes, 'auto_node_remediation', environment.amdgpu_driver_spec)
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

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "exporter_port_map", exporter_port_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)
    yield devcfg_info

def wait_for_argo_workflow(namespace, target_node_name, timeout=300, max_retries=3):
    """
    Watches workflows for node. 
    Returns: (bool, str)
    """
    global Logger
    custom_api = client.CustomObjectsApi()
    watcher = watch.Watch()

    def _get_wf_failure_reason(wf_obj):
        """Internal helper to extract failure message from Argo tree."""
        status_dict = wf_obj.get('status', {})
        reason = status_dict.get('message', '')
        if not reason or "child" in reason:
            nodes = status_dict.get('nodes', {})
            failed_step = next((n for n in nodes.values() if n.get('phase') in ['Failed', 'Error']), {})
            reason = failed_step.get('message', 'Unknown failure in workflow steps')
        return reason
        
    attempt_count = 0
    wf_error = "No workflows appeared"

    Logger.info(f"Monitoring remediation for node: {target_node_name}")

    for event in watcher.stream(
        custom_api.list_namespaced_custom_object,
        group="argoproj.io", version="v1alpha1",
        namespace=namespace, plural="workflows",
        timeout_seconds=timeout
    ):
        wf = event["object"]
        wf_name = wf['metadata']['name']
        
        # Filter: Only care if the node name is in the workflow name
        if target_node_name not in wf_name:
            continue

        status_dict = wf.get('status', {})
        phase = status_dict.get('phase', 'Running')
        if phase == "Succeeded":
            watcher.stop()
            return 0, f"Success: {wf_name} completed.", ""

        if phase in ["Failed", "Error"]:
            attempt_count += 1
            wf_error = _get_wf_failure_reason(wf)
            Logger.warning(f"Attempt {attempt_count}/{max_retries} failed for {target_node_name}: {wf_error}")
            
            if attempt_count >= max_retries:
                watcher.stop()
                return -1, "", f"Remediation Failed. error: {wf_error}"

    return -1, "",  f"Timeout: Remediation did not complete. {wf_error}"

def verify_controller_logs(environment, log_msg_list, pod_str="controller-manager", since="120s"):
    global Logger
    global LogPrettyPrinter
    namespace = environment.gpu_operator_namespace

    i = 0
    ret_code, stdout, stderr = k8_util.k8_get_pod_logs(pod_str, namespace, since)

    for log_msg in log_msg_list:
        while log_msg not in stdout and i < 5:
            time.sleep(10)
            ret_code, stdout, stderr = k8_util.k8_get_pod_logs(pod_str, namespace, since)
            i += 1
        K8Helper.triage(environment, log_msg in stdout,
                            f"didn't find {log_msg} in logs for {pod_str}\n" + LogPrettyPrinter.pformat(stdout.split('\n')[-20:]))

def test_anr_workflow(gpu_cluster, images, deviceconfig_install, environment, request):
    global Logger
    def _cleanup_workflow():
        for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
            tcfg['remediationWorkflow.enable'] = False
            cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
            ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
            K8Helper.triage(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")

        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        for node in gpu_nodes:
            labels = node.get('metadata', {}).get('labels', {})
            if 'node-role.kubernetes.io/control-plane' in labels or 'node-role.kubernetes.io/master' in labels:
                continue
            node_name = node['metadata']['labels']['kubernetes.io/hostname']
            conditions = node.get('status', {}).get('conditions', [])
            hang_cond = next((c for c in conditions if c['type'] == "AMDGPUHwsHang"), {})
            if hang_cond.get('status') == "True":
                condition_body = {
                                    "status": {
                                        "conditions": [
                                            {   
                                                "type": "AMDGPUHwsHang",
                                                "message": "",
                                                "reason": "Resolved",
                                                "status": "False",
                                            }
                                        ]
                                    }
                                }   
                ret_code, resp, err = k8_util.k8_patch_node_status(node_name, condition_body)
                K8Helper.triage(environment, ret_code == 0 , f"failed to patch the node status : {resp}")

            has_taint = node.get('spec', {}).get('taints', [])
            if has_taint:
                k8_util.k8_untaint_node(node_name)

        ret_code, workflows, err = k8_util.k8_get_custom_resource_objects( group="argoproj.io", version="v1alpha1", plural="workflows")
        K8Helper.triage(environment, (ret_code == 0) , f"failed to get workflows in {environment.gpu_operator_namespace}")
        for wf in workflows:
            wf_name = wf['metadata']['name']
            ret_code, std_out, std_err = k8_util.k8_delete_custom_resource( group="argoproj.io",version="v1alpha1", plural="workflows",  namespace=environment.gpu_operator_namespace, name=wf_name)
            K8Helper.triage(environment, (ret_code == 0) , f"unable to delete workflows in {environment.gpu_operator_namespace}")

    request.addfinalizer(_cleanup_workflow)

    ret_code, pods = k8_util.k8_get_pods(environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to fetch GPU Operator pods in namespace {environment.gpu_operator_namespace}")

    devcfg_name = ''
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['remediationWorkflow.enable'] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")
        devcfg_name = cr_spec['metadata']['name']

    time.sleep(10)
    # check workflow template created
    ret_code, workflowtemplates, err = k8_util.k8_get_custom_resource_objects(group="argoproj.io", version="v1alpha1", plural="workflowtemplates")
    K8Helper.triage(environment, (ret_code == 0), f"Failed to list workflowtemplates from cluster: {err}")
    template_names = [t['metadata']['name'] for t in workflowtemplates or []]
    target_template = "default-template"
    K8Helper.triage(environment, target_template in template_names, f"Required WorkflowTemplate '{target_template}' not found in the cluster list")

    # check configmap created
    configmap_name = f"{devcfg_name}-default-conditional-workflow-mappings"
    ret_code, config_map, err = k8_util.k8_get_configmap(environment.gpu_operator_namespace, configmap_name )
    K8Helper.triage(environment, config_map  , f"Required configmap {configmap_name} was not created in {environment.gpu_operator_namespace}")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    # verify if node is clean 
    for node in gpu_nodes:

        labels = node.get('metadata', {}).get('labels', {})
        if 'node-role.kubernetes.io/control-plane' in labels or 'node-role.kubernetes.io/master' in labels:
            continue

        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        hang_status = False
        conditions = node.get('status', {}).get('conditions', [])
        for condition in conditions:
            if condition.get('type') == "AMDGPUHwsHang":
                if condition.get('status') == "True":
                    hang_status = True
                break

        if not hang_status :
            condition_body = {
                            "status": {
                                "conditions": [
                                    {   
                                        "type": "AMDGPUHwsHang",
                                        "lastTransitionTime": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                                        "message": "Simulated GPU hang",
                                        "reason": "GPUHangDetected",
                                        "status": "True",
                                    }
                                ]
                            }
                        }   
            ret_code, resp , err = k8_util.k8_patch_node_status(node_name, condition_body)
            K8Helper.triage(environment, ret_code == 0 , f"failed to patch the node status : {resp}")
            
        else :
            Logger.info(f"Node {node_name} already has AMDGPUHwsHang=True. Skipping patch.")

        ret_code, stdout, stderr = wait_for_argo_workflow(environment.gpu_operator_namespace, node_name)
        K8Helper.triage(environment, (ret_code == 0 ) , f"workflow status : {stderr}")

        verify_controller_logs(environment,
                    [   f"\"Node condition detected, triggering remediation\" node=\"{node_name}\"",
                        f"\"Workflow created\" workflow=\""                             
                    ],
                    )

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        taints = node.get('spec', {}).get('taints', [])
        K8Helper.triage(environment, not taints, f"Post-workflow check failed: Taint was not removed on {node_name}")
        conditions = node.get('status', {}).get('conditions', [])
        is_hung = any(c['type'] == "AMDGPUHwsHang" and c['status'] == "True" for c in conditions)
        K8Helper.triage(environment, not is_hung , f"Node {node_name} still reports GPU Hang condition")