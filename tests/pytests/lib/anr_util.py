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
import pprint
import logging
import lib.k8_util as k8_util
import lib.spec_util as spec_util
from lib.util import K8Helper
from datetime import datetime
from kubernetes import client, watch

Logger = logging.getLogger("lib.anr_util")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)  

def monitor_and_patch_remediation(environment, node_name, condition_type , timeout=800):
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("WorkflowWatcher")
    custom_api = client.CustomObjectsApi()
    watcher = watch.Watch()
    logger.info(f"Streaming events for workflow: {node_name}")
    condition_cleared = False
    for event in watcher.stream(
        custom_api.list_namespaced_custom_object,
        group="argoproj.io", 
        version="v1alpha1",
        namespace=environment.gpu_operator_namespace, 
        plural="workflows",
        timeout_seconds=timeout
    ):
    
        wf = event["object"]
        wf_name = wf['metadata']['name']
        status = wf.get('status', {})
        nodes = status.get('nodes', {})
        wf_phase = status.get('phase')

        if wf_phase in ['Failed', 'Error']:
            logger.error(f"Workflow {wf_name} failed with phase: {wf_phase}")
            watcher.stop()
            return -1 , None, f"Workflow terminal phase: {wf_phase}"

        for node_id, node_data in nodes.items():
            display_name = node_data.get('displayName')
            node_phase = node_data.get('phase')

            if node_phase in ['Failed', 'Error']:
                logger.error(f"Remediation node '{display_name}' failed!")
                watcher.stop()
                return -1 , None, f"Remediation node {display_name} is in {node_phase} phase."

            if display_name == "test" and node_phase == "Succeeded" :
                if not condition_cleared:
                    patch_node_condition(environment, node_name, condition_type = condition_type, condition_status = False)
                    condition_cleared = True

        if wf_phase == 'Succeeded':
            logger.info(f"All steps in workflow: {wf_name} completed successfully.")
            watcher.stop()
            return 0, "workflow completed", None

    return -1 , None, "Watcher timed out"

def cleanup_workflow(deviceconfig_install, environment, condition_type , config_overrides = None ):
    custom_cm_name = None
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        if tcfg.get('remediationWorkflow.config'):
            custom_cm_name = tcfg.get('remediationWorkflow.config')
            del tcfg['remediationWorkflow.config']
        if tcfg.get('remediationWorkflow.testerImage.repository') and tcfg.get('remediationWorkflow.testerImage.version'):
            del tcfg['remediationWorkflow.testerImage.repository'] 
            del tcfg['remediationWorkflow.testerImage.version'] 
        if config_overrides:
            for key, value in config_overrides.items():
                tcfg[key] = value
        else:
            tcfg['remediationWorkflow.enable'] = False

        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to modify deviceconfig: {ret_stderr}")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    for node in gpu_nodes:
        labels = node.get('metadata', {}).get('labels', {})
        if 'node-role.kubernetes.io/control-plane' in labels or 'node-role.kubernetes.io/master' in labels:
            continue
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        conditions = node.get('status', {}).get('conditions', [])
        hang_cond = next((c for c in conditions if c['type'] == condition_type), {})
        if hang_cond.get('status') == "True":
            patch_node_condition(environment, node_name, condition_type = condition_type, condition_status = False)

        has_taint = node.get('spec', {}).get('taints', [])
        if has_taint:
            k8_util.k8_untaint_node(node_name)

    ret_code, workflows, err = k8_util.k8_get_custom_resource_objects( group="argoproj.io", version="v1alpha1", plural="workflows")
    K8Helper.triage(environment, (ret_code == 0) , f"failed to get workflows in {environment.gpu_operator_namespace}")
    for wf in workflows:
        wf_name = wf['metadata']['name']
        ret_code, std_out, std_err = k8_util.k8_delete_custom_resource( group="argoproj.io",version="v1alpha1", plural="workflows",  namespace=environment.gpu_operator_namespace, name=wf_name)
        K8Helper.triage(environment, (ret_code == 0) , f"unable to delete workflows in {environment.gpu_operator_namespace}: {std_err}")

    if custom_cm_name:
        ret_code, _, ret_stderr = k8_util.k8_delete_configmap(environment.gpu_operator_namespace,  custom_cm_name)
        if ret_code != 0:
            Logger.warning(f"Failed to delete custom configmap {custom_cm_name}: {ret_stderr}")

def patch_node_condition(environment, node_name, condition_type, condition_status):
 
    if condition_status:
        condition_body = {
                        "status": {
                            "conditions": [
                                {   
                                    "type": condition_type,
                                    "lastTransitionTime": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                                    "message": "Simulated node condition",
                                    "reason": "Node Condition Detected",
                                    "status": "True",
                                }
                            ]
                        }
                    }   
    else:
        condition_body = {
                        "status": {
                            "conditions": [
                                {   
                                    "type": condition_type,
                                    "lastTransitionTime": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                                    "message": "",
                                    "reason": "Resolved",
                                    "status": "False",
                                }
                            ]
                        }
                    }   

    ret_code, resp , err = k8_util.k8_patch_node_status(node_name, condition_body)
    K8Helper.triage(environment, ret_code == 0 , f"failed to patch the node status : {err}")