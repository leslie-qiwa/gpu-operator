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
import yaml
import pytest
import os
import time
import logging
import pprint
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.anr_util as anr_util
from lib.util import K8Helper

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
  
def test_anr_workflow(gpu_cluster, images, deviceconfig_install, environment, request):
    global Logger
    clean_params = {
        'remediationWorkflow.enable' : False,
    }
    request.addfinalizer(lambda: anr_util.cleanup_workflow(deviceconfig_install, environment, "AMDGPUHwsHang", clean_params))

    ret_code, pods = k8_util.k8_get_pods(environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to fetch GPU Operator pods in namespace {environment.gpu_operator_namespace}")

    devcfg_name = ''
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['remediationWorkflow.enable'] = True
        tcfg['remediationWorkflow.testerImage.repository'] = images['testRunnerAgfhc.image.repository']
        tcfg['remediationWorkflow.testerImage.version'] =  images['testRunnerAgfhc.image.version']
        
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

    # check configmap created and change recipe
    configmap_name = f"{devcfg_name}-default-conditional-workflow-mappings"
    ret_code, config_map, err = k8_util.k8_get_configmap(environment.gpu_operator_namespace, configmap_name )
    K8Helper.triage(environment, config_map  , f"Required configmap {configmap_name} was not created in {environment.gpu_operator_namespace}")
    condition_type = "AMDGPUHwsHang"
    patch_body = {
        "nodeCondition": condition_type,
        "validationTestsProfile": {
            "recipe": "all_lvl1",
            "timeoutSeconds": 600,
        },
        "skipRebootStep" : True,
    }
    ret_code, resp, err = k8_util.k8_patch_workflow_config(environment.gpu_operator_namespace, configmap_name, patch_body)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to modify configmap : {err}")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

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
            anr_util.patch_node_condition(environment, node_name, condition_type = condition_type, condition_status=True)
        else :
            Logger.info(f"Node {node_name} already has AMDGPUHwsHang=True. Skipping patch.")

        ret_code, stdout, stderr = anr_util.monitor_and_patch_remediation(environment, node_name, condition_type = condition_type)
        K8Helper.triage(environment, (ret_code == 0 ) , f"workflow status - {stderr}")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    for node in gpu_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        taints = node.get('spec', {}).get('taints', [])
        K8Helper.triage(environment, not taints, f"Post-workflow check failed: Taint was not removed on {node_name}")
        conditions = node.get('status', {}).get('conditions', [])
        is_hung = any(c['type'] == "AMDGPUHwsHang" and c['status'] == "True" for c in conditions)
        K8Helper.triage(environment, not is_hung , f"Node {node_name} still reports GPU Hang condition")

def test_custom_configmap(gpu_cluster, images, deviceconfig_install, environment, request):
    global Logger
    clean_params = {
        'remediationWorkflow.enable' : False,
    }
    request.addfinalizer(lambda: anr_util.cleanup_workflow(deviceconfig_install, environment,  "AMDGPUBootFailed", clean_params))

    ret_code, pods = k8_util.k8_get_pods(environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to fetch GPU Operator pods in namespace {environment.gpu_operator_namespace}")
    
    configmap_name = "custom-remediation-config"
    configmap_file = os.path.join(environment.logdir, f"{configmap_name}.yaml")
    condition_type = "AMDGPUBootFailed"
    
    custom_mapping = [{
        "nodeCondition": condition_type,
        "notifyRemediationMessage": "Rerun the known failing workload.",
        "notifyTestFailureMessage": 'Remove the failing OAM (see OAM Removal and Installation)',
        "physicalActionNeeded": False,
        "recoveryPolicy": {
            "maxAllowedRunsPerWindow": 2,
            "windowSize": "15m",
        },
        "validationTestsProfile": {
            "framework": "RVS",
            "iterations": 1,
            "recipe": "levels/rvs_level_1",
            "stopOnFailure": True,
            "timeoutSeconds": 320,
        },
        "workflowTemplate": "default-template"
    }]

    with open(configmap_file, "w") as fp:
        yaml.dump(custom_mapping, fp, default_flow_style=False)

    k8_util.k8_delete_configmap(environment.gpu_operator_namespace, configmap_name)
    ret_code, ret_stdout, ret_stderr = k8_util.k8_create_configmap(environment.gpu_operator_namespace, configmap_name, configmap_file)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create configmap {configmap_name} for {configmap_file}, err: {ret_stderr.strip()}")
    
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['remediationWorkflow.enable'] = True
        tcfg['remediationWorkflow.config'] = configmap_name
        
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")
    
    time.sleep(10)
    ret_code, workflowtemplates, err = k8_util.k8_get_custom_resource_objects(group="argoproj.io", version="v1alpha1", plural="workflowtemplates")
    K8Helper.triage(environment, (ret_code == 0), f"Failed to list workflowtemplates from cluster: {err}")
    template_names = [t['metadata']['name'] for t in workflowtemplates or []]
    K8Helper.triage(environment, "default-template" in template_names, f"Required WorkflowTemplate  not found in the cluster list")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    for node in gpu_nodes:
        labels = node.get('metadata', {}).get('labels', {})
        if 'node-role.kubernetes.io/control-plane' in labels or 'node-role.kubernetes.io/master' in labels:
            continue
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        anr_util.patch_node_condition(environment, node_name, condition_type = condition_type, condition_status=True)

        ret_code, stdout, stderr = anr_util.monitor_and_patch_remediation(environment, node_name, condition_type = condition_type)
        K8Helper.triage(environment, (ret_code == 0 ) , f"workflow status - {stderr}")

def test_disable_autoStartWorkflow(gpu_cluster, images, deviceconfig_install, environment, request):
    global Logger
    clean_params = {
        'remediationWorkflow.enable' : False,
        'remediationWorkflow.autoStartWorkflow':  True,
    }
    request.addfinalizer(lambda: anr_util.cleanup_workflow(deviceconfig_install, environment,"AMDGPUUnhealthy", clean_params))

    ret_code, pods = k8_util.k8_get_pods(environment.gpu_operator_namespace)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to fetch GPU Operator pods in namespace {environment.gpu_operator_namespace}")

    devcfg_name = ''
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['remediationWorkflow.enable'] = True
        tcfg['remediationWorkflow.autoStartWorkflow'] = False
        tcfg['remediationWorkflow.testerImage.repository'] = images['testRunnerAgfhc.image.repository']
        tcfg['remediationWorkflow.testerImage.version'] =  images['testRunnerAgfhc.image.version']
        
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to create deviceconfig, stderr: {ret_stderr}")
        devcfg_name = cr_spec['metadata']['name']

    time.sleep(10)
    configmap_name = f"{devcfg_name}-default-conditional-workflow-mappings"
    ret_code, config_map, err = k8_util.k8_get_configmap(environment.gpu_operator_namespace, configmap_name )
    K8Helper.triage(environment, config_map  , f"Required configmap {configmap_name} was not created in {environment.gpu_operator_namespace}")
    condition_type = "AMDGPUUnhealthy"
    patch_body = {
        "nodeCondition": condition_type,
        "validationTestsProfile": {
            "recipe": "all_lvl1",
            "timeoutSeconds": 320,
        },
    }
    ret_code, resp, err = k8_util.k8_patch_workflow_config(environment.gpu_operator_namespace, configmap_name, patch_body)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to modify configmap : {err}")

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    K8Helper.triage(environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster")

    for node in gpu_nodes:
        labels = node.get('metadata', {}).get('labels', {})
        if 'node-role.kubernetes.io/control-plane' in labels or 'node-role.kubernetes.io/master' in labels:
            continue
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        anr_util.patch_node_condition(environment, node_name, condition_type = condition_type, condition_status = True)

        ret_code, stdout, stderr = anr_util.monitor_and_patch_remediation(environment, node_name, condition_type = condition_type , timeout = 60)
        K8Helper.triage(environment, (ret_code == 0 ) , f"workflow appeared when autostartworkflow is disabled.")

        labels_dict = { "operator.amd.com/gpu-force-resume-workflow" : "true",}
        k8_util.k8_label_node(node_name, labels_dict, overwrite=True)

        ret_code, stdout, stderr = anr_util.monitor_and_patch_remediation(environment, node_name, condition_type = condition_type )
        K8Helper.triage(environment, (ret_code == 0 ) , f"workflow status - {stderr}")