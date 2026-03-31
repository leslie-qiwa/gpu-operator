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
import os
import json
import logging
import pytest
import subprocess
import pprint
from functools import wraps
import lib.common as common

Logger = logging.getLogger("lib.helmutil")
LogPrettyPrinter = pprint.PrettyPrinter(indent = 2)

def log_arguments(func):
    global Logger
    global LogPrettyPrinter

    @wraps(func)
    def wrapper(*args, **kwargs):
        Logger.debug(f"Function::'{func.__name__}' with args: {args} kwargs: {kwargs}")
        return func(*args, **kwargs)
    return wrapper

@log_arguments
def helm_list(k8_cluster : common.k8_cluster, namespace : str) -> (int, str, str):
    """
    API to list installed helm-charts in a given namespace
    
    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    namespace : The name of namespace

    Returns:
    int: return-code, 0 for success else failure
    stdout : stdout from command execution
    stderr : stderr from command execution
    """
    global Logger
    cmd = ["helm", "list", "-a", "--namespace", namespace, "-o", "json"]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    cmd_resp = subprocess.run(cmd, check=False,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_add_repo(k8_cluster : common.k8_cluster, repo_name : str, repo_url : str) -> None:
    """
    API to add helm repo

    For example, following commands will be run:
    helm repo add rocm <repo>
    helm repo update

    Parameters:
    k8_cluster : intance of lib.common.k8_cluster
    repo_name  : Name of the repo
    repo_url   : repo url
    """
    cmd = ["helm", "repo", "add", repo_name, repo_url]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    cmd_resp = subprocess.run(cmd, check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                encoding='utf-8')
    ret_code = cmd_resp.returncode
    assert ret_code == 0, f"Failed to add helm repo {repo_name}, stdout : {ret_stdout} stderr: {ret_stderr}"

    cmd = ["helm", "repo", "update"]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    cmd_resp = subprocess.run(cmd, check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                encoding='utf-8')
    ret_code = cmd_resp.returncode
    assert ret_code == 0, f"Failed to update helm repo {repo_name}, stdout : {cmd_resp.stdout} stderr: {cmd_resp.stderr}"
    return

@log_arguments
def helm_install(k8_cluster : common.k8_cluster, release_name : str, namespace : str, helm_chart_path : str, version : str, values_yaml : str, **kwargs) -> (int, str, str):
    """
    API to install helm-chart

    For example, following command will be run:
    helm install <release-name> <path-to-helm-chart>
        -n kube-amd-gpu --create-namespace --version=<version>
        --set controllerManager.manager.image.repository=registry.test.pensando.io:5000/amd-gpu-operator
        --set controllerManager.manager.image.tag=latest 

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release_name : release-name to use for helm-chart installation
    namespace : namespace in which to install helm-chart
    helm_chart_path : path to helm chart (file or repo path)
    version : version of the helm-chart to install
    values_yaml : values.yaml file

    Returns:
    ret_code   : Return code for command execution. 0 for success else failure
    ret_stdout : Stdout from command execution
    ret_stderr : Stderr from command execution
    """

    global Logger
    cmd = ["helm", "install", "--debug", f"{release_name}", f"{helm_chart_path}"]
    cmd.extend(["-n", f"{namespace}", "--create-namespace"])
    if version:
        cmd.extend([f"--version={version}"])

    for key, value in kwargs.items():
        cmd.extend(["--set", f"{key}={value}"])

    if release_name == 'gpu-operator':
        if os.getenv("GPU_DEVICE") == "VF":
            node_selection = {
                "feature.node.kubernetes.io/amd-gpu"    : None,
                "feature.node.kubernetes.io/amd-vgpu"   : "true",
            }
        else:
            node_selection = {
                "feature.node.kubernetes.io/amd-gpu"    : "true",
                "feature.node.kubernetes.io/amd-vgpu"   : None,
            }
        cmd.extend(["--set-json", f"deviceConfig.spec.selector={json.dumps(node_selection)}"])

    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    if values_yaml:
        if not os.path.exists(values_yaml):
            return -1, "", f"Missing values.yaml : {values_yaml}"
        cmd.extend(["-f", values_yaml])
    Logger.debug(f"helm-install command: {' '.join(cmd)}")
    cmd_resp = subprocess.run(cmd, check=False,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_upgrade(k8_cluster : common.k8_cluster, release_name : str, namespace : str, helm_chart_path : str, version : str, values_yaml : str, **kwargs) -> (int, str, str):
    """
    API to upgrade helm-chart

    For example, following command will be run:
    helm upgrade <release-name> <path-to-helm-chart>
        -n kube-amd-gpu --version=<version>
        --set controllerManager.manager.image.repository=registry.test.pensando.io:5000/amd-gpu-operator
        --set controllerManager.manager.image.tag=latest

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release_name : release-name of the helm-chart to upgrade
    namespace : namespace in which helm-chart is installed
    helm_chart_path : path to helm chart (file or repo path)
    version : version of the helm-chart to upgrade to
    values_yaml : values.yaml file

    Returns:
    ret_code   : Return code for command execution. 0 for success else failure
    ret_stdout : Stdout from command execution
    ret_stderr : Stderr from command execution
    """

    global Logger
    cmd = ["helm", "upgrade", "--debug", f"{release_name}", f"{helm_chart_path}"]
    cmd.extend(["-n", f"{namespace}"])
    if version:
        cmd.extend([f"--version={version}"])

    for key, value in kwargs.items():
        cmd.extend(["--set", f"{key}={value}"])

    if release_name == 'gpu-operator':
        if os.getenv("GPU_DEVICE") == "VF":
            node_selection = {
                "feature.node.kubernetes.io/amd-gpu"    : None,
                "feature.node.kubernetes.io/amd-vgpu"   : "true",
            }
        else:
            node_selection = {
                "feature.node.kubernetes.io/amd-gpu"    : "true",
                "feature.node.kubernetes.io/amd-vgpu"   : None,
            }
        cmd.extend(["--set-json", f"deviceConfig.spec.selector={json.dumps(node_selection)}"])

    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    if values_yaml:
        if not os.path.exists(values_yaml):
            return -1, "", f"Missing values.yaml : {values_yaml}"
        cmd.extend(["-f", values_yaml])
    Logger.debug(f"helm-upgrade command: {' '.join(cmd)}")
    cmd_resp = subprocess.run(cmd, check=False,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_uninstall(k8_cluster : common.k8_cluster, release_name : str, namespace : str) -> (int, str, str):
    """
    API to uninstall helm-chart

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release_name : Release-name of the helm-chart
    namespace : name-space in which helm-chart was installed

    Returns:
    int: return-code, 0 for success else failure
    stdout: stdout of command execution
    stderr: stderr of command execution
    """

    global Logger
    cmd = ["helm", "uninstall", "--debug", f"{release_name}", "--namespace", f"{namespace}"]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    cmd_resp = subprocess.run(cmd, check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def helm_cleanup(k8_cluster : common.k8_cluster, release_name : str, namespace : str) -> (int, str, str):
    """
    API to do forceful cleanup, if helm-uninstall resulted in failure

    Following command will be run: "helm uninstall <release-name> --namespace <namespace> --no-hooks"

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release_name : helm-chart release name
    namespace : namespace to use

    Returns:
    int: return-code, 0 for success else failure
    stdout: stdout of command execution
    stderr: stderr of command execution
    """
    global Logger
    cmd = ["helm", "uninstall", f"{release_name}", "--namespace", f"{namespace}", "--no-hooks"]
    if k8_cluster.k8_kube_config:
        cmd.extend(["--kubeconfig", k8_cluster.k8_kube_config])

    cmd_resp = subprocess.run(cmd, check=False,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                encoding='utf-8')
    return cmd_resp.returncode, cmd_resp.stdout, cmd_resp.stderr

@log_arguments
def is_helm_chart_deployed(k8_cluster : common.k8_cluster, release_name : str, namespace : str) -> bool:
    """
    API to check if helm-chart is deployed. 

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release-name: release-name used for helm-chart
    namespace : namespace in which helm-chart is installed

    Returns:
    bool : True if chart is deployed else False
    """
    ret_code, ret_stdout, ret_stderr = helm_list(k8_cluster, namespace)

    """
    Sample output
    vm@master-node:~/sandbox$ helm list -n kube-amd-gpu -o json | jq .
    [
      {
        "name": "gpu-operator",
        "namespace": "kube-amd-gpu",
        "revision": "1",
        "updated": "2024-12-11 10:04:56.122288711 +0000 UTC",
        "status": "failed", or "deployed",
        "chart": "gpu-operator-v1.0.0",
        "app_version": "v1.0.0"
      }
    ]
    """
    for chart in json.loads(ret_stdout):
        if chart['name'] == release_name and chart['status'] != 'uninstalling':
            return True
    return False

@log_arguments
def is_helm_chart_healthy(k8_cluster : common.k8_cluster, release_name : str, namespace : str) -> bool:
    """
    API to check if installed helm-chart is healthy

    Parameters:
    k8_cluster : instance of lib.common.k8_cluster
    release-name: release-name used for helm-chart
    namespace : namespace in which helm-chart is installed

    Returns:
    bool : True if chart is deployed and healthy else False
    """
    """
    Sample output
    vm@master-node:~/sandbox$ helm list -n kube-amd-gpu -o json | jq .
    [
      {
        "name": "gpu-operator",
        "namespace": "kube-amd-gpu",
        "revision": "1",
        "updated": "2024-12-11 10:04:56.122288711 +0000 UTC",
        "status": "failed", or "deployed",
        "chart": "gpu-operator-v1.0.0",
        "app_version": "v1.0.0"
      }
    ]
    """
    ret_code, ret_stdout, ret_stderr = helm_list(k8_cluster, namespace)
    for chart in json.loads(ret_stdout):
        if chart['name'] == release_name and chart['status'] == 'deployed':
            return True
    return False

