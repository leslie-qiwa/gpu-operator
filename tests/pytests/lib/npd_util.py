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
import logging
import json
import lib.k8_util as k8_util
import lib.helm_util as helm_util

# Configuration settings
NPD_NAMESPACE = "node-problem-detector" #"kube-system"
NPD_APP_NAME = "amdgpu-npd-app"
NPD_SA_NAME = "npd-service-account"
NPD_ROLE_NAME = "npd-amdgpu-role"
NPD_ROLE_BINDING_NAME = "npd-amdgpu-role-binding"

DEFAULT_NPD_DAEMONSET = {
    "apiVersion": "apps/v1",
    "kind": "DaemonSet",
    "metadata": {"name": NPD_APP_NAME, "namespace": NPD_NAMESPACE},
    "spec": {
        "selector": {"matchLabels": {"app": NPD_APP_NAME}},
        "template": {
            "metadata": {"labels": {"app": NPD_APP_NAME}},
            "spec": {
                "serviceAccountName": NPD_SA_NAME,
                "containers": [{
                    "name": NPD_APP_NAME,
                    "image": "registry.k8s.io/node-problem-detector/node-problem-detector:v0.8.15",
                    "args": [
                        "--logtostderr",
                    ],
                    "securityContext": {"privileged": True},
                    "volumeMounts": [
                        {
                            "name": "config",
                            "mountPath": "/config",
                            "readOnly": True
                        },
                        {
                            "name": "log",
                            "mountPath": "/var/log",
                            "readOnly": True
                        },
                    ]
                }],
                "volumes": [
                    {
                        "name": "log",
                        "hostPath": {
                            "path": "/var/log"
                        }
                    },
                    {
                        "name": "config",
                        "configMap": {
                            "name": f"{NPD_APP_NAME}-config"
                        }
                    }
                ]
            }
        }
    }
}

# Default configmap defn
# --- 2. CONFIGMAP: The GPU Plugin ---
DEFAULT_NPD_CONFIGMAP = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": f"{NPD_APP_NAME}-config", "namespace": NPD_NAMESPACE},
}

Logger = logging.getLogger("lib.npd")

def _run_tasks(task_list, stop_on_failure = True) -> int:
    final_ret_code = 0
    for cb_func, error_msg, args in task_list:
        ret_code, ret_stdout, ret_stderr = cb_func(*args)
        if ret_code != 0:
            Logger.warning(f"{error_msg}, error : {ret_stderr}")
            if stop_on_failure:
                return ret_code
            else:
                final_ret_code = ret_code
    return final_ret_code

def init_npd_k8(gpu_cluster) -> (int, str, str):
    """
    API to configure default service-account and rbac
    """

    # Lets cleanup for any trace from previous deployment
    fini_npd_k8(gpu_cluster)
    # --- 1. RBAC: ServiceAccount ---

    rules = list()
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["nodes", "pods", "services"], verbs=["get", "list", "watch"], api_groups=[""]))
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["events"], verbs=["create", "patch"], api_groups=[""]))
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["nodes/status"], verbs=["patch"], api_groups=[""]))
    rules.extend(k8_util.k8_create_rules_from_endpoint_list([("/metrics", "get"), ("/gpumetrics", "get"), ("/inbandraserrors", "get")]))
    todo_tasks = [
        (k8_util.k8_create_namespace, f"Failed to create namespace : {NPD_NAMESPACE}", (NPD_NAMESPACE,)),
        (k8_util.k8_create_service_account, f"Failed to create service-account {NPD_APP_NAME}", (NPD_SA_NAME, NPD_NAMESPACE,)),
        (k8_util.k8_create_cluster_role, f"Failed to create cluster-roles", (NPD_ROLE_NAME, rules,)),
        (k8_util.k8_create_role_binding, "Failed to create npd-role binding",
         (NPD_ROLE_BINDING_NAME, NPD_NAMESPACE, NPD_ROLE_NAME, NPD_SA_NAME,))
    ]

    ret_code = _run_tasks(todo_tasks)
    if ret_code != 0:
        return ret_code, "", "Failed to setup/create service-account, roles and role-binding"


    # --- 3. DAEMONSET: The NPD Workload ---
    Logger.info(f"Deploy/Configure node-problem-detector with default config-map")
    todo_tasks = [
        (k8_util.k8_create_configmap, "Failed to init npd config-map",
         (NPD_NAMESPACE, DEFAULT_NPD_CONFIGMAP["metadata"]["name"], None,)),
        (k8_util.k8_patch_daemonset, "Failed to apply/patch daemonset",
         (NPD_APP_NAME, NPD_NAMESPACE, DEFAULT_NPD_DAEMONSET,))
    ]
    ret_code = _run_tasks(todo_tasks)
    if ret_code != 0:
        return ret_code, "", "Failed to deploy/configure node-problem-detector with default config-map"

    Logger.info(f"Successfully deployed node-problem-detector {NPD_APP_NAME} in namespace {NPD_NAMESPACE}")
    return ret_code, "", ""

def fini_npd_k8(gpu_cluster) -> (int, str, str):
    """
    API to remove/uninstall node-problem-detector and custom plugin
    """
    global Logger

    Logger.info(f"Removing npd amdgpuhealth custom-plugin from the cluster")

    cleanup_tasks = [
        (k8_util.k8_delete_configmap, "Failed to delete config-map", (NPD_NAMESPACE, f"{NPD_APP_NAME}-config",)),
        (k8_util.k8_delete_daemonset, "Failed to delete daemonset", (NPD_NAMESPACE, NPD_APP_NAME,))
    ]

    ret_code = _run_tasks(cleanup_tasks, stop_on_failure = False)
    if ret_code != 0:
        Logger.warning("Failed to delete npd config-map and daemonset - ignoring error for now")

    Logger.info(f"Removing npd service-account, cluster-role and role-bindings")

    cleanup_tasks = [
        (k8_util.k8_delete_cluster_role_binding, "Failed to delete cluster-role-binding", (NPD_ROLE_BINDING_NAME,)),
        (k8_util.k8_delete_cluster_role, "Failed to delete cluster-role", (NPD_ROLE_NAME,)),
        (k8_util.k8_delete_service_account, "Failed to delete service-account", (NPD_SA_NAME, NPD_NAMESPACE,)),
    ]

    ret_code = _run_tasks(cleanup_tasks, stop_on_failure = False)
    if ret_code != 0:
        Logger.warning("Failed to cleanup npd cluster role-binding/cluster-role/service-account - ignored")
    return 0, "", ""

def init_npd_oc(gpu_cluster) -> (int, str, str):
    """
    oc create clusterrolebinding npd-privileged-scc \
    --clusterrole=system:openshift:scc:privileged \
    --serviceaccount=node-problem-detector:npd

    oc create clusterrole npd-pod-endpoint-access \
    --verb=get,list,watch --resource=pods,endpoints

    oc create clusterrolebinding npd-pod-endpoint-access-binding \
    --clusterrole=npd-pod-endpoint-access \
    --serviceaccount=node-problem-detector:npd

    helm install npd oci://ghcr.io/deliveryhero/helm-charts/node-problem-detector \
    --version 2.4.0 \
    -n node-problem-detector \
    --set serviceAccount.name=npd \
    --set serviceAccount.create=false
    """

    OCI_NPD_HELMCHART_URL = "oci://ghcr.io/deliveryhero/helm-charts/node-problem-detector"
    OCI_NPD_HELMCHART_VERSION = "2.4.0"

    rules = list()
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["events"], verbs=["get", "list", "watch"], api_groups=[""]))
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["nodes", "pods", "services", "endpoints"], verbs=["get", "list", "watch"], api_groups=[""]))
    rules.append(k8_util.k8_create_rules_from_verbs(resources=["nodes/status"], verbs=["get", "list", "watch"], api_groups=[""]))
    todo_tasks = [
        (k8_util.k8_create_namespace, f"Failed to create namespace : {NPD_NAMESPACE}", (NPD_NAMESPACE,)),
        (k8_util.k8_create_service_account, f"Failed to create service-account {NPD_APP_NAME}", (NPD_SA_NAME, NPD_NAMESPACE,)),
        # Bind to existing system:openshift:scc:privileged ClusterRole instead of trying to create it
        (k8_util.k8_create_cluster_role_binding, "Failed to create cluster-role binding for privileged SCC",
         ("npd-scc-privileged-binding", "system:openshift:scc:privileged", NPD_SA_NAME, NPD_NAMESPACE,))
    ]

    ret_code = _run_tasks(todo_tasks)
    if ret_code != 0:
        return ret_code, "", "Failed to init privileged roles"

    rules.extend(k8_util.k8_create_rules_from_endpoint_list([("/metrics", "get"), ("/gpumetrics", "get"), ("/inbandraserrors", "get")]))
    todo_tasks = [
        (k8_util.k8_create_cluster_role, f"Failed to create npd-role", (NPD_ROLE_NAME, rules,)),
        (k8_util.k8_create_role_binding, "Failed to create npd-role binding",
         (NPD_ROLE_BINDING_NAME, NPD_NAMESPACE, NPD_ROLE_NAME, NPD_SA_NAME,))
    ]

    ret_code = _run_tasks(todo_tasks)
    if ret_code != 0:
        return ret_code, "", "Failed to init endpoint access/roles"

    opts = {
      "serviceAccount.name" : NPD_SA_NAME,
      "serviceAccount.create" : "false",
    }
    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(gpu_cluster, "npd",
                                                              NPD_NAMESPACE, OCI_NPD_HELMCHART_URL, OCI_NPD_HELMCHART_VERSION,
                                                              values_yaml = None, **opts)
    return ret_code, ret_stdout, ret_stderr

def fini_npd_oc(gpu_cluster) -> (int, str, str):
    ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(gpu_cluster, "npd", NPD_NAMESPACE)
    return ret_code, ret_stdout, ret_stderr

def deploy_npd_amdgpuhealth_plugin(metric_type : str, metric_to_test : str, threshold : int):
    # Note the fix: strings for max_output_length/concurrency
    amdgpu_config = {
        "plugin": "custom",
        "pluginConfig": {
            "invoke_interval": "30s",
            "timeout": "15s",
            "max_output_length": 80,
            "concurrency": 3,
            "enable_message_change_based_condition_update": False
        },
        "source": "amdgpu-custom-plugin-monitor",
        "metricsReporting": True,
        "conditions": [{"type": "AMDGPUProblem", "reason": "AMDGPUIsUp", "message": "AMD GPU is up"}],
        "rules": [{
            "type": "permanent",
            "condition": "AMDGPUProblem",
            "reason": "AMDGPUIsDown",
            "path": "/amd-metrics-exporter/amdgpuhealth", # Path inside the container
            "args": ["query", f"{metric_type}", f"-m={metric_to_test}", f"-t={threshold}"],
            "timeout": "10s"
        }]
    }

    cm_body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"{NPD_APP_NAME}-config", "namespace": NPD_NAMESPACE},
        "data": {
            "amdgpuhealth.json": json.dumps(amdgpu_config, indent=2)
        }
    }

    ds_body = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": NPD_APP_NAME, "namespace": NPD_NAMESPACE},
        "spec": {
            "selector": {"matchLabels": {"app": NPD_APP_NAME}},
            "template": {
                "metadata": {"labels": {"app": NPD_APP_NAME}},
                "spec": {
                    "serviceAccountName": NPD_SA_NAME,
                    "containers": [{
                        "name": NPD_APP_NAME,
                        "image": "registry.k8s.io/node-problem-detector/node-problem-detector:v0.8.15",
                        "args": [
                            "--logtostderr",
                            # FIX: Use custom-plugin-monitor flag
                            "--config.custom-plugin-monitor=/config/amdgpuhealth.json"
                        ],
                        "securityContext": {"privileged": True},
                        "volumeMounts": [
                            {"name": "config", "mountPath": "/config", "readOnly": True},
                            {"name": "log", "mountPath": "/var/log", "readOnly": True},
                            {"name": "amd-metrics-exporter", "mountPath" : "/amd-metrics-exporter", "readOnly" : True}
                        ]
                    }],
                    "volumes": [
                        {
                            "name": "config",
                            "configMap": {
                                "name": f"{NPD_APP_NAME}-config",
                                "defaultMode": 0o755, # OCTAL for rwxr-xr-x
                                "items" : [
                                    {
                                        "key" : "amdgpuhealth.json",
                                        "path" : "amdgpuhealth.json",
                                        "mode" : 0o644
                                    }
                                ]
                            }
                        },
                        {
                            "name": "log",
                            "hostPath": {
                                "path": "/var/log"
                            }
                        },
                        {
                            "name": "amd-metrics-exporter",
                            "hostPath": {
                                "path" : "/var/lib/amd-metrics-exporter"
                            }
                        }
                    ]
                }
            }
        }
    }

    Logger.info(f"Deploy/Configure amdgpuhealth plugin in node-problem-detector")

    todo_tasks = [
        (k8_util.k8_patch_config_map, "Failed to apply/patch config-map", (cm_body["metadata"]["name"], NPD_NAMESPACE, cm_body,)),
        (k8_util.k8_patch_daemonset, "failed to apply/patch daemonset", (NPD_APP_NAME, NPD_NAMESPACE, ds_body,))
    ]

    ret_code = _run_tasks(todo_tasks)
    return ret_code

def remove_npd_amdgpuhealth_plugin():
    Logger.info(f"Remove/Restore node-problem-detector")

    #todo_tasks = [
    #    (k8_util.k8_patch_config_map, "Failed to apply/patch config-map", 
    #     (DEFAULT_NPD_CONFIGMAP["metadata"]["name"], NPD_NAMESPACE, DEFAULT_NPD_CONFIGMAP,)),
    #    (k8_util.k8_patch_daemonset, "failed to apply/patch daemonset", 
    #     (NPD_APP_NAME, NPD_NAMESPACE, DEFAULT_NPD_DAEMONSET,))
    #]

    #ret_code = _run_tasks(todo_tasks)
    #if ret_code == 0:
    #    Logger.info("Successfully removed custom-plugins from node-problem-detector")
    #else:
    #    Logger.error("Failed to remove custom-plugins from node-problem-detector")
    #return ret_code
    return 0

