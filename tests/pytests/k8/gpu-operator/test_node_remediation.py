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


def _get_step_phases(node_name):
    """Return {displayName: phase} for the most recent workflow for this node."""
    _, workflows, _ = k8_util.k8_get_custom_resource_objects(
        group="argoproj.io", version="v1alpha1", plural="workflows"
    )
    node_wfs = sorted(
        [w for w in (workflows or []) if node_name in w['metadata']['name']],
        key=lambda w: w['metadata']['creationTimestamp']
    )
    if not node_wfs:
        return {}
    return {
        n['displayName']: n.get('phase')
        for n in node_wfs[-1].get('status', {}).get('nodes', {}).values()
        if 'displayName' in n
    }


def _wait_for_step(node_name, step_name, expected_phase, timeout=120):
    """Poll until the named step reaches expected_phase in the most recent workflow for this node.
    Returns True if reached within timeout, False otherwise."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        phases = _get_step_phases(node_name)
        if phases.get(step_name) == expected_phase:
            return True
        time.sleep(5)
    return False


def _wait_for_workflow_terminal(node_name, timeout=300):
    """Poll until the most recent workflow for this node reaches a terminal state.
    Returns the terminal phase string, or None on timeout."""
    terminal = {'Succeeded', 'Failed', 'Error'}
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, workflows, _ = k8_util.k8_get_custom_resource_objects(
            "argoproj.io", "v1alpha1", "workflows"
        )
        node_wfs = sorted(
            [w for w in (workflows or []) if node_name in w['metadata']['name']],
            key=lambda w: w['metadata']['creationTimestamp']
        )
        if node_wfs:
            phase = node_wfs[-1].get('status', {}).get('phase')
            if phase in terminal:
                return phase
        time.sleep(10)
    return None


def _patch_sa_image_pull_secret(namespace, sa_name, secret_name):
    """Add secret_name to imagePullSecrets of the given ServiceAccount if not already present."""
    from kubernetes import client as k8s_client
    v1 = k8s_client.CoreV1Api()
    sa = v1.read_namespaced_service_account(sa_name, namespace)
    existing = [s.name for s in (sa.image_pull_secrets or [])]
    if secret_name not in existing:
        sa.image_pull_secrets = (sa.image_pull_secrets or []) + [k8s_client.V1LocalObjectReference(name=secret_name)]
        v1.patch_namespaced_service_account(sa_name, namespace, sa)


def _abort_workflow(node_name, namespace):
    """Label the node to abort its running workflow and remove the label after."""
    abort_label = {"amd.com/gpu.abort-workflow": "abort"}
    k8_util.k8_label_node(node_name, abort_label, overwrite=True)
    time.sleep(10)
    remove_label = {"amd.com/gpu.abort-workflow": None}
    k8_util.k8_label_node(node_name, remove_label, overwrite=True)


def _wait_and_check_label(node_name, label_key, timeout=120):
    """Poll until the applylabels step succeeds, then verify label is present on the node.
    Returns True if label found after applylabels succeeded, False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, workflows, _ = k8_util.k8_get_custom_resource_objects(
            "argoproj.io", "v1alpha1", "workflows"
        )
        active_wfs = [
            w for w in (workflows or [])
            if node_name in w['metadata']['name']
            and w.get('status', {}).get('phase') not in ('Succeeded', 'Failed', 'Error')
        ]
        for wf in active_wfs:
            for n in wf.get('status', {}).get('nodes', {}).values():
                if n.get('displayName') == 'applylabels' and n.get('phase') == 'Succeeded':
                    node_labels = k8_util.k8_get_node_labels(node_name) or {}
                    return label_key in node_labels
        time.sleep(5)
    return False
    
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
    
    # skipRebootStep=true and a fast-failing testerImage (gpu-operator-utils has no test runner
    # binary — container exits non-zero immediately, Job fails in ~25s). This keeps the test
    # short while still exercising all policy fields (labels, drain, suspend, recoveryPolicy).
    custom_mapping = [{
        "nodeCondition": condition_type,
        "notifyRemediationMessage": "Rerun the known failing workload.",
        "notifyTestFailureMessage": 'Remove the failing OAM (see OAM Removal and Installation)',
        "physicalActionNeeded": False,
        "skipRebootStep": True,
        "recoveryPolicy": {
            "maxAllowedRunsPerWindow": 1,
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
    
    remediation_label_key = "amd.com/remediating"
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['remediationWorkflow.enable'] = True
        tcfg['remediationWorkflow.config'] = configmap_name
        tcfg['remediationWorkflow.nodeRemediationLabels'] = {remediation_label_key: "true"}
        # Use gpu-operator-utils as testerImage — already present on nodes, exits non-zero
        # immediately (no test runner binary), so the test step fails fast (~25s)
        tcfg['remediationWorkflow.testerImage.repository'] = "docker.io/amdpsdo/gpu-operator-utils"
        tcfg['remediationWorkflow.testerImage.version'] = "latest"

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
        anr_util.patch_node_condition(environment, node_name, condition_type=condition_type, condition_status=True)

        # Verify nodeRemediationLabels applied during workflow (poll until applylabels step succeeds)
        label_present = _wait_and_check_label(node_name, remediation_label_key, timeout=120)
        K8Helper.triage(environment, label_present,
            f"nodeRemediationLabels '{remediation_label_key}' not found on {node_name} after applylabels step succeeded")

        # Verify drain completes — confirms taint+drain steps executed correctly
        drain_ok = _wait_for_step(node_name, 'drain', 'Succeeded', timeout=180)
        K8Helper.triage(environment, drain_ok,
            f"drain step did not succeed on {node_name} within timeout — step phases: {_get_step_phases(node_name)}")

        # Verify suspend auto-resumes — with physicalActionNeeded=false the controller must resume
        # within one reconcile cycle (~20s). A 120s timeout gives ample margin.
        # Failure here indicates the controller is blocking resume (e.g. recoveryPolicy check
        # firing on an in-flight workflow before physicalActionNeeded is evaluated).
        suspend_ok = _wait_for_step(node_name, 'suspend', 'Succeeded', timeout=120)
        K8Helper.triage(environment, suspend_ok,
            f"suspend step did not auto-resume on {node_name} — "
            f"physicalActionNeeded=false but suspend is still Running. "
            f"Step phases: {_get_step_phases(node_name)}")

        # Wait for workflow to reach terminal state — we don't assert success here because
        # the fast-fail testerImage will cause the test step to fail (by design).
        terminal_phase = _wait_for_workflow_terminal(node_name, timeout=300)
        K8Helper.triage(environment, terminal_phase is not None,
            f"workflow on {node_name} did not reach terminal state within timeout — "
            f"step phases: {_get_step_phases(node_name)}")

        # Verify recoveryPolicy blocks re-trigger: re-inject condition and confirm no new
        # workflow is created within two reconcile cycles (maxAllowedRunsPerWindow=1 exhausted).
        max_allowed = custom_mapping[0]['recoveryPolicy']['maxAllowedRunsPerWindow']
        anr_util.patch_node_condition(environment, node_name, condition_type=condition_type, condition_status=True)
        time.sleep(45)  # two reconcile cycles (20s each) + buffer
        _, workflows, _ = k8_util.k8_get_custom_resource_objects("argoproj.io", "v1alpha1", "workflows")
        node_wf_count = sum(1 for w in (workflows or []) if node_name in w['metadata']['name'])
        K8Helper.triage(environment, node_wf_count <= max_allowed,
            f"recoveryPolicy not enforced: {node_wf_count} workflows created for {node_name}, "
            f"expected at most {max_allowed} within the recovery window")
        anr_util.patch_node_condition(environment, node_name, condition_type=condition_type, condition_status=False)

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


def test_max_parallel_workflows(gpu_cluster, deviceconfig_install, environment, request):
    """Verify maxParallelWorkflows limits the number of concurrently Running workflows.
    Requires at least 2 GPU worker nodes — skipped on single-node clusters.
    """
    global Logger
    CONDITION = "AMDGPUHwsHang"
    MAX_PARALLEL = 1
    clean_params = {'remediationWorkflow.enable': False}
    request.addfinalizer(lambda: anr_util.cleanup_workflow(
        deviceconfig_install, environment, CONDITION, clean_params))

    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['remediationWorkflow.enable'] = True
        tcfg['remediationWorkflow.maxParallelWorkflows'] = MAX_PARALLEL
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, err = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to patch DeviceConfig: {err}")

    time.sleep(10)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster")
    worker_nodes = [
        n for n in gpu_nodes
        if 'node-role.kubernetes.io/control-plane' not in n.get('metadata', {}).get('labels', {})
        and 'node-role.kubernetes.io/master' not in n.get('metadata', {}).get('labels', {})
    ]
    if len(worker_nodes) < 2:
        pytest.skip("test_max_parallel_workflows requires at least 2 GPU worker nodes")

    # Inject condition on all worker nodes simultaneously
    for node in worker_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        anr_util.patch_node_condition(environment, node_name, condition_type=CONDITION, condition_status=True)

    time.sleep(10)  # give controller time to react

    ret_code, workflows, err = k8_util.k8_get_custom_resource_objects(
        group="argoproj.io", version="v1alpha1", plural="workflows")
    K8Helper.triage(environment, (ret_code == 0), f"Failed to list workflows: {err}")

    running = [w for w in (workflows or []) if w.get('status', {}).get('phase') == 'Running']
    pending = [w for w in (workflows or []) if w.get('status', {}).get('phase') == 'Pending']
    Logger.info(f"maxParallelWorkflows={MAX_PARALLEL}: Running={len(running)}, Pending={len(pending)}")

    K8Helper.triage(environment, len(running) <= MAX_PARALLEL,
        f"maxParallelWorkflows={MAX_PARALLEL} violated: {len(running)} Running workflows found")
    K8Helper.triage(environment, len(pending) >= len(worker_nodes) - MAX_PARALLEL,
        f"Expected {len(worker_nodes) - MAX_PARALLEL} Pending workflow(s), got {len(pending)}")


def test_skip_reboot_step(gpu_cluster, deviceconfig_install, environment, request):
    """Verify skipRebootStep=true causes the reboot step to be Skipped in the workflow.

    Uses amdpsdo-secret for test runner image pull and aborts the workflow after
    verifying the reboot step — no need to wait for full AGFHC completion.
    """
    global Logger
    CONDITION = "AMDGPUHwsHang"
    CONFIGMAP_NAME = "skip-reboot-test-cm"
    TEST_RUNNER_SA = "amd-gpu-operator-test-runner"
    PULL_SECRET = "amdpsdo-secret"
    clean_params = {'remediationWorkflow.enable': False}
    request.addfinalizer(lambda: anr_util.cleanup_workflow(
        deviceconfig_install, environment, CONDITION, clean_params))

    # Patch the test runner SA with the pull secret so the test runner image can be pulled
    _patch_sa_image_pull_secret(environment.gpu_operator_namespace, TEST_RUNNER_SA, PULL_SECRET)

    skip_reboot_mapping = [{
        "nodeCondition": CONDITION,
        "physicalActionNeeded": False,
        "skipRebootStep": True,
        "validationTestsProfile": {
            "framework": "AGFHC",
            "recipe": "all_lvl1",
            "iterations": 1,
            "stopOnFailure": True,
            "timeoutSeconds": 4800,
        },
        "workflowTemplate": "default-template"
    }]

    configmap_file = os.path.join(environment.logdir, f"{CONFIGMAP_NAME}.yaml")
    with open(configmap_file, "w") as fp:
        yaml.dump(skip_reboot_mapping, fp, default_flow_style=False)

    k8_util.k8_delete_configmap(environment.gpu_operator_namespace, CONFIGMAP_NAME)
    ret_code, _, err = k8_util.k8_create_configmap(environment.gpu_operator_namespace, CONFIGMAP_NAME, configmap_file)
    K8Helper.triage(environment, (ret_code == 0), f"Failed to create configmap: {err}")

    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg['remediationWorkflow.enable'] = True
        tcfg['remediationWorkflow.config'] = CONFIGMAP_NAME
        cr_spec = spec_util.generate_k8_deviceconfig_cr(environment.gpu_operator_version, tcfg)
        ret_code, _, err = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        K8Helper.triage(environment, (ret_code == 0), f"Failed to patch DeviceConfig: {err}")

    time.sleep(10)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Failed to get GPU nodes")
    worker_nodes = [
        n for n in gpu_nodes
        if 'node-role.kubernetes.io/control-plane' not in n.get('metadata', {}).get('labels', {})
        and 'node-role.kubernetes.io/master' not in n.get('metadata', {}).get('labels', {})
    ]
    K8Helper.triage(environment, len(worker_nodes) > 0, "No GPU worker nodes found")

    node = worker_nodes[0]
    node_name = node['metadata']['labels']['kubernetes.io/hostname']

    anr_util.patch_node_condition(environment, node_name, condition_type=CONDITION, condition_status=True)

    # Wait for drain to complete (confirms workflow started and progressed)
    drain_ok = _wait_for_step(node_name, 'drain', 'Succeeded', timeout=180)
    K8Helper.triage(environment, drain_ok,
        f"drain step did not succeed on {node_name} — step phases: {_get_step_phases(node_name)}")

    # Wait for suspend to auto-resume (physicalActionNeeded=false)
    suspend_ok = _wait_for_step(node_name, 'suspend', 'Succeeded', timeout=120)
    K8Helper.triage(environment, suspend_ok,
        f"suspend step did not auto-resume on {node_name} — step phases: {_get_step_phases(node_name)}")

    # Core assertion: reboot step must be Skipped (skipRebootStep=true)
    reboot_skipped = _wait_for_step(node_name, 'reboot', 'Skipped', timeout=60)
    K8Helper.triage(environment, reboot_skipped,
        f"reboot step is not Skipped on {node_name} — skipRebootStep=true was not respected. "
        f"Step phases: {_get_step_phases(node_name)}")

    Logger.info(f"skipRebootStep verified: reboot step is Skipped on {node_name}")

    # Abort — no need to wait for full AGFHC run
    _abort_workflow(node_name, environment.gpu_operator_namespace)
    anr_util.patch_node_condition(environment, node_name, condition_type=CONDITION, condition_status=False)