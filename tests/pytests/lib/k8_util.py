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

import os
import pdb
import time
import json
import logging
import pytest
import base64
import pprint
import datetime
import yaml
from functools import wraps
from collections import defaultdict
from typing import List, Dict
from kubernetes import client, config, stream, watch
from kubernetes.client.rest import ApiException
from urllib3.exceptions import ProtocolError, IncompleteRead
import lib.common as common

Logger = logging.getLogger("lib.k8util")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)

def log_arguments(func):
    global Logger
    global LogPrettyPrinter

    @wraps(func)
    def wrapper(*args, **kwargs):
        Logger.debug(f"Function::'{func.__name__}' with args: {args} kwargs: {kwargs}")
        return func(*args, **kwargs)
    return wrapper

K8Items = List[dict]

def k8_lib_init(k8_kube_config : str) -> None:
    global Logger
    # Load Kubernetes configuration
    try:
        config.load_kube_config(config_file = k8_kube_config)
    except config.ConfigException as e:
        pytest.fail(f"failed to load kube-config, error : {e}")

    # retrieve with worker-node information
    ret_code, k8_nodes = k8_get_nodes()
    for node in k8_nodes:
        node_name = node['metadata']['labels']['kubernetes.io/hostname']
        k8_untaint_node(node_name, effects=["NoSchedule", "NoExecute"])
    assert ret_code == 0, f"Failed to collect worker nodes from k8/cluster"

def k8_init_cluster(k8_cluster : common.k8_cluster, namespaces):
    if k8_cluster.k8_secrets:
        # create secrets, but first create the non-default namespace(s) listed in each entry
        for namespace in namespaces:
            ret_code, ret_stdout, ret_stderr = k8_create_namespace(namespace)
            if ret_code != 0:
                Logger.debug(f"Failed to create namespace {namespace}, error: {ret_stderr}")

            for entry in k8_cluster.k8_secrets["secrets"]:
                ret_code, ret_stdout, ret_stderr = k8_delete_secret(entry.get("name"),
                                                                    entry.get("type"), namespace)
                if ret_code != 0:
                    Logger.warn(f"secret deletion failed, code: {ret_code}, stdout: {ret_stdout}, stderr: {ret_stderr}")
                ret_code, ret_stdout, ret_stderr = k8_create_secret(entry.get("name"),
                                                                    entry.get("type"),
                                                                    username = entry.get("username"),
                                                                    password = entry.get("password"),
                                                                    namespace = namespace)
                if ret_code != 0:
                    Logger.error(f"secret create failed, code: {ret_code}, stdout: {ret_stdout}, stderr: {ret_stderr}")
                    pytest.fail(f"failed to create secret type {entry.get('name')} - Abort")
    return

@log_arguments
def k8_get_version():
    """
    Get Kubernetes cluster version information

    Returns:
        tuple: (ret_code, version_info_dict) where version_info_dict contains:
            - major: major version
            - minor: minor version
            - git_version: full git version string
    """
    global Logger

    try:
        version_api = client.VersionApi()
        version_info = version_api.get_code()

        return 0, {
            "major": version_info.major,
            "minor": version_info.minor,
            "git_version": version_info.git_version,
            "platform": version_info.platform,
        }
    except Exception as e:
        Logger.error(f"Failed to get Kubernetes version: {e}")
        return 1, {}

@log_arguments
def k8_get_nodes() -> (int, str, K8Items):
    """
    API to get nodes from k8 cluster

    Parameters:
    k8_kube_config : path to kube config

    Returns:
    list of dict. For example refer to output of 'kubectl get nodes -o json | jq .items'
    """
    global Logger
    api = client.CoreV1Api()
    try:
        nodes = api.list_node().to_dict()
        return 0, nodes.get('items', None)
    except ApiException as ae:
        Logger.error(f"Failed to collect nodes, error : {ae}")
        return -1, None
    except Exception as e:
        Logger.error(f"Unexpected failure while collecting nodes, error : {e}")
        return -1, None

@log_arguments
def k8_get_gpu_nodes(skip_not_ready : bool = True) -> (int, K8Items):
    """
    API to get nodes from k8 cluster which have 'feature.node.kubernetes.io/amd-gpu : true'

    Parameters:
    skip_not_ready : bool, skip nodes which are not ready

    Returns:
    list of dict. For example refer to output of 'kubectl get nodes -o json | jq .items'
    """
    global Logger
    global LogPrettyPrinter
    ret_code, k8_nodes = k8_get_nodes()
    if ret_code != 0:
        return ret_code, None
    #Logger.debug(f"Nodes : \n{LogPrettyPrinter.pformat(k8_nodes)}")

    feature_labels = ['feature.node.kubernetes.io/amd-gpu', 'feature.node.kubernetes.io/amd-vgpu']

    k8_gpu_nodes = list()
    for node in k8_nodes:
        gpu_node = False
        for lbl in feature_labels:
            if lbl in node['metadata']['labels']:
                if node['metadata']['labels'][lbl] == 'true':
                    gpu_node = True
                    break
        if not gpu_node:
            continue

        if skip_not_ready:
            ready_condition = list(filter(lambda x: x.get('type', 'NotReady') == 'Ready', node['status']['conditions']))
            assert len(ready_condition) == 1, 'Failed to find Ready condition for node'

            if ready_condition[0]['status'] != 'True':
                continue
        k8_gpu_nodes.append(node)

    return ret_code, k8_gpu_nodes

@log_arguments
def k8_get_node_gpu_capacity(node_name : str) -> (int, int):
    """
    API to get the node's status.capacity and status.allocatable values of gpu

    Parameters:
    node_name  : name of the node

    Returns:
    gpu_capacity
    gpu_allocatable
    """
    ret_code, gpu_nodes = k8_get_gpu_nodes()
    filtered_list = list(filter(lambda x: x['metadata']['name'] == node_name, gpu_nodes))
    assert len(filtered_list) == 1, f"No such cluster-node exists : {node_name}"
    node = filtered_list[0]
    gpu_capacity = node['status']['capacity'].get("amd.com/gpu", -1)
    gpu_allocatable = node['status']['allocatable'].get("amd.com/gpu", -1)
    return(int(gpu_capacity), int(gpu_allocatable))

@log_arguments
def k8_get_node_gpu_allocatable(node_name: str) -> str:
    ret_code, gpu_nodes = k8_get_gpu_nodes()
    filtered_list = list(filter(lambda x: x['metadata']['name'] == node_name, gpu_nodes))
    assert len(filtered_list) == 1, f"No such cluster-node exists: {node_name}"

    node = filtered_list[0]

    for gpu_type in node['status']['allocatable'].keys():
        if node['status']['allocatable'][gpu_type] != '0':
            return gpu_type
    return "amd.com/gpu"


@log_arguments
def k8_get_pods(namespace, node_name = None, pod_name_pattern = None) -> (int, List):
    """
    API to get all pods for a given namespace from a k8 cluster
    """
    global Logger
    try:
        api = client.CoreV1Api()
        if namespace:
            pod_info = api.list_namespaced_pod(namespace = namespace).to_dict()
        else:
            pod_info = api.list_pod_for_all_namespaces().to_dict()

        sel_pods = pod_info['items']
        if node_name:
            sel_pods = list(filter(lambda x: x['spec']['node_name'] == node_name, sel_pods))
        if pod_name_pattern:
            sel_pods = list(filter(lambda x: pod_name_pattern in x['metadata']['name'], sel_pods))
        return 0, sel_pods
    except ApiException as e:
        Logger.error(f"Failed to list all pods for give namespace {namespace} error : {e}")
        return -1, None

@log_arguments
def k8_get_endpoints(namespace):
    """
    API to get endpoints from a k8 cluster for a given namespace and filtered by service-name
    """
    global Logger
    global LogPrettyPrinter
    ret_values = defaultdict(list)
    ret_code = -1
    api = client.CoreV1Api()
    try:
        k8_endpoint_info = api.list_endpoints_for_all_namespaces().to_dict()
        Logger.debug(f"List Endpoints, resp:\n{LogPrettyPrinter.pformat(k8_endpoint_info)}")
        endpoints = list(filter(lambda x: x['metadata']['namespace'] == namespace, k8_endpoint_info.get("items", list())))
        ret_code = 0
    except ApiException as e:
        Logger.error(f"Failed to collect endpoints, error: {e}")
        return -1, None
    for item in endpoints:
        service_name = item["metadata"]["name"]
        subset_infos = item.get("subsets", [])
        if subset_infos:
            for subset in subset_infos:
                port = subset['ports'][0]['port']
                for address in subset['addresses']:
                    ip_address = address['ip']
                    host = address['node_name']
                    ret_values[service_name].append((host, ip_address, port))
    return ret_code, ret_values

@log_arguments
def k8_create_custom_resource(cr_spec : dict) -> (int, str, str):
    """
    API to create custom-resource on a K8 cluster.
    """
    global Logger

    custom_objects_api = client.CustomObjectsApi()
    # Read cr_file and derive: group, version, plural and name
    group, version = cr_spec['apiVersion'].split('/')
    plural = cr_spec['kind'].lower() + 's'
    try:
        if 'namespace' in cr_spec['metadata']:
            namespace = cr_spec['metadata']['namespace'] # TODO: If namespace is not defined, then use different/default API
            resp = custom_objects_api.create_namespaced_custom_object(group, version, namespace, plural, cr_spec)
        else:
            resp = custom_objects_api.create_cluster_custom_object(group, version, plural, cr_spec)
    except ApiException as e:
        Logger.error(f"Failed to create deviceconfig-cr, error: {e}")
        return -1, "", str(e)
    except Exception as e:
        Logger.error(f"Failed to create deviceconfig-cr, error: {e}")
        return -1, "", str(e)
    return 0, "", ""

k8_create_deviceconfig_cr = k8_create_custom_resource

@log_arguments
def k8_modify_deviceconfig_cr(cr_spec : dict) -> (int, str, str):
    """
    API to modify custom-resource on a K8 cluster.
    """
    global Logger

    custom_objects_api = client.CustomObjectsApi()
    # Read cr_file and derive: group, version, plural and name
    group, version = cr_spec['apiVersion'].split('/')
    plural = cr_spec['kind'].lower() + 's'
    namespace = cr_spec['metadata']['namespace']
    devcfg_name = cr_spec['metadata']['name']
    resp = None
    error = None
    retcode = -1
    for _ in range(5):
        try:
            devcfg_obj = custom_objects_api.get_namespaced_custom_object(group = group, version = version, namespace = namespace, 
                                                                         plural = plural, name = devcfg_name)
            # Modify devcfg_obj['spec']
            devcfg_obj['spec'] = cr_spec['spec']
            resp = custom_objects_api.replace_namespaced_custom_object(group = group, version = version, namespace = namespace,
                                                                       plural = plural, name = devcfg_name, body=devcfg_obj)
            Logger.debug(f"Modified devcfg_obj: {devcfg_obj}")
            retcode = 0
            break
        except ApiException as e:
            Logger.error(f"Failed to modify deviceconfig {devcfg_name}, Exception: {e}")
            time.sleep(5)
            error = str(e)
    else:
        Logger.error(f"Unable to modify deviceconfig {devcfg_name} - aborting")
    return retcode, "", error

@log_arguments
def k8_apply_cr(cr_spec : dict, cr_file : str) -> (int, str, str):
    """
    API to create custom-resource on a K8 cluster.
    """
    global Logger

    namespace = cr_spec.get('metadata').get('namespace')
    api = client.CoreV1Api()
    try:
        result = api.create_namespaced_pod(namespace, body=cr_spec)
    except ApiException as e:
        assert True, f"Failed to start pod\n{cr_spec}\n{str(e)}\n{result}"
        return -1, "", str(e)
    return 0, "", ""

@log_arguments
def k8_delete_custom_resource(group : str, version : str, plural : str, namespace : str, name : str) -> (int, str, str):
    """
    API to delete CR with given group, vesion, plural, namespace and name
    """
    global Logger
    custom_objects_api = client.CustomObjectsApi()
    # check if it exists:
    entry = None
    try:
        cr_info = custom_objects_api.list_cluster_custom_object(group = group, version = version, plural = plural)
        for item in cr_info["items"]:
            if name == item["metadata"]["name"]:
                entry = item
                break
    except ApiException as e:
        Logger.error(f"Failed to query CR, error: {e}")

    if entry is None:
        Logger.warn(f"CustomResource of type {plural} with name {name} does not exists")
        return 0, "", ""
    try:
        if entry["metadata"].get("namespace", None):
            resp = custom_objects_api.delete_namespaced_custom_object(group=group,
                                                                      version=version,
                                                                      namespace=entry["metadata"]["namespace"],
                                                                      plural=plural,
                                                                      name=name,
                                                                      body=client.V1DeleteOptions())
        else:
            resp = custom_objects_api.delete_cluster_custom_object(group=group,
                                                                   version=version,
                                                                   plural=plural,
                                                                   name=name,
                                                                   body=client.V1DeleteOptions())
        if resp.get("status") == "Success":
            Logger.debug(f"CR {name} deletion successful")
        else:
            Logger.debug(resp)
    except ApiException as e:
        Logger.error(f"Failed to delete CR, error: {e}")
        return -1, "", str(e)

    # Wait till resources are removed
    for _ in range(10):
        found = False
        try:
            cr_info = custom_objects_api.list_cluster_custom_object(group = group, version = version, plural = plural)
            for item in cr_info["items"]:
                if name == item["metadata"]["name"]:
                    found = True
        except ApiException as e:
            pass

        if not found:
            break
        time.sleep(5)
    return 0, "", ""

@log_arguments
def k8_delete_deviceconfig_cr(namespace : str, name : str) -> (int, str, str):
    """
    API to delete deviceconfig CR with given name and namespace
    """
    global Logger
    return k8_delete_custom_resource("amd.com", "v1alpha1", "deviceconfigs", namespace, name)

@log_arguments
def k8_delete_cr(cr_spec, cr_file):
    """
    API to delete CR with given spec (dict)
    """
    global Logger

    api = client.CoreV1Api()
    try:
        api.delete_namespaced_pod(name=cr_spec.get('metadata').get('name'),
                                  namespace=cr_spec.get('metadata').get('namespace'))
    except ApiException as e:
        return -1, "", str(e)
    return 0, "", ""

@log_arguments
def k8_get_custom_resource_objects(group : str, version : str, plural : str) -> (int, str, str):
    """
    API to get list of CR
    """
    global Logger
    custom_objects_api = client.CustomObjectsApi()
    try:
        cr_info = custom_objects_api.list_cluster_custom_object(group = group, version = version,
                                                                plural = plural)
        return 0, cr_info['items'], None
    except ApiException as e:
        Logger.error(f"Failed to query deviceconfig CR, error: {e}")
        return -1, None, str(e)

@log_arguments
def k8_get_namespaced_custom_resource(group : str, version : str, plural : str, namespace : str, name : str) -> (int, dict, str):
    """
    API to get a single namespaced custom resource by name

    Parameters:
        group: API group (e.g., 'resource.k8s.io')
        version: API version (e.g., 'v1' or 'v1beta1')
        plural: Resource plural name (e.g., 'resourceclaims')
        namespace: Namespace of the resource
        name: Name of the resource

    Returns:
        tuple: (return_code, resource_dict, error_message)
    """
    global Logger
    custom_objects_api = client.CustomObjectsApi()
    try:
        cr_info = custom_objects_api.get_namespaced_custom_object(
            group = group,
            version = version,
            namespace = namespace,
            plural = plural,
            name = name
        )
        return 0, cr_info, None
    except ApiException as e:
        Logger.error(f"Failed to get {plural}/{name} in namespace {namespace}, error: {e}")
        return -1, None, str(e)

def k8_get_servicemonitor_cr(namespace : str) -> (int, str, str):
    """
    API to delete deviceconfig CR with given name and namespace
    """
    global Logger
    group = 'monitoring.coreos.com'
    version = 'v1'
    plural = 'servicemonitors'
    ret_code, cr_list, err = k8_get_custom_resource_objects(group, version, plural)
    if ret_code == 0:
        return ret_code, list(filter(lambda x: x['metadata']['namespace'] == namespace, cr_list)), err
    return ret_code, cr_list, err

@log_arguments
def k8_create_rules_from_endpoint_list(endpoint_verbs : List):
    rules = []
    for url_verb in endpoint_verbs:
        url, verb = url_verb
        rules.append(client.V1PolicyRule(non_resource_ur_ls = [url], verbs=[verb]))
    return rules

@log_arguments
def k8_create_rules_from_verbs(resources, verbs, api_groups=[""]):
    return client.V1PolicyRule(
        api_groups=api_groups,
        resources=resources,
        verbs=verbs
    )

@log_arguments
def k8_create_cluster_role(cluster_role_name : str, rules : List) -> (int, str, str):
    """
    API to create a cluster-role with specific endpoint and corresponding verb

    Parameters:
    cluster_role_name : name of cluster-role
    endpoint_verbs : list of tuple of (endpoint, verb)

    Returns:
    ret_code (int) : 0 for success else failure
    stdout (str) : stdout
    stderr (str) : stderr
    """
    global Logger
    api = client.RbacAuthorizationV1Api()

    cluster_role = client.V1ClusterRole(
            api_version="rbac.authorization.k8s.io/v1",
            kind = "ClusterRole",
            metadata = client.V1ObjectMeta(name=cluster_role_name),
            rules=rules)
    try:
        api_response = api.create_cluster_role(cluster_role)
        return 0, "", ""
    except ApiException as e:
        return -1, "", str(e)

def k8_create_role_binding_generic(crb_name: str, cluster_role_name: str, subject_kind: str, subject_name: str, namespace: None,) -> (int, str, str):
    """
    API to create cluster-role-binding

    Parameters:
    crb_name : cluster-role-binding name
    namespace : reader namespace
    cluster_role_name : cluster-role name
    sa_name : service-account name

    Returns:
    int : 0 on success else failure
    str : stdout
    str : stderr
    kwargs["name"]
    kwargs["namespace"]
    """
    global Logger
    subject_kwargs = {"kind": subject_kind, "name": subject_name}
    if subject_kind == "ServiceAccount" and namespace:
        subject_kwargs["namespace"] = namespace

    cluster_role_binding = client.V1ClusterRoleBinding(
        api_version="rbac.authorization.k8s.io/v1",
        kind="ClusterRoleBinding",
        metadata=client.V1ObjectMeta(name=crb_name),
        subjects=[client.RbacV1Subject(**subject_kwargs)],
        
        role_ref=client.V1RoleRef(
            kind="ClusterRole",
            name=cluster_role_name,
            api_group="rbac.authorization.k8s.io",
        ),
    )
    api = client.RbacAuthorizationV1Api()
    try:
        # Create the ClusterRoleBinding
        api.create_cluster_role_binding(cluster_role_binding)
        return 0, "", ""
    except ApiException as e:
        return -1, "", str(e)

@log_arguments
def k8_create_role_binding(crb_name: str, namespace: str, cluster_role_name: str, sa_name: str) -> (int, str, str):
    return k8_create_role_binding_generic(
        crb_name=crb_name,
        cluster_role_name=cluster_role_name,
        subject_kind="ServiceAccount",
        subject_name=sa_name,
        namespace=namespace,
    )

@log_arguments
def k8_create_role_binding_user(crb_name: str, cluster_role_name: str, user_name: str) ->(int, str, str):
    return k8_create_role_binding_generic(
        crb_name=crb_name,
        cluster_role_name=cluster_role_name,
        subject_kind="User",
        subject_name=user_name,
        namespace=None,
    )
@log_arguments
def k8_delete_pod(pod_name : str, namespace : str, force : bool = False):
    """
    API to delete a pod
    """
    global Logger
    global LogPrettyPrinter
    api = client.CoreV1Api()
    try:
        resp = api.delete_namespaced_pod(pod_name, namespace)
        #Logger.debug(f"Successfully delete pod, resp:\n{LogPrettyPrinter.pformat(resp)}")
        return 0, "", ""
    except ApiException as e:
        Logger.error(f"Failed to delete pod, error: {e}")
        return -1, "", str(e)

@log_arguments
def k8_delete_all_pods(namespace : str):
    """
    API to delete all pods in a given namespace
    """
    global Logger
    ret_code, pods = k8_get_pods(namespace)
    if ret_code != 0:
        return ret_code, "", f"Failed to get all pods for given namespace {namespace}"
    for pod in pods:
        k8_delete_pod(pod['metadata']['name'], namespace)
    return 0, "", ""

@log_arguments
def k8_delete_all_pods_with_name_pattern(namespace : str, pod_name_pattern: str) -> int:
    """
    API to delete all pods with given name pattern
    """
    global Logger
    global LogPrettyPrinter
    ret_code = 0
    api = client.CoreV1Api()

    delete_list = []
    try:
        pods = api.list_namespaced_pod(namespace = namespace)
        for pod in pods.items:
            if pod_name_pattern in pod.metadata.name:
                delete_list.append(pod.metadata.name)
    except ApiException as e:
        Logger.error(f"Failed to get all pods from namespace {namespace}, error: {e}")
        return -1

    Logger.info(f"Deleting following pods from the cluster : {delete_list}")
    for pod_name in delete_list:
        ret_code, ret_stdout, ret_stderr = k8_delete_pod(pod_name, namespace, force = True)
        if ret_code != 0:
            Logger.error(f"Failed to delete pod {pod_name}, error {ret_stderr}")
    return ret_code

@log_arguments
def k8_get_namespaces():
    """
    API to get all namespaces in a given k8 cluster
    """
    global Logger
    api = client.CoreV1Api()
    try:
        k8_namespace_info = api.list_namespace().to_dict()
    except ApiException as e:
        Logger.error(f"Failed to collect namespaces, error : {e}")
        return -1, None
    return 0, k8_namespace_info.get("items", list())

@log_arguments
def k8_delete_namespace(namespace : str):
    """
    API to delete namespace in a given k8 cluster
    """
    global Logger
    api = client.CoreV1Api()
    try:
        api_response = api.delete_namespace(name = namespace, body=client.V1DeleteOptions())
        Logger.debug("k8_delete_namespace::api_response : {api_response}")
        return 0, "", ""
    except ApiException as e:
        Logger.error(f"Failed to delete namespace, error : {e}")
        return -1, "", str(e)

@log_arguments
def k8_create_namespace(namespace : str):
    """
    API to create a namespace
    """
    global Logger
    api = client.CoreV1Api()
    msg_body = client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace))
    try:
        api_response = api.create_namespace(body = msg_body)
        Logger.debug(f"k8_create_namespace::api_response : {api_response}")
        return 0, "", ""
    except ApiException as e:
        if e.status == 409 and e.reason == 'Conflict':
            return 0, "", ""
        Logger.error(f"Failed to create namespace, error : {e}")
        return -1, "", str(e)

@log_arguments
def k8_create_pre_test_runner_job(namespace: str, images: dict, sa_name: str, deployment_name: str, worker: str, framework: str, init_cap: str):
    global Logger
    apps_v1 = client.AppsV1Api()
    core_v1 = client.CoreV1Api()
    gpu_type = k8_get_node_gpu_allocatable(worker)

    # Define deployment metadata
    labels = {"purpose": "demo-pytorch-amdgpu"}

    # Define the volume mounts for the init container
    init_container_volume_mounts = [
        client.V1VolumeMount(
            name="config-volume",
            mount_path="/etc/test-runner/"
        ),
        client.V1VolumeMount(
            name="rvs-logs",
            mount_path="/var/log"
        )
    ]
    testrunner_image_key = 'testRunner'
    if framework == "AGFHC":
        testrunner_image_key += 'Agfhc'
    repository = images.get(testrunner_image_key + '.image.repository')
    version = images.get(testrunner_image_key + '.image.version')

    # Define initContainer for the test runner
    init_container = client.V1Container(
        name="init-test-runner",
        image=f"{repository}:{version}",
        image_pull_policy="IfNotPresent",
        volume_mounts=init_container_volume_mounts,
        resources=client.V1ResourceRequirements(
            requests={gpu_type: init_cap},
            limits={gpu_type: init_cap}
        ),
        env=[
            client.V1EnvVar(name="TEST_TRIGGER", value="PRE_START_JOB_CHECK"),
            client.V1EnvVar(
                name="POD_NAME",
                value_from=client.V1EnvVarSource(
                    field_ref=client.V1ObjectFieldSelector(field_path="metadata.name")
                )
            ),
            client.V1EnvVar(
                name="POD_NAMESPACE",
                value_from=client.V1EnvVarSource(
                    field_ref=client.V1ObjectFieldSelector(field_path="metadata.namespace")
                )
            ),
            client.V1EnvVar(
                name="NODE_NAME",
                value_from=client.V1EnvVarSource(
                    field_ref=client.V1ObjectFieldSelector(field_path="spec.nodeName")
                )
            )
        ]
    )

    # Define the copy-rvs-logs container
    copy_logs_container = client.V1Container(
        name="copy-rvs-logs",
        image="busybox",
        command=["sh", "-c", "echo 'Copying RVS logs...'; cp -rv /var/log/* /host-logs/ && sleep 3600"],
        volume_mounts=[
            client.V1VolumeMount(name="rvs-logs", mount_path="/var/log"),
            client.V1VolumeMount(name="host-logs", mount_path="/host-logs"),
        ]
    )

    # Define the main container for the PyTorch workload
    main_container = client.V1Container(
        name="gpu-workload",
        image="busybox",
        command=["/bin/sh", "-c", "--"],
        args=["sleep 6000"],
        resources=client.V1ResourceRequirements(
            requests={gpu_type: "1"},
            limits={gpu_type: "1"}
        ),
    )

    # Define the volumes
    volumes = [
        client.V1Volume(
            name="rvs-logs",
            empty_dir=client.V1EmptyDirVolumeSource()
        ),
        client.V1Volume(
            name="host-logs",
            host_path=client.V1HostPathVolumeSource(
                path="/var/log/amd-test-runner",
                type="DirectoryOrCreate"
            )
        ),
        client.V1Volume(
            name="config-volume",
            config_map=client.V1ConfigMapVolumeSource(name="config-test-runner")
        ),
    ]

    # Define the Pod template spec
    pod_template_spec = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels=labels),
        spec=client.V1PodSpec(
            service_account_name=sa_name,
            init_containers=[init_container],
            containers=[copy_logs_container, main_container],
            volumes=volumes
        )
    )

    # Define the Deployment spec
    deployment_spec = client.V1DeploymentSpec(
        replicas=1,
        selector=client.V1LabelSelector(match_labels=labels),
        template=pod_template_spec
    )

    # Combine everything into the final Deployment body
    deployment_body = client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=deployment_name, namespace=namespace, labels=labels),
        spec=deployment_spec
    )

    # Call the function to create the Deployment
    try:
        # Create the Deployment using the create_namespaced_deployment method
        deployment = apps_v1.create_namespaced_deployment(
            namespace=namespace,
            body=deployment_body
        )
        Logger.info(f"Deployment '{deployment_body.metadata.name}' created successfully in namespace '{namespace}'.")
        Logger.info(f"Status : {deployment.status.ready_replicas}")
    except ApiException as e:
        assert True, f"Error creating Deployment: {e}"

@log_arguments
def k8_get_deployment(namespace, deployment_name):
    global Logger
    apps_v1 = client.AppsV1Api()
    try:
        deployment = apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
        Logger.info(f"Deployment '{deployment.metadata.name}' found. Status:")
        Logger.info(f"  Replicas: {deployment.status.replicas}")
        Logger.info(f"  Ready Replicas: {deployment.status.ready_replicas}")
        Logger.info(f"  Available Replicas: {deployment.status.available_replicas}")
        Logger.info(f"  Unavailable Replicas: {deployment.status.unavailable_replicas}")

        Logger.info("\n  Deployment Conditions:")
        if deployment.status.conditions:
            Logger.info(deployment.status.conditions)
        else:
            Logger.info("No conditions reported for the deployment.")
        return deployment
    except client.ApiException as e:
        if e.status == 404:
            Logger.info(f"Error: Deployment '{deployment_name}' not found in namespace '{namespace}'.")
        else:
            Logger.error(f"Error fetching deployment status: {e}")
        assert True, f"Error fetching Deployment: {e}"

@log_arguments
def k8_delete_deployment(namespace, deployment_name):
    global Logger
    apps_v1 = client.AppsV1Api()
    try:
        delete_options = client.V1DeleteOptions(
            propagation_policy="Foreground", # Options: "Foreground", "Background", "Orphan"
            grace_period_seconds=5 # Graceful shutdown period in seconds
        )

        # Delete the namespaced deployment
        api_response = apps_v1.delete_namespaced_deployment(
            name=deployment_name,
            namespace=namespace,
            body=delete_options
        )

        Logger.info(f"Deployment '{deployment_name}' in namespace '{namespace}' deleted successfully.")
        # You might inspect api_response for further details, though it often returns a V1Status object on success.
        return

    except client.ApiException as e:
        if e.status == 404:
            assert True, f"Error: Deployment '{deployment_name}' not found in namespace '{namespace}'."
        else:
            assert True, f"Error deleting deployment: {e}"
    except Exception as e:
        assert True, f"An unexpected error occurred: {e}"

@log_arguments
def k8_create_test_runner_job(namespace : str, images : dict, worker : str, sa_name: str, job_name : str, framework : str, healthy : bool, schedule : bool, minute : str):
    global Logger

    # Pre loaded Load Kubernetes configuration
    # This will typically load from ~/.kube/config or from within a cluster
    # Create an instance of the BatchV1Api, which is used for Jobs

    batch_v1_api = client.BatchV1Api()
    gpu_type = k8_get_node_gpu_allocatable(worker)
    init_cap, alloc = k8_get_node_gpu_capacity(worker)
    testrunner_image_key = 'testRunner'
    if framework == "AGFHC":
        testrunner_image_key += 'Agfhc'
    repository = images.get(testrunner_image_key + '.image.repository')
    version = images.get(testrunner_image_key + '.image.version')

    # Define environment variables
    env_vars = [
        client.V1EnvVar(name="TEST_TRIGGER", value="MANUAL"),
        client.V1EnvVar(
            name="POD_NAME",
            value_from=client.V1EnvVarSource(
                field_ref=client.V1ObjectFieldSelector(field_path="metadata.name")
            ),
        ),
        client.V1EnvVar(
            name="POD_NAMESPACE",
            value_from=client.V1EnvVarSource(
                field_ref=client.V1ObjectFieldSelector(field_path="metadata.namespace")
            ),
        ),
        client.V1EnvVar(
            name="NODE_NAME",
            value_from=client.V1EnvVarSource(
                field_ref=client.V1ObjectFieldSelector(field_path="spec.nodeName")
            ),
        ),
    ]
    if schedule:
        container_name = "init-test-runner"
    else:
        container_name = "amd-test-runner"

    # Define volume mounts
    volume_mounts = [
        client.V1VolumeMount(mount_path="/dev/dri", name="dri"),
        client.V1VolumeMount(mount_path="/dev/kfd", name="kfd"),
        client.V1VolumeMount(mount_path=f"/var/log/{container_name}", name="host-logs")
    ]

    # Define volumes
    volumes = [
        client.V1Volume(
            name="kfd",
            host_path=client.V1HostPathVolumeSource(
                path="/dev/kfd", type="CharDevice"
            ),
        ),
        client.V1Volume(
            name="dri",
            host_path=client.V1HostPathVolumeSource(
                path="/dev/dri", type="Directory"
            ),
        ),
        client.V1Volume(
            name="host-logs",
            host_path=client.V1HostPathVolumeSource(
                path=f"/var/log/{container_name}",
                type="DirectoryOrCreate"
            )
        )
    ]

    if healthy and not schedule:
        # Define resource limits for the container
        # Note: Custom resources like 'amd.com/gpu' are strings in the limits dictionary.
        resources = client.V1ResourceRequirements(
            limits={
                gpu_type: init_cap  # Requesting 8 GPUs
            }
        )
        # Define containers
        container = client.V1Container(
            name=container_name,
            image=f"{repository}:{version}",
            image_pull_policy="IfNotPresent",
            security_context=client.V1SecurityContext(privileged=True),
            volume_mounts=volume_mounts,
            env=env_vars,
            resources=resources
        )
    else:
        # Define containers
        container = client.V1Container(
            name=container_name,
            image=f"{repository}:{version}",
            image_pull_policy="IfNotPresent",
            security_context=client.V1SecurityContext(privileged=True),
            volume_mounts=volume_mounts,
            env=env_vars,
        )

    # Define pod template spec
    pod_template_spec = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": "test-runner"}), # Add a label for easier identification
        spec=client.V1PodSpec(
            service_account_name=sa_name,
            node_selector={"kubernetes.io/hostname": worker},
            volumes=volumes,
            containers=[container],
            restart_policy="Never",
        ),
    )

    # Define job spec
    job_spec = client.V1JobSpec(
        template=pod_template_spec,
        backoff_limit=0,
        ttl_seconds_after_finished=120,
    )

    # Define job metadata
    job_metadata = client.V1ObjectMeta(
        name=job_name, namespace=namespace
    )

    api_client = client.ApiClient()
    if not schedule:
        # Create the V1Job object
        job = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=job_metadata,
            spec=job_spec,
        )
        sanitized_dict = api_client.sanitize_for_serialization(job)
        try:
            # Create the Job in the specified namespace
            api_response = batch_v1_api.create_namespaced_job(namespace=namespace, body=job)
            Logger.info(f"Job created successfully: {api_response.metadata.name}")
        except client.ApiException as e:
            assert True, f"Error creating Job: {e}"
    else:
            # Define the Job template spec for the CronJob
        job_template_spec = client.V1JobTemplateSpec(spec=job_spec)
        cronjob_spec = client.V1CronJobSpec(
            schedule=f"{minute} * * * *", # Daily at midnight
            job_template=job_template_spec
        )

        # Create the V1CronJob object
        cron_job = client.V1CronJob(
            api_version="batch/v1", # Changed apiVersion for CronJob
            kind="CronJob",        # Changed kind to CronJob
            metadata=job_metadata,
            spec=cronjob_spec,
        )
        sanitized_dict = api_client.sanitize_for_serialization(cron_job)
        try:
            # Create the Job in the specified namespace
            api_response = batch_v1_api.create_namespaced_cron_job(namespace=namespace, body=cron_job)
            Logger.info(f"Job created successfully: {api_response.metadata.name}")
        except client.ApiException as e:
            assert True, f"Error creating Job: {e}"
    yaml_output = yaml.dump(sanitized_dict, default_flow_style=False)
    Logger.info(pprint.pprint(yaml_output))


@log_arguments
def k8_get_job_status(namespace : str, job_name):
    global Logger
    api = client.BatchV1Api()
    v1 = client.CoreV1Api()
    try:
        time.sleep(2)
        api_response = api.list_namespaced_job(namespace=namespace)
        Logger.debug(f"k8_get_job::api_response : {api_response}")
        result = ""
        if len(api_response.items) != 0:
            item = api_response.items[-1]
            if item.status.succeeded is None:
                result = "Unknown"
            elif item.status.active is None and item.status.succeeded == "1":
                result = "Succeeded"
            elif item.status.active == "1":
                result = "Active"
        else:
            return "Incomplete"
        Logger.debug(f"Status of job is {result} and full status is {item.status}")
        label_selector = f"job-name={job_name}"
        pods = v1.list_pod_for_all_namespaces(label_selector=label_selector)
        Logger.debug("k8_get_job::list of pods assoc with job: {job_name}:\n{pods}")
            #Pending, Running, Succeeded
        if len(pods.items) == 0:
            return "Completed"
        if pods.items[-1].status.phase:
            return pods.items[-1].status.phase
        return result
    except ApiException as e:
        Logger.error(f"Failed to get job, error : {e}")
        return "Completed"

@log_arguments
def k8_delete_job(namespace : str, job_name : str):
    global Logger
    api = client.BatchV1Api()
    v1 = client.CoreV1Api()
    try:
        label_selector = f"job-name={job_name}"
        pods = v1.list_pod_for_all_namespaces(label_selector=label_selector)
        for pod in pods.items:
            k8_delete_pod(pod.metadata.name, namespace, True)
        api_response = api.delete_namespaced_job(namespace=namespace, name=job_name)
        Logger.debug("k8_get_job::api_response : {api_response}")
        return True
    except ApiException as e:
        return -1, "", str(e)

@log_arguments
def k8_get_cron_job_status(namespace : str, job_name):
    global Logger
    api = client.BatchV1Api()
    try:
        api_response = api.list_namespaced_cron_job(namespace=namespace)
        Logger.debug("k8_get_cron_job::api_response : {api_response}")
        return api_response.items[-1].metadata.name == job_name
    except ApiException as e:
        Logger.error(f"Failed to get cron job, error : {e}")
        return False

@log_arguments
def k8_delete_cron_job(namespace : str, job_name : str):
    global Logger
    api = client.BatchV1Api()
    try:
        api_response = api.delete_namespaced_cron_job(namespace=namespace, name=job_name)
        Logger.debug("k8_get_job::api_response : {api_response}")
    except ApiException as e:
        return -1, "", str(e)

@log_arguments
def k8_check_pod_status(namespace, pod_list) -> Dict:
    """
    API to check the status of pods in a given namespace, returns a dict of pod_name -> phase and pod info
    """
    pod_status = dict()
    ret_code, k8_pod_list = k8_get_pods(namespace)
    assert ret_code == 0, "Error while getting all pods from k8-cluster"
    for pod_info in pod_list:
        sel_pods = list(filter(lambda x: pod_info.PodName in x['metadata'].get('name', None), k8_pod_list))
        for sel_pod_info in sel_pods:
            name = sel_pod_info.get('metadata', {}).get('name')
            status_json = sel_pod_info.get('status', {})
            pod_status[name] = (status_json.get('phase', 'Unknown'), sel_pod_info)
    return pod_status


@log_arguments
def k8_check_pod_running(namespace : str, pod_list : List, sleep_time : int = 10, total_attempts : int = 10):
    """
    API to check if ALL of given list of PODs are running
    """
    global Logger
    global LogPrettyPrinter
    def _is_pod_present_and_match_status(k8_pod_list, pod_name, exp_pod_count, exp_cont_count, exp_status):
        sel_pods = list(filter(lambda x: pod_name in x['metadata'].get('name', None), k8_pod_list))
        if len(sel_pods) < exp_pod_count:
            Logger.warn(f"Found {len(sel_pods)} instances of pod_name: {pod_name}")
            return False

        match_status = True
        for sel_pod_info in sel_pods:
            status_json = sel_pod_info.get('status', None)
            if status_json and status_json.get('phase', None) != exp_status:
                match_status = False
                status = status_json.get('phase', None)
                Logger.warn(f"Pod: {pod_name} instance in {status} and not in {exp_status}")
                #Logger.debug(f"PodInfo:\n{LogPrettyPrinter.pformat(sel_pod_info)}")
        return match_status

    assert len(pod_list) > 0, "No pods specified to verify"
    if total_attempts == 0:
        total_attempts = 1

    failed_pods = list()
    for x in range(total_attempts):
        failed_pods.clear()
        ret_code, k8_pod_list = k8_get_pods(namespace)
        assert ret_code == 0, "Error while getting all pods from k8-cluster"
        for pod_info in pod_list:
            if not _is_pod_present_and_match_status(k8_pod_list, pod_info.PodName, pod_info.NumInstances, pod_info.ContainerCount, 'Running'):
                failed_pods.append(pod_info.PodName)

        if failed_pods:
            time.sleep(sleep_time)
        else:
            break
    if failed_pods:
        Logger.debug(f"Status of the Pods {pod_list}\n{LogPrettyPrinter.pformat(k8_pod_list)}")
    return failed_pods

@log_arguments
def k8_check_pod_terminated(namespace : str, pod_list : List, sleep_time : int = 10, total_attempts : int = 10):
    """
    API to check if ALL of given list of PODs are terminated
    """
    global Logger
    global LogPrettyPrinter
    def _is_pod_terminated(k8_pod_list, pod_name):
        sel_pods = list(filter(lambda x: pod_name in x['metadata'].get('name', None), k8_pod_list))
        if len(sel_pods) == 0:
            return True
        return False

    assert len(pod_list) > 0, "No pods specified to verify"
    if total_attempts == 0:
        total_attempts = 1

    running_pods = list()
    for x in range(total_attempts):
        running_pods.clear()
        ret_code, k8_pod_list = k8_get_pods(namespace)
        assert ret_code == 0, "Error while getting all pods from k8-cluster"

        for pod_info in pod_list:
            if not _is_pod_terminated(k8_pod_list, pod_info.PodName):
                running_pods.append(pod_info.PodName)

        if running_pods:
            time.sleep(sleep_time)
        else:
            break
    if running_pods:
        Logger.debug(f"Status of Pods {pod_list}\n{LogPrettyPrinter.pformat(k8_pod_list)}")
    return running_pods

@log_arguments
def k8_create_configmap(namespace : str, configmap_name : str, configmap_file : str, config_map_name : str):
    """
    API to create configmap in a k8-cluster

    Example: kubectl create configmap -n kube-amd-gpu exporter-config --from-file=config.json
    - If the input file has .json extension, store raw file content under "config.json".
    - If the input file has .crt extension, store raw file content under file name.
    - If the input file has .yaml extension, store raw file content under workflow name.
    """
    global Logger 
    if os.path.splitext(configmap_file)[1] == '.json' : 
        with open(configmap_file) as fp:
            data = json.load(fp)
        data = {config_map_name : json.dumps(data)}
    elif os.path.splitext(configmap_file)[1] == '.crt' : 
        with open(configmap_file, "r", encoding="utf-8") as fp:
            raw_text = fp.read()
        data = {config_map_name : raw_text}
    elif os.path.splitext(configmap_file)[1] == '.yaml':
        with open(configmap_file, "r") as fp:
            data = {config_map_name : fp.read()}
    else:
        Logger.error(
            f"Unsupported file type for '{configmap_file}'. "
            "Expected a file with .json, .crt, .yaml extension.")
        return -1, "", f"Unsupported file type: {configmap_file}"
    api = client.CoreV1Api()
    config_map = client.V1ConfigMap(
            api_version = "v1",
            kind = "ConfigMap",
            metadata = client.V1ObjectMeta(name=configmap_name, namespace=namespace),
            data = data
        )
    try:
        api_response = api.create_namespaced_config_map(namespace, config_map)
    except ApiException as e:
        Logger.error(f"Failed to create configmap, error : {e}")
        return -1, "", str(e)
    return 0, "", ""

@log_arguments
def k8_delete_configmap(namespace : str, configmap_name : str):
    """
    API to create configmap in a k8-cluster

    Example: kubectl delete configmap --namespace kube-amd-gpu exporter-config
    """
    global Logger

    api = client.CoreV1Api()
    try:
        api_response = api.delete_namespaced_config_map(configmap_name, namespace)
    except ApiException as e:
        Logger.debug(f"Failed to delete config-map, error : {e}")
        return -1, "", str(e)
    return 0, "", ""

def k8_get_node_address(node_info, address_type = "InternalIP"):
    assert 'status' in node_info, f"k8 node missing status section, {node_info}"
    assert 'addresses' in node_info['status'], f"k8 node missing status.addresses, {node_info}"

    for addr in node_info['status']['addresses']:
        if addr.get("type", None) == address_type:
            return addr.get("address", None)
    assert f"Missing address-type : {address_type} in k8 node, {node_info}"

def k8_lookup_node_by_name(node_name):
    global Logger
    ret_code, k8_nodes = k8_get_nodes()
    if ret_code != 0:
        return ret_code, None

    for node in k8_nodes:
        if node['metadata']['name'] == node_name:
            return k8_get_node_address(node)

    assert f"Missing node k8-cluster, {node_name}"

def k8_get_node_hostname(node_info, address_type = "Hostname"):
    assert 'status' in node_info, f"k8 node missing status section, {node_info}"
    assert 'addresses' in node_info['status'], f"k8 node missing status.addresses, {node_info}"

    for addr in node_info['status']['addresses']:
        if addr.get("type", None) == address_type:
            return addr.get("address", None)
    assert f"Missing address-type : {address_type} in k8 node, {node_info}"

def k8_get_node_os_info(node_info):
    assert 'status' in node_info, f"k8 node missing status section, {node_info}"
    assert 'node_info' in node_info['status'], f"k8 node missing status.node_info, {node_info}"
    os_type = node_info['status']['node_info'].get('operating_system', 'unknown').lower()
    os_img = node_info['status']['node_info'].get('os_image', 'unknown 0.0')
    if os_type != "linux":
        return (os_type, "unknown", "0.0")

    os_info = os_img.split()
    if "Ubuntu" in os_img:
        if len(os_info) > 1 and "22.04" in os_info[1]:
            return (os_type, "Ubuntu", "22.04")
        if len(os_info) > 1 and "24.04" in os_info[1]:
            return (os_type, "Ubuntu", "24.04")
        return (os_type, "Ubuntu", os_info[1] if len(os_info) > 1 else "unknown")
    if "Debian" in os_img:
        if "12" in os_img:
            return (os_type, "Debian", "12")
    return (os_type, os_info[0] if len(os_info) > 0 else "unknown", os_info[1] if len(os_info) > 1 else "unknown")

@log_arguments
def k8_cordon_node(node_name : str):
    """
    API to cordon node
    """
    global Logger
    try:
        v1 = client.CoreV1Api()
        patch_body = {"spec": {"unschedulable": True}}
        api_response = v1.patch_node(name=node_name, body=patch_body)
        return 0, api_response
    except ApiException as e:
        Logger.error(f"Failed cordon node {node_name}: {e}")
        return -1, None
    return -1, None

@log_arguments
def k8_uncordon_node(node_name):
    """
    API to uncodon node
    """
    global Logger
    try:
        v1 = client.CoreV1Api()
        patch_body = {"spec": {"unschedulable": False}}
        api_response = v1.patch_node(name=node_name, body=patch_body)
        return 0, api_response
    except ApiException as e:
        Logger.error(f"Failed cordon node {node_name}: {e}")
        return -1, None
    return -1, None

@log_arguments
def k8_delete_cluster_role(cluster_role_name):
    """
    API to delete cluster-role

    Example: kubectl delete clusterrole metrics
    """
    rbac_api = client.RbacAuthorizationV1Api()
    try:
        api_response = rbac_api.delete_cluster_role(cluster_role_name)
    except ApiException as e:
        Logger.debug(f"Failed to delete cluster-role {cluster_role_name}, error {e}")
        return -1, "", str(e)
    return 0, "", ""

@log_arguments
def k8_get_node_labels(node_name):
    global Logger
    v1 = client.CoreV1Api()
    try:
        node = v1.read_node(name=node_name)
        return node.metadata.labels
    except ApiException as e:
        Logger.error(f"Error getting labels for node '{node_name}': {e}")
    return None

@log_arguments
def k8_label_node(node_name, labels_dict=None, overwrite=True):
    """Applies labels to a node."""
    global Logger
    v1 = client.CoreV1Api()
    POLL_INTERVAL_SECONDS = 5
    if labels_dict is None:
        labels_dict = {}
    body = {
        "metadata": {
            "labels": labels_dict
        }
    }
    try:
        v1.patch_node(name=node_name, body=body)
        Logger.info(f"Labels applied to node '{node_name}': {labels_dict}")
        time.sleep(POLL_INTERVAL_SECONDS) # Give system time to update
        return True
    except ApiException as e:
        Logger.erro(f"Error labeling node '{node_name}': {e}")
        return False

@log_arguments
def k8_get_events(namespace : str, pod_name=None):
    """
    API to

    Example: kubectl get events --namespace kube-amd-gpu
    """
    global Logger
    global LogPrettyPrinter
    api = client.CoreV1Api()
    field_selector = None
    if pod_name:
        field_selector = f"involvedObject.kind=Pod,involvedObject.name={pod_name}"
    try:
        events = api.list_namespaced_event(namespace=namespace, field_selector=field_selector)
    except ApiException as e:
        Logger.error(f"Failed to get events from {namespace}, field_selector={field_selector}, error : {e}")
        return -1, "", str(e)
    return 0, events, ""

@log_arguments
def k8_get_pod_name(pod_str : str, namespace : str, node_name : str = None):
    ret_code, pods = k8_get_pods(namespace, node_name = node_name)
    assert ret_code == 0, f"Failed to get pod names in namespace {namespace}"
    for pod in pods:
        if pod.get('metadata') != None and pod_str in pod.get('metadata').get('name'):
            return pod['metadata']['name']

@log_arguments
def k8_get_container_logs(pod_str, namespace, container):
    global Logger
    pod_name = k8_get_pod_name(pod_str, namespace)
    api = client.CoreV1Api()
    logs = ""

    try:
        logs = api.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container
        )
    except client.ApiException as e:
        Logger.error(f"Error getting container logs: {e}")
    return logs

@log_arguments
def k8_get_pod_logs(pod_str : str, namespace : str, since="180s", container = None):
    if container != None:
        logs = k8_get_container_logs(pod_str, namespace, container)
        return 0, logs, ""

    pod_name = k8_get_pod_name(pod_str, namespace)
    api = client.CoreV1Api()
    try:
        logs = api.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            since_seconds=int(since[:-1]),
            _return_http_data_only=True
        )
        return 0, logs, ""
    except client.ApiException as e:
        Logger.error(f"Error getting container logs: {e}")
        return 0, "", str(e)

@log_arguments
def k8_taint_node(node_name : str, taint_add=True, effect="NoSchedule"):
    """
    Add or remove amd-dcm taint from a node.

    This function adds or removes the amd-dcm=up taint with the specified effect.
    Only the amd-dcm taint is modified; other existing taints are preserved.

    Args:
        node_name: Name of the Kubernetes node to taint/untaint
        taint_add: True to add the taint, False to remove it (default: True)
        effect: Taint effect - "NoSchedule", "NoExecute", or "PreferNoSchedule" (default: "NoSchedule")
                - NoSchedule: Prevents new pods from scheduling (existing pods continue)
                - NoExecute: Evicts existing pods and prevents new scheduling
                - PreferNoSchedule: Soft version of NoSchedule

    Examples:
        # Add NoSchedule taint (default)
        k8_taint_node("worker-1", taint_add=True)

        # Add NoExecute taint (evicts existing pods)
        k8_taint_node("worker-1", taint_add=True, effect="NoExecute")

        # Remove taint (effect doesn't matter when removing)
        k8_taint_node("worker-1", taint_add=False)

    Notes:
        - For DCM partition operations, use effect="NoExecute" to evict device-plugin pods
        - Retries up to 5 times on conflict errors
        - Preserves other taints on the node (e.g., master, control-plane taints)
    """
    global Logger
    taint_key = "amd-dcm"
    taint_value = "up"

    # Validate effect parameter
    valid_effects = ["NoSchedule", "NoExecute", "PreferNoSchedule"]
    if effect not in valid_effects:
        Logger.error(f"Invalid taint effect '{effect}'. Must be one of: {valid_effects}")
        return

    v1 = client.CoreV1Api()
    node = v1.read_node(name=node_name)

    # Preserve existing taints, only modify amd-dcm taint
    existing_taints = node.spec.taints or []

    # Remove any existing amd-dcm taints (to avoid duplicates and handle effect changes)
    filtered_taints = [
        t for t in existing_taints
        if not (t.key == taint_key and t.value == taint_value)
    ]

    # Add the new taint if requested
    if taint_add:
        new_taint = client.V1Taint(key=taint_key, value=taint_value, effect=effect)
        filtered_taints.append(new_taint)
        Logger.info(f"Adding taint to node '{node_name}': {taint_key}={taint_value}:{effect}")
    else:
        Logger.info(f"Removing taint from node '{node_name}': {taint_key}={taint_value}")

    node.spec.taints = filtered_taints

    # Update the node object with the modified taints
    for attempt in range(5):
        try:
            v1.patch_node(name=node_name, body=node)
            action = "added" if taint_add else "removed"
            Logger.info(f"Successfully {action} taint on node '{node_name}': {taint_key}={taint_value}:{effect}")
            break
        except client.ApiException as e:
            Logger.warning(f"Error modifying taint on node '{node_name}' (attempt {attempt + 1}/5): {e}")
            if e.reason == "Conflict":
                # Node was modified by another process, re-read and retry
                time.sleep(5)
                node = v1.read_node(name=node_name)
                existing_taints = node.spec.taints or []
                filtered_taints = [
                    t for t in existing_taints
                    if not (t.key == taint_key and t.value == taint_value)
                ]
                if taint_add:
                    new_taint = client.V1Taint(key=taint_key, value=taint_value, effect=effect)
                    filtered_taints.append(new_taint)
                node.spec.taints = filtered_taints
                continue
            else:
                Logger.error(f"Failed to modify taint on node '{node_name}': {e}")
                break
    return

@log_arguments
def k8_untaint_node(node_name : str, effects=["NoSchedule"]):
    """
    API to untaint node

    Example: kubectl untaint nodes node_name gpu=unhealthy:NoSchedule
    """
    for effect in effects:
        k8_taint_node(node_name, taint_add=False, effect=effect)

@log_arguments
def k8_patch_deployment(deployment, namespace, new_toleration, tolerate_add):
    """Adds a toleration to a single Deployment."""
    api = client.AppsV1Api()
    name = deployment.metadata.name
    Logger.info(f"-> Patching Deployment: {name}")
    op = "add"
    if not tolerate_add:
        op = "remove"

    body = [
        {"op": op, "path": "/spec/template/spec/tolerations/-", "value": new_toleration.to_dict()}
    ]
    try:
        api.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
    except ApiException as e:
        Logger.error(f"Could not patch Deployment {name}: {e}")

@log_arguments
def k8_patch_config_map(config_map_name, namespace, body):
    """
    API to modify config-map
    """
    api = client.CoreV1Api()
    Logger.info(f"-> Patching ConfigMap: {config_map_name}")
    try:
        api.patch_namespaced_config_map(name=config_map_name, namespace=namespace, body=body)
    except ApiException as e:
        Logger.error(f"Could not patch ConfigMap {config_map_name}: {e}")
        return -1, "", str(e)
    return 0, "", ""

@log_arguments
def k8_patch_daemonset(daemonset_name, namespace, body):
    """
    API to modify daemonset
    """
    api = client.AppsV1Api()
    Logger.info(f"Patching DaemonSet: {daemonset_name}")
    try:
        api.patch_namespaced_daemon_set(name=daemonset_name, namespace=namespace, body=body)
    except ApiException as e:
        Logger.error(f"Could not patch DaemonSet {daemonset_name}: {e}")
        if e.status == 404:
            try:
                api.create_namespaced_daemon_set(namespace=namespace, body=body)
                return 0, "", ""
            except ApiException as ae:
                Logger.error(f"Could not create Daemonset {daemonset_name}: {ae}")
        return -1, "", str(e)
    return 0, "", ""

@log_arguments
def k8_delete_daemonset(namespace : str, daemonset_name : str):
    """
    API to delete daemonset
    """
    api = client.AppsV1Api()
    Logger.info(f"Deleting DaemonSet: {daemonset_name}")
    try:
        api.delete_namespaced_daemon_set(name=daemonset_name, namespace=namespace)
    except ApiException as e:
        Logger.error(f"Could not delete DaemonSet {daemonset_name}: {e}")
        return -1, "", str(e)
    return 0, "", ""

@log_arguments
def k8_daemonset_modify_tolerations(daemonset, namespace, new_toleration, tolerate_add):
    """Adds a toleration to a single DaemonSet."""
    api = client.AppsV1Api()
    name = daemonset.metadata.name
    Logger.info(f"-> Patching DaemonSet: {name}")
    op = "add"
    if not tolerate_add:
        op = "remove"

    body = [
        {"op": op, "path": "/spec/template/spec/tolerations/-", "value": new_toleration.to_dict()}
    ]
    try:
        api.patch_namespaced_daemon_set(name=name, namespace=namespace, body=body)
    except ApiException as e:
        Logger.error(f"Could not patch DaemonSet {name}: {e}")

@log_arguments
def k8_patch_statefulset(statefulset, namespace, new_toleration, tolerate_add):
    """Adds a toleration to a single StatefulSet."""
    api = client.AppsV1Api()
    name = statefulset.metadata.name
    Logger.info(f"-> Patching StatefulSet: {name}")
    op = "add"
    if not tolerate_add:
        op = "remove"


    body = [
        {"op": op, "path": "/spec/template/spec/tolerations/-", "value": new_toleration.to_dict()}
    ]
    try:
        api.patch_namespaced_stateful_set(name=name, namespace=namespace, body=body)
    except ApiException as e:
        Logger.error(f"Could not patch StatefulSet {name}: {e}")

@log_arguments
def k8_patch_tolerations(namespace, toleration, tolerate_add=True):
    """
    API to add tolerations to all deployments under the particular namespace

    Example: kubectl taint nodes node_name gpu=unhealthy:NoSchedule
    """

    client_v1 = client.AppsV1Api()
    new_toleration = client.V1Toleration(
        key=toleration['key'],
        operator=toleration['operator'],
        value=toleration['value'],
        effect=toleration['effect']
    )

    # --- Patch Deployments ---
    #Logger.debug(f"Patching Deployments in namespace: {namespace}")
    deployments = client_v1.list_namespaced_deployment(namespace=namespace)
    for deployment in deployments.items:
        k8_patch_deployment(deployment, namespace, new_toleration, tolerate_add)

    # --- Patch DaemonSets ---
    #Logger.debug(f"Patching DaemonSets in namespace: {namespace}")
    daemonsets = client_v1.list_namespaced_daemon_set(namespace=namespace)
    for daemonset in daemonsets.items:
        k8_daemonset_modify_tolerations(daemonset, namespace, new_toleration, tolerate_add)

    # --- Patch StatefulSets ---
    #Logger.debug(f"Patching StatefulSets in namespace: {namespace}")
    statefulsets = client_v1.list_namespaced_stateful_set(namespace=namespace)
    for statefulset in statefulsets.items:
        k8_patch_statefulset(statefulset, namespace, new_toleration, tolerate_add)

@log_arguments
def k8_watch_daemon_set_rollout(namespace, timeout = 300):
    """
    API to watch for rollout completion of the given daemon-set
    """
    global Logger

    def _is_rollout_complete(ds):
        status = getattr(ds, "status", None)
        if not status:
            return False
        observed_generation = status.observed_generation or 0
        generation = ds.metadata.generation or 0
        updated_scheduled = status.updated_number_scheduled or 0
        desired_scheduled = status.desired_number_scheduled or 0
        num_available = status.number_available or 0

        if observed_generation < generation:
            return False

        return updated_scheduled == desired_scheduled and num_available == desired_scheduled

    start_time = time.time()
    v1_api = client.AppsV1Api()
    watcher = watch.Watch()
    ds_items  = v1_api.list_namespaced_daemon_set(namespace=namespace).items
    ds_status = {ds.metadata.name : False for ds in ds_items }
 
    if not ds_status:
        Logger.info(f"No DaemonSets present in namespace {namespace}")
        return  ds_status

    for ds in ds_items:
        if _is_rollout_complete(ds):
            ds_status[ds.metadata.name] = True
            if all(ds_status.values()):
                Logger.info(f"All DaemonSets in namespace {namespace} already rolled out")
                return ds_status

    for event in watcher.stream(v1_api.list_namespaced_daemon_set,
                                namespace = namespace,
                                timeout_seconds = timeout):
        daemon_set = event["object"]

        if _is_rollout_complete(daemon_set):
            ds_status[daemon_set.metadata.name] = True
            Logger.info(f"Daemonset {daemon_set.metadata.name} rollout complete")
            if all(ds_status.values()):
                watcher.stop()
                break

    if not all(ds_status.values()) and (time.time() - start_time) >= timeout:
        Logger.error(f"Daemonset in namespace {namespace} rollout timeout")     

    return ds_status
    
@log_arguments
def k8_metrics_error(counts, error_list, namespace : str):
    """
    API to artificially set health threshold
    kubectl exec -n kube-amd-gpu metrics-exporter -c metrics-exporter-container -- sh -c 'cat > /tmp/ecc.json <<EOF
    {
        "ID": "0",
        "Fields": [
            "GPU_ECC_UNCORRECT_SEM",
            "GPU_ECC_UNCORRECT_FUSE"
        ],
        "Counts" : [
            1, 2
        ]
    }
    EOF'
    """
    pod_name = k8_get_pod_name("metrics-exporter", namespace)
    api = client.CoreV1Api()
    ecc = {
        "ID": "0",
        "Fields": error_list,
        "Counts": counts,
    }
    ecc_json = json.dumps(ecc)
    cmds = ["metricsclient",
            "rm -f /tmp/ecc.json",
            f"echo '{ecc_json}' > /tmp/ecc.json",
            "cat /tmp/ecc.json",
            "metricsclient --ecc-file-path /tmp/ecc.json"]
    try:
        for cmd in cmds:
            resp = stream.stream(
                api.connect_get_namespaced_pod_exec,
                name=pod_name,
                namespace=namespace,
                container="metrics-exporter-container",
                command=["sh", "-c", cmd],
                stdin=False,
                stdout=True,
                stderr=True,
                tty=False
            )
            Logger.info(f"executed on metrics-exporter:\n{cmd}\n\n")
            Logger.info(f"response from metrics-exporter:\n{resp}")
    except ApiException as e:
        assert True, f"Failed with str(e) while trying to exec {cmd} on {pod_name}"

@log_arguments
def k8_get_node_health(node_name : str, namespace : str):
    api = client.CoreV1Api()
    node: client.V1Node = api.read_node(name=node_name)
    if node.metadata and node.metadata.annotations:
        gpu_state_annotation_key = "metricsexporter.amd.com/gpu.0.state" # Correct annotation key
        if gpu_state_annotation_key in node.metadata.annotations:
            state = node.metadata.annotations[gpu_state_annotation_key]
            Logger.info(f"Found GPU state for node '{node_name}': {state}")
            return state
        else:
            return "unhealthy"
            # You might want to inspect node.status.conditions here too for 'Unhealthy'
            # or related conditions that kubectl describe shows.
    else:
        return "unhealthy"

    if node.status and node.status.conditions:
        Logger.debug("Node conditions:")
        for condition in node.status.conditions:
            # Node condition types typically include "Ready", "MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"
            Logger.debug(f"  Type: {condition.type}, Status: {condition.status}, Reason: {condition.reason}, Message: {condition.message}")
            if condition.type == "Ready" and condition.status == "False":
                Logger.debug(f"Node '{node_name}' is not Ready. Reason: {condition.reason}, Message: {condition.message}")

    return None

@log_arguments
def k8_delete_cluster_role_binding(cluster_role_name):
    """
    API to delete cluster-role

    Example: kubectl delete clusterrole metrics
    """
    rbac_api = client.RbacAuthorizationV1Api()
    try:
        api_response = rbac_api.delete_cluster_role_binding(cluster_role_name)
    except ApiException as e:
        Logger.debug(f"Failed to delete cluster-role-binding {cluster_role_name}, error {e}")
        return -1, "", str(e)
    return 0, "", ""

@log_arguments
def k8_create_service_account(sa_name : str, namespace : str) -> (int, str, str):
    """
    API to create service-account

    Parameters:
    sa_name : name of service-account
    namespace : namespace to create SA
    """
    api = client.CoreV1Api()

    sa = client.V1ServiceAccount(
            metadata = client.V1ObjectMeta(name = sa_name)
         )
    try:
        api_response = api.create_namespaced_service_account(namespace = namespace, body = sa)
    except ApiException as ae:
        return -1, "", str(ae)
    return 0, "", ""

@log_arguments
def k8_delete_service_account(sa_name : str, namespace : str) -> (int, str, str):
    """
    API to delete service-account

    Example: kubectl delete serviceaccount exporter-client
    """
    api = client.CoreV1Api()
    try:
        api_response = api.delete_namespaced_service_account(sa_name, namespace)
    except ApiException as e:
        Logger.debug(f"Failed to delete service-account {sa_name} error : {e}")
        return -1, "", str(e)
    return 0, "", ""

@log_arguments
def k8_create_token(namespace : str, sa_name : str, duration : str) -> (int, str, str):
    """
    API to create token

    Example
    kubectl create token --namespace metrics-reader exporter-client --duration 1h
    """
    duration_in_seconds = 0
    if not duration[-1].isdigit():
        if duration[-1].lower() == 's':
            duration_in_seconds = int(duration[:-1])
        elif duration[-1].lower() == 'm':
            duration_in_seconds = int(duration[:-1]) * 60
        elif duration[-1].lower() == 'h':
            duration_in_seconds = int(duration[:-1]) * 60 * 60
        elif duration[-1].lower() == 'd':
            duration_in_seconds = int(duration[:-1]) * 60 * 60 * 24
    token_request = client.AuthenticationV1TokenRequest(
            spec=client.V1TokenRequestSpec(audiences = ['https://kubernetes.default.svc',
                                                        'https://kubernetes.default.svc.cluster.local'],
                                           expiration_seconds = duration_in_seconds))
    api = client.CoreV1Api()
    try:
        api_response = api.create_namespaced_service_account_token(name = sa_name, 
                                                                   namespace = namespace,
                                                                   body = token_request)
        Logger.debug(f"Created token: {api_response}")
        return api_response.status.token
    except ApiException as e:
        Logger.error(f"Failed to create token for sa-account : {sa_name}, error: {e}")
    return None

@log_arguments
def k8_create_secret(secret_name : str,
                     secret_type : str, **kwargs) -> (int, str, str):
    """
    API to create a secret in kubernetes cluster
    """
    global Logger
    namespace = kwargs.get('namespace', 'default')
    server = kwargs.get('server', "https://index.docker.io/v1/")
    v1 = client.CoreV1Api()

    if secret_type == "docker-registry":
        # Prepare the Docker config JSON structure
        username = kwargs.get('username')
        password = kwargs.get('password')
        docker_config = {
            "auths": {
                server: {
                    "username": username,
                    "password": password,
                    "email": "",
                    "auth": base64.b64encode(f"{username}:{password}".encode()).decode()
                }
            }
        }

        docker_config_json = json.dumps(docker_config).encode()

        # Kubernetes expects this data base64 encoded in a secret under the key ".dockerconfigjson"
        secret_data = {
            ".dockerconfigjson": base64.b64encode(docker_config_json).decode()
        }

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name, namespace=namespace),
            data=secret_data,
            type="kubernetes.io/dockerconfigjson"
        )

    elif secret_type == "tls":
        cert_path = kwargs.get("cert_path")
        key_path = kwargs.get("key_path")
    
        if not cert_path or not key_path:
            return 1, "", "cert_path and key_path are required for TLS secrets"

        with open(cert_path, "r") as f:
            cert_pem = f.read()
        with open(key_path, "r") as f:
            key_pem = f.read()

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name),
            string_data={
                "tls.crt": cert_pem,
                "tls.key": key_pem,
            },
            type="kubernetes.io/tls",
        )
    elif secret_type == "generic":
        data = {}
        for key, value in kwargs.items():
            if key == 'namespace':
                continue
            data[key] = value
        secret = client.V1Secret(
                metadata=client.V1ObjectMeta(name=secret_name),
                string_data=data,
                type="Opaque")
    errmsg = ""
    retval = 0
    try:
        v1.create_namespaced_secret(namespace=namespace, body=secret)
    except ApiException as e:
        retval = 1
        if e.status == 409:
            errmsg = f"Secret '{secret_name}' already exists in namespace '{namespace}'."
        else:
            errmsg = f"Exception when creating secret: {e}"
    return retval, "", errmsg

@log_arguments
def k8_delete_secret(secret_name : str, secret_type : str, namespace : str = "default") -> (int, str, str):
    """
    API to delete a secret in kubernetes cluster
    """
    global Logger
    v1 = client.CoreV1Api()
    errmsg = ""
    retval = 0
    try:
        api_response = v1.delete_namespaced_secret(name = secret_name, 
                                                   namespace = namespace,
                                                   body=client.V1DeleteOptions())
    except ApiException as e:
        if e.status != 404:
            retval = 1
            errmsg = f"Exception when deleting secret {secret_name}, err: {e}"
    return retval, "", errmsg

@log_arguments
def k8_create_auth_file(secret_name : str, namespace : str) -> bool:
    """
    API to create $HOME/.config/containers/auth.json given secret-name and namespace

    This is needed for OpenShift to pull olm-bundle from secure private registry
    """
    # Configuration
    AUTH_JSON_FILE = os.path.join(os.getenv("HOME"), ".config/containers/auth.json")

    try:
        v1 = client.CoreV1Api()
        # 2. Fetch the secret
        Logger.debug(f"Fetching secret {secret_name} from namespace {namespace}...")
        secret = v1.read_namespaced_secret(name=secret_name, namespace=namespace)

        # 3. Extract the '.dockerconfigjson' key
        # The data is a dictionary where values are base64-encoded strings
        encoded_data = secret.data.get(".dockerconfigjson")

        if not encoded_data:
            Logger.error(f"Error: Key '.dockerconfigjson' not found in secret {secret_name}")
            return

        # 4. Decode the Base64 data
        decoded_data = base64.b64decode(encoded_data)

        # 5. Create the directory if it doesn't exist
        os.makedirs(os.path.dirname(AUTH_JSON_FILE), exist_ok=True)

        # 6. Write to the file
        with open(AUTH_JSON_FILE, "wb") as f:
            f.write(decoded_data)

        # Set restricted permissions (600) like a standard auth file
        os.chmod(AUTH_JSON_FILE, 0o600)
        Logger.debug(f"Successfully exported secret to {AUTH_JSON_FILE}")
        return True
    except ApiException as e:
        Logger.error(f"Exception when calling CoreV1Api->read_namespaced_secret: {e}")
    except Exception as e:
        Logger.error(f"An unexpected error occurred: {e}")
    return False

@log_arguments
def k8_get_deviceconfigs_info(namespace : str, deviceconfig_name : str = None) -> Dict:
    """
    API to get deviceconfig information

    Parameters:

    namespace : name-space
    deviceconfig_name : name of the deviceconfigs

    Returns:
    map of deviceconfig-name => deviceconfig-info
    """

    global Logger
    global LogPrettyPrinter
    ret_values = {}
    api = client.CustomObjectsApi()
    try:
        k8_deviceconfig_info = api.list_custom_object_for_all_namespaces(version = "v1alpha1", group = "amd.com", resource_plural = "deviceconfigs")
    except ApiException as e:
        Logger.debug(f"Failed to list deviceconfigs for namespace {namespace}, error: {e}")
        return ret_values

    Logger.debug(f"Status of DeviceConfig CR\n{LogPrettyPrinter.pformat(k8_deviceconfig_info)}")
    for item in k8_deviceconfig_info.get('items', []):
        ret_values[item.get('metadata').get('name')] = item
    return ret_values

@log_arguments
def k8_get_crd(crd_name : str) -> Dict:
    """
    API to retrieve DeviceConfig CRD information post gpu-operator installation

    Parameters:
    crd_name : The name of crd to lookup/filter

    Returns:
    Dict: dict of CRD information or None on error
    """

    global Logger
    global LogPrettyPrinter
    
    api = client.ApiextensionsV1Api()

    try:
        crd_list = api.list_custom_resource_definition().to_dict()
    except ApiException as e:
        Logger.error(f"Error retrieving CRDs from cluster, error: {e}")
        return None

    for crd in crd_list.get('items', None):
        if crd['metadata']['name'] == crd_name:
            return crd
    Logger.debug(f"CRDs from the cluster\n{LogPrettyPrinter.pformat(crd_list)}")
    return None

@log_arguments
def k8_check_crds(crd_names : List[str]) -> List[str]: 
    """
    API to check the presence of specific GPU-operator CRDs.

    Parameters:
        crd_names (List[str]): list of CRD names to verify.

    Returns:
        List[str]: list of names of any CRDs that were not found
    """

    global Logger    
    api = client.ApiextensionsV1Api()

    try:
        crd_list = api.list_custom_resource_definition()
        existing_crds_names = {item.metadata.name for item in crd_list.items}
    except ApiException as e:
        Logger.error(f"Error retrieving CRDs from cluster, error: {e}")
        return None

    missing_crds = [name for name in crd_names if name not in existing_crds_names]
    if missing_crds:
        Logger.warning(f"CRD not found: '{missing_crds}'")
        return  missing_crds

    return  missing_crds

@log_arguments
def k8_run_curl_cmd(k8_cluster : common.k8_cluster, args : List, retry = 10) -> (int, int, str):
    """
    API to run a curl command in the kubernetes cluster to collect information
    """

    global Logger
    global LogPrettyPrinter
    pod_name = f"curl-cmd-pod-{common.generate_8byte_sha('gpu-operator/metrics-exporter')}"
    namespace = "default"

    curl_pod_manifest = client.V1Pod(
        api_version="v1",
        kind="Pod",
        metadata=client.V1ObjectMeta(name=pod_name),
        spec=client.V1PodSpec(
            restart_policy="Never",
            containers=[
                client.V1Container(
                    name="curl-container",
                    image=f"{k8_cluster.k8_registry}/curlimages/curl",
                    command=["curl"],
                    args=args
                )
            ]
        )
    )

    v1 = client.CoreV1Api()
    for _ in range(retry):
        try:
            v1.create_namespaced_pod(body=curl_pod_manifest, namespace=namespace)
            Logger.debug(f"Pod : {pod_name} created. Waiting for completion...")

            cmd_complete = False
            pod_status = v1.read_namespaced_pod_status(name=pod_name, namespace=namespace)
            for _ in range(20):
                Logger.debug(f"Pod : {pod_name} current status : {pod_status.status.phase}")
                if pod_status.status.phase in ["Succeeded", "Failed"]:
                    cmd_complete = True
                    break
                time.sleep(10)
                pod_status = v1.read_namespaced_pod_status(name=pod_name, namespace=namespace)

            exit_code = -1
            if pod_status.status.container_statuses:
                for container_status in pod_status.status.container_statuses:
                    if container_status.name == "curl-container" and container_status.state.terminated:
                        exit_code = container_status.state.terminated.exit_code
                        break
            else:
                Logger.debug("No container statuses found in the pod_status.")

            if pod_status.status.phase in ["Succeeded", "Failed"]:
                # Retrieve logs from the completed Pod
                pod_logs = v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)
                Logger.debug(f"Response of curl-command {args}\n{LogPrettyPrinter.pformat(pod_logs)}")
                return exit_code, pod_logs, ""
            else:
                Logger.warn(f"Unexpected curl-command POD Status\n{LogPrettyPrinter.pformat(pod_status)}")
        except ApiException as e:
            Logger.error(f"Failed to create pod: {pod_name}, error: {e}")
        finally:
            # Clean up: Delete the Pod
            try:
                v1.delete_namespaced_pod(name=pod_name, namespace=namespace, body=client.V1DeleteOptions())
            except ApiException as e:
                Logger.error(f"Failed to delete pod: {pod_name}, error: {e}")
        time.sleep(20)
    return -1, "", ""

@log_arguments
def run_command_on_node(k8_cluster : common.k8_cluster, node_name : str, cmd : List, skip_chroot : bool = False, retry : int = 10, timeout_seconds = 300):
    """
    Runs a command on a specific Kubernetes worker node using an ephemeral debug pod.

    Args:
        k8_cluster (common.k8_cluster): k8_cluster object.
        node_name (str): The name of the worker node.
        cmd (list): The shell command to execute on the node.
        skip_chroot : Flag to skip/include chrooot /host
        retry : Number of retries, default 10

    Returns:
        tuple: A tuple containing (exit_code, logs)
    """
    global Logger
    global LogPrettyPrinter
    v1 = client.CoreV1Api()
    pod_name = f"node-debug-{node_name}-{common.generate_8byte_sha('gpu-operator/metrics-exporter')}"
    namespace = "default"

    full_cmd = []
    if not skip_chroot:
        chroot_cmd = ["chroot", "/host"]
        full_cmd.extend(chroot_cmd)
    full_cmd.extend(cmd)

    # Define the debug pod
    debug_pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {
                "app": "node-debugger",
                "node": node_name
            }
        },
        "spec": {
            "nodeName": node_name,  # Target the specific node
            "restartPolicy": "Never", # Ensure it doesn't restart after command completes
            "hostPID": True,         # Allows access to host process IDs
            "hostNetwork": True,     # Allows access to host network namespace
            "hostIPC": True,         # Allows access to host IPC namespace
            "containers": [
                {
                    "name": "debugger",
                    "image": f"{k8_cluster.k8_registry}/ubuntu",
                    "command": full_cmd,
                    "securityContext": {
                        "privileged": True # Essential for full host access
                    },
                    "volumeMounts": [
                        {
                            "name": "host-root",
                            "mountPath": "/host",
                            "mountPropagation": "Bidirectional"
                        }
                    ]
                }
            ],
            "volumes": [
                {
                    "name": "host-root",
                    "hostPath": {
                        "path": "/",
                        "type": "Directory"
                    }
                }
            ]
        }
    }

    for _ in range(retry):
        try:
            v1.create_namespaced_pod(body=debug_pod_manifest, namespace=namespace)
            Logger.debug(f"Creating debug pod {pod_name} on node '{node_name}'...")

            # Wait for the pod to complete
            start_time = time.time()
            while True:
                pod_status = v1.read_namespaced_pod_status(name=pod_name, namespace=namespace)
                if pod_status.status.phase in ["Succeeded", "Failed"]:
                    Logger.info(f"Pod {pod_name} finished with status: {pod_status.status.phase}")
                    break
                if time.time() - start_time > timeout_seconds:
                    Logger.error(f"Pod {pod_name} timed out after {timeout_seconds} seconds.")
                    return -1, f"Pod {pod_name} timed out after {timeout_seconds} seconds."
                time.sleep(5)

            # Get container status to check exit code
            exit_code = -1
            if pod_status.status.container_statuses:
                for container_status in pod_status.status.container_statuses:
                    if container_status.name == "debugger" and container_status.state.terminated:
                        exit_code = container_status.state.terminated.exit_code
                        break
            else:
                Logger.debug("No container statuses found in the pod.")

            # Get logs
            logs = v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)
            Logger.debug(f"Logs from {pod_name}:\n{LogPrettyPrinter.pformat(logs)}")
            return exit_code, logs
        except client.ApiException as e:
            Logger.error(f"Kubernetes API Error: {e}")
            # Attempt to get logs if pod was created but failed
            try:
                logs = v1.read_namespaced_pod_log(name=pod_name, namespace=namespace)
                Logger.info(f"Logs (before error): {logs}")
            except Exception as log_e:
                Logger.error(f"Could not retrieve logs after API error: {log_e}")
            return -1, f"Kubernetes API Error: {e}"
        except Exception as e:
            Logger.error(f"An unexpected error occurred: {e}")
            return -1, f"An unexpected error occurred: {e}"
        finally:
            # Delete the pod
            Logger.debug(f"Deleting pod {pod_name}...")
            v1.delete_namespaced_pod(name=pod_name, namespace=namespace,
                                     body=client.V1DeleteOptions(propagation_policy='Foreground',
                                                                 grace_period_seconds=0))
    return

@log_arguments
def exec_command_in_pod(namespace : str, cmds : List, pod_name : str, container_name : str = None):
    """
    Exec a command inside a specific container within a Kubernetes pod.

    Args:
        namespace (str): The namespace of the pod.
        cmds (list): A list of strings representing the command and its arguments.
                        For example: ["ls", "-l", "/tmp"]
        pod_name (str): The name of the pod.
        container_name (str, optional): The name of the container within the pod
                                        to execute the command in. If None, it
                                        defaults to the first container if only one
                                        exists.
    Returns:
        tuple: A tuple containing (stdout, stderr) of the executed command.
    """
    for _ in range(3):
        try:
            # Create a CoreV1Api client
            v1 = client.CoreV1Api()

            # Execute the command
            # _preload_content=False is crucial for interactive sessions or streaming output
            # For simple command execution, you might set it to True to get all output at once.
            resp = stream.stream(
                    v1.connect_get_namespaced_pod_exec,
                    pod_name,
                    namespace,
                    command=cmds,
                    container=container_name,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                    _preload_content=True)  # Set to True to get all content at once

            # The 'resp' object contains the combined stdout and stderr if _preload_content is True
            # Otherwise, you would read from the stream interactively.
            return 0, resp, None
        except client.ApiException as e:
            Logger.error(f"Error executing command {cmds}, error: {e}")
            time.sleep(10)
        except Exception as e:
            Logger.error(f"An unexpected error occurred while running command {cmds}, error: {e}")
            time.sleep(10)
    return -1, None, f"Unexpected error"

@log_arguments
def reboot_node(k8_cluster : common.k8_cluster, node_name : str):
    """
    Reboot node using run_command_on_node API and check for node status.conditions to declare as ready
    Args:
        k8_cluster (common.k8_cluster): k8 Cluster
        node_name (str): name of the node.
    """
    ret_code, _ = k8_cordon_node(node_name)
    if ret_code != 0:
        return ret_code
    ret_code, _ = run_command_on_node(k8_cluster, node_name, ["systemctl", "reboot"], timeout_seconds = 30)
    # For now ignore ret_code
    # Check for status to be declared as NotReady
    for _ in range(10):
        ret_code, k8_nodes = k8_get_nodes()
        if ret_code != 0:
            return ret_code

        reboot_success = False
        for node in k8_nodes:
            if node['metadata']['labels'].get('feature.node.kubernetes.io/amd-gpu', 'false') != 'true':
                continue

            if node['metadata']['name'] != node_name:
                continue

            for entry in node['status']['conditions']:
                if entry.get('type', 'NotReady') == 'Ready':
                    if entry['status'] != 'True':
                        reboot_success = True
                    break
            break
        if reboot_success:
            break
        time.sleep(20)

    if not reboot_success:
        Logger.error(f"Failed to reboot node {node_name}")
        return -1
    Logger.info(f"Node {node_name} successfully rebooted, sleep for 240s")
    time.sleep(240)

    # Check for status to be declared as Ready
    for _ in range(10):
        ret_code, k8_nodes = k8_get_nodes()
        if ret_code != 0:
            return ret_code

        node_online = False
        for node in k8_nodes:
            if node['metadata']['labels'].get('feature.node.kubernetes.io/amd-gpu', 'false') != 'true':
                continue

            if node['metadata']['name'] != node_name:
                continue

            for entry in node['status']['conditions']:
                if entry.get('type', 'NotReady') == 'Ready':
                    if entry['status'] == 'True':
                        node_online = True
                    break
            break

        if node_online:
            break
        time.sleep(20)
    if not node_online:
        Logger.error(f"Node {node_name} failed to come online - fatal error")
        return -1
    Logger.info(f"Node {node_name} is up")
    ret_code, _ = k8_uncordon_node(node_name)
    if ret_code != 0:
        Logger.error(f"Failed to uncordon node - {node_name}")
        return ret_code
    return 0

@log_arguments
def k8_list_subscriptions() -> (int, List, str):
    """
    API to list subscriptions (items) in openshift.
    """
    global Logger

    custom_objects_api = client.CustomObjectsApi()
    group = "operators.coreos.com"
    version = "v1alpha1"
    plural = "subscriptions"

    # Retry logic for IncompleteRead errors
    max_retries = 5
    retry_delay = 2  # seconds

    for attempt in range(max_retries):
        try:
            subscriptions = custom_objects_api.list_cluster_custom_object(
                group=group,
                version=version,
                plural=plural,
                _request_timeout=60
            )
            return 0, subscriptions.get("items", []), ""
        except (ProtocolError, IncompleteRead) as e:
            if attempt < max_retries - 1:
                Logger.warning(f"IncompleteRead error on attempt {attempt + 1}/{max_retries}, retrying in {retry_delay}s... Error: {e}")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
            else:
                Logger.error(f"Failed to list subscriptions after {max_retries} attempts due to IncompleteRead: {e}")
                return -1, [], str(e)
        except ApiException as e:
            Logger.error(f"Failed to list CR, error: {e}")
            return -1, [], str(e)
        except Exception as e:
            Logger.error(f"Unexpected error listing subscriptions: {e}")
            return -1, [], str(e)
    return 0, [], ""

@log_arguments
def k8_list_catalogsources() -> (int, List, str):
    """
    API to list catalogsources (items) in openshift.
    """
    global Logger

    custom_objects_api = client.CustomObjectsApi()
    group = "operators.coreos.com"
    version = "v1alpha1"
    plural = "catalogsources"

    # Retry logic for IncompleteRead errors
    max_retries = 5
    retry_delay = 2  # seconds

    for attempt in range(max_retries):
        try:
            catalogsources = custom_objects_api.list_cluster_custom_object(
                group=group,
                version=version,
                plural=plural,
                _request_timeout=60
            )
            return 0, catalogsources.get("items", []), ""
        except (ProtocolError, IncompleteRead) as e:
            if attempt < max_retries - 1:
                Logger.warning(f"IncompleteRead error on attempt {attempt + 1}/{max_retries}, retrying in {retry_delay}s... Error: {e}")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
            else:
                Logger.error(f"Failed to list catalogsources after {max_retries} attempts due to IncompleteRead: {e}")
                return -1, [], str(e)
        except ApiException as e:
            Logger.error(f"Failed to list CR, error: {e}")
            return -1, [], str(e)
        except Exception as e:
            Logger.error(f"Unexpected error listing catalogsources: {e}")
            return -1, [], str(e)
    return 0, [], ""

@log_arguments
def k8_list_clusterserviceversions() -> (int, List, str):
    """
    API to list catalogsources (items) in openshift.
    """
    global Logger

    custom_objects_api = client.CustomObjectsApi()
    group = "operators.coreos.com"
    version = "v1alpha1"
    plural = "clusterserviceversions"

    # Retry logic for IncompleteRead errors
    max_retries = 5
    retry_delay = 2  # seconds

    for attempt in range(max_retries):
        try:
            # Increase timeout for large responses
            custom_objects_api.api_client.rest_client.pool_manager.connection_pool_kw['timeout'] = 60
            catalogsources = custom_objects_api.list_cluster_custom_object(
                group=group,
                version=version,
                plural=plural,
                _request_timeout=60  # 60 second timeout
            )
            return 0, catalogsources.get("items", []), ""
        except (ProtocolError, IncompleteRead) as e:
            if attempt < max_retries - 1:
                Logger.warning(f"IncompleteRead error on attempt {attempt + 1}/{max_retries}, retrying in {retry_delay}s... Error: {e}")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
            else:
                Logger.error(f"Failed to list clusterserviceversions after {max_retries} attempts due to IncompleteRead: {e}")
                return -1, [], str(e)
        except ApiException as e:
            Logger.error(f"Failed to list CR, error: {e}")
            return -1, [], str(e)
        except Exception as e:
            Logger.error(f"Unexpected error listing clusterserviceversions: {e}")
            return -1, [], str(e)
    return 0, [], ""

@log_arguments
def k8_get_services(namespace : str) -> (int, List, str):
    """
    API to list services (items) in a given namespace
    """

    api = client.CoreV1Api()
    try:
        services = api.list_namespaced_service(namespace = namespace).to_dict()
        if services.get("items", None):
            return 0, services["items"], ""
    except client.ApiException as ae:
        Logger.error(f"Failed to list services in namespace : {namespace}, error : {ae}")
        return -1, [], str(ae)
    return 0, [], ""

@log_arguments
def k8_patch_serviceaccount(namespace : str, sa_name : str, patch_body : Dict) -> (int, str, str):
    """
    API to patch service-account with given patch
    """
    global Logger

    try:
        api = client.CoreV1Api()
        resp = api.patch_namespaced_service_account(name = sa_name, namespace = namespace, body = patch_body)
    except client.ApiException as ae:
        Logger.error(f"Failed to patch service-account : {sa_name} of {namespace}, error : {ae}")
        return -1, "", str(ae)
    except config.ConfigException as ce:
        Logger.error(f"Failed to patch service-account : {sa_name} of {namespace}, error : {ce}")
        return -1, "", str(ce)
    return 0, str(resp), ""

@log_arguments
def k8_wait_for_cluster_ready(minikube : bool = False) -> (int):
    """
    API to wait for (all) nodes to be declared Ready in kubernetes
    """
    # Check for status to be declared as Ready
    for _ in range(30):
        ret_code, k8_nodes = k8_get_nodes()
        if ret_code != 0 or k8_nodes is None:
            if minikube:
                time.sleep(30) # If this is a minikube, we have lost connectivity to the cluster itself
            else:
                return ret_code

        status_list = list()
        for node in k8_nodes:
            ready = False
            for entry in node['status']['conditions']:
                if entry.get('type', 'NotReady') == 'Ready':
                    if entry['status'] == 'True':
                        Logger.info(f"Node {node['metadata']['name']} is up")
                        ready = True
                    break
            status_list.append(ready)
        if all(status_list):
            return 0
        time.sleep(20)
    Logger.error(f"Some of the nodes of cluster failed to come online - fatal error")
    return -1

@log_arguments
def k8_get_configmap(namespace: str, configmap_name: str):
    """
    API to get a specific configmap in a k8-cluster
    Equivalent to: kubectl get configmap <name> -n <ns>
    """
    global Logger
    api = client.CoreV1Api()
    try:
        api_response = api.read_namespaced_config_map(configmap_name, namespace)
        return 0, api_response, ""
    except ApiException as e:
        Logger.debug(f"Config-map {configmap_name} not found or error: {e}")
        return -1, None, str(e)

@log_arguments
def k8_patch_node_status(node_name, status_body):
    """
    Patches the node status subresource.
    """
    global Logger
    v1 = client.CoreV1Api()
    try:
        api_response = v1.patch_node_status(name=node_name, body=status_body)
        Logger.info(f"Successfully patched status for node: {node_name}")
        return 0, api_response, ""
    except ApiException as e:
        error_msg = f"K8s ApiException ({e.status}): {e.reason} - {e.body}"
        Logger.debug(error_msg)
        return e.status, None, error_msg
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        Logger.error(error_msg)
        return -1, None, error_msg


@log_arguments
def k8_patch_workflow_config(namespace: str, configmap_name: str, patch_body: dict):
    """
    patch a workflow entry using the exact YAML structure.

    """
    global Logger
    api = client.CoreV1Api()
    target_node = patch_body.get("nodeCondition")
    if not target_node:
        return -1, None, "patch_body must contain 'nodeCondition' to identify which entry to fix."

    try:
        cm = api.read_namespaced_config_map(name=configmap_name, namespace=namespace)
        workflow_str = cm.data.get("workflow", "")
        if not workflow_str:
            return 404, None, "Workflow field missing in ConfigMap"

        workflow_list = yaml.safe_load(workflow_str)

        found = False
        for entry in workflow_list:
            if entry.get("nodeCondition") == target_node:
                for key, value in patch_body.items():
                    if isinstance(value, dict) and key in entry:
                        entry[key].update(value)
                    else:
                        entry[key] = value
                found = True
                break
        
        if not found:
            return 404, None, f"Node condition '{target_node}' not found in the workflow list."

        updated_workflow_str = yaml.dump(workflow_list, default_flow_style=False)
        api_body = {"data": {"workflow": updated_workflow_str}}
        
        api_response = api.patch_namespaced_config_map(
            name=configmap_name,
            namespace=namespace,
            body=api_body
        )
        
        Logger.info(f"Successfully patched {target_node} in {configmap_name}")
        return 0, api_response, ""

    except ApiException as e:
        return e.status, None, f"ApiException: {e.body}"
    except Exception as e:
        return -1, None, str(e)
