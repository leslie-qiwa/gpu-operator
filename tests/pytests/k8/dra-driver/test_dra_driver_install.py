#!/usr/bin/python3

"""
Copyright (c) Advanced Micro Devices, Inc. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

     http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import pdb
import pytest
import pprint
import sys
import os
import time
import json
import logging
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.dra_util as dra_util
import lib.common as common
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_dra_driver_install")

# DRA driver chart name constant
DRA_DRIVER_CHART_NAME = "k8s-gpu-dra-driver"


def check_amd_gpu_deviceclass_exists(deviceclass_name="gpu.amd.com"):
    """Check if AMD GPU DeviceClass exists

    Args:
        deviceclass_name: Name of the DeviceClass to check (default: gpu.amd.com)

    Returns:
        tuple: (bool, str) - (exists, deviceclass_name or error_message)
    """
    # Use existing k8_util helper
    # kubectl equivalent: kubectl get deviceclasses.resource.k8s.io
    ret_code, device_classes, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io",
        version="v1",
        plural="deviceclasses"
    )

    if ret_code != 0:
        error_msg = f"Error checking DeviceClass: {err}"
        Logger.error(error_msg)
        return False, error_msg

    for dc in device_classes:
        dc_name = dc.get("metadata", {}).get("name", "")
        if dc_name == deviceclass_name:
            return True, dc_name

    return False, f"DeviceClass '{deviceclass_name}' not found"


def check_dra_driver_pods(dra_driver_release_name, dra_driver_namespace, environment):
    """Check that all DRA driver pods are running
    
    Args:
        dra_driver_release_name: Name of the Helm release
        dra_driver_namespace: Namespace where DRA driver is installed
        environment: Test environment
    """
    # kubectl equivalent: kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(
        environment, (ret_code == 0), "Failed to find AMD GPU nodes in the cluster"
    )
    K8Helper.triage(
        environment, len(gpu_nodes) > 0, "No AMD GPU nodes found in cluster"
    )

    # Wait for all DRA driver pods to be created and running
    # DRA driver typically runs as a DaemonSet on GPU nodes
    # Pod name format: {release-name}-{chart-name}-kubeletplugin
    pod_name_prefix = f"{dra_driver_release_name}-{DRA_DRIVER_CHART_NAME}-kubeletplugin"
    exp_pod_list = [
        common.PodInfo(pod_name_prefix, len(gpu_nodes), 1),
    ]

    failed_pods = k8_util.k8_check_pod_running(dra_driver_namespace, exp_pod_list)
    K8Helper.triage(
        environment,
        not failed_pods,
        f"One or more DRA driver pods are not ready - {failed_pods}",
    )
    
    Logger.info(f"All DRA driver pods are running ({len(gpu_nodes)} pod(s))")


def check_dra_driver_resource_class(environment):
    """Check that AMD GPU DeviceClass is created
    
    Args:
        environment: Test environment
    """
    # Wait a bit for DeviceClass to be created
    time.sleep(10)

    # Check if AMD GPU DeviceClass exists
    amd_gpu_class_found, message = check_amd_gpu_deviceclass_exists("gpu.amd.com")
    
    if amd_gpu_class_found:
        Logger.info(f"Found AMD GPU DeviceClass: {message}")
    else:
        Logger.error(f"AMD GPU DeviceClass not found: {message}")

    K8Helper.triage(
        environment,
        amd_gpu_class_found,
        "AMD GPU DeviceClass 'gpu.amd.com' not found",
    )


def check_dra_driver_logs_no_errors(dra_driver_namespace, environment):
    """Check that DRA driver pods have no critical errors in logs
    
    Args:
        dra_driver_namespace: Namespace where DRA driver is installed
        environment: Test environment
    """
    # kubectl equivalent: kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, (ret_code == 0), "Failed to get GPU nodes")

    # Get all DRA driver pods
    # kubectl equivalent: kubectl get pods -n <namespace>
    ret_code, pods = k8_util.k8_get_pods(dra_driver_namespace)
    K8Helper.triage(environment, ret_code == 0, "Failed to get DRA driver pods")

    error_found = False
    for pod in pods:
        pod_name = pod["metadata"]["name"]
        # Check for DRA driver pods (k8s-gpu-dra-driver in the name)
        if DRA_DRIVER_CHART_NAME in pod_name:
            # Check pod logs for errors
            # kubectl equivalent: kubectl logs <pod_name> -n <namespace>
            ret_code, logs, error = k8_util.k8_get_pod_logs(
                pod_name, dra_driver_namespace
            )
            if ret_code == 0:
                # Look for common error patterns
                error_patterns = [
                    "fatal error",
                    "panic:",
                    "failed to start",
                    "connection refused",
                ]

                for pattern in error_patterns:
                    if pattern.lower() in logs.lower():
                        Logger.error(
                            f"Found error pattern '{pattern}' in pod {pod_name} logs"
                        )
                        error_found = True
            else:
                Logger.warn(f"Could not retrieve logs for pod {pod_name}")

    K8Helper.triage(
        environment, not error_found, "Critical errors found in DRA driver pod logs"
    )
    
    Logger.info("DRA driver pods have no critical errors")


@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    """Skip if not testing on K8s"""
    if environment.deployment_mode != "k8":
        pytest.skip(
            f"Skipping DRA driver testcases for {environment.deployment_mode} deployment"
        )
    return


def test_dra_driver_install(
    gpu_cluster,
    dra_driver_release_name,
    dra_driver_namespace,
    dra_driver_install,
    environment,
):
    """Test DRA driver installation via Helm chart"""
    global Logger

    # Verify Helm release is deployed
    ret_code, ret_stdout, ret_stderr = helm_util.helm_list(
        gpu_cluster, dra_driver_namespace
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to list helm-charts")

    dra_driver_running = False
    for chart in json.loads(ret_stdout):
        if chart["name"] == dra_driver_release_name and chart["status"] == "deployed":
            dra_driver_running = True
            Logger.info(f"DRA driver helm chart is deployed: {chart}")

    K8Helper.triage(
        environment,
        dra_driver_running,
        f"helm-chart {dra_driver_release_name} is not in expected state",
    )

    # Check if DRA driver namespace is created
    # kubectl equivalent: kubectl get namespaces
    ret_code, k8_namespaces = k8_util.k8_get_namespaces()
    K8Helper.triage(
        environment, (ret_code == 0), "Error checking k8-namespaces from cluster"
    )
    K8Helper.triage(
        environment,
        (
            len(
                list(
                    filter(
                        lambda x: x["metadata"]["name"] == dra_driver_namespace,
                        k8_namespaces,
                    )
                )
            )
            == 1
        ),
        f"Could not find {dra_driver_namespace} namespace in the cluster",
    )

    # Check DRA driver pods are running
    Logger.info("Checking DRA driver pods...")
    check_dra_driver_pods(dra_driver_release_name, dra_driver_namespace, environment)

    # Check AMD GPU DeviceClass is created
    Logger.info("Checking AMD GPU DeviceClass...")
    check_dra_driver_resource_class(environment)

    # Check DRA driver logs for errors
    Logger.info("Checking DRA driver logs for errors...")
    check_dra_driver_logs_no_errors(dra_driver_namespace, environment)

    Logger.info("DRA driver installation validation complete")


def test_dra_driver_gpu_node_labels(dra_driver_install, environment):
    """Test that GPU nodes have appropriate labels for DRA"""
    global Logger

    # kubectl equivalent: kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get GPU nodes")

    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        labels = node["metadata"].get("labels", {})

        # Check for AMD GPU feature labels
        has_amd_gpu_label = (
            "feature.node.kubernetes.io/amd-gpu" in labels
            or "feature.node.kubernetes.io/amd-vgpu" in labels
        )

        K8Helper.triage(
            environment,
            has_amd_gpu_label,
            f"Node {node_name} missing AMD GPU feature labels",
        )

        Logger.info(f"Node {node_name} has proper AMD GPU labels for DRA")


def test_dra_driver_uninstall(
    gpu_cluster,
    dra_driver_release_name,
    dra_driver_namespace,
    dra_driver_install,
    environment,
):
    """Test DRA driver uninstallation and cleanup
    
    This test performs actual uninstallation and should be run:
    - As part of the full test suite (for complete cleanup)
    - Explicitly when you want to cleanup after manual testing
    """
    global Logger

    # Check if installation exists
    # kubectl equivalent: kubectl get namespaces
    ret_code, k8_namespaces = k8_util.k8_get_namespaces()
    K8Helper.triage(environment, (ret_code == 0), "Error while collecting namespaces")
    namespace_list = list(
        filter(lambda x: x["metadata"]["name"] == dra_driver_namespace, k8_namespaces)
    )
    K8Helper.triage(
        environment,
        (len(namespace_list) == 1),
        f"Missing namespace: {dra_driver_namespace}",
    )

    # Verify helm release exists before uninstalling
    ret_code, ret_stdout, ret_stderr = helm_util.helm_list(
        gpu_cluster, dra_driver_namespace
    )
    K8Helper.triage(environment, (ret_code == 0), "Failed to list helm releases")
    
    release_exists = False
    for chart in json.loads(ret_stdout):
        if chart["name"] == dra_driver_release_name:
            release_exists = True
            Logger.info(f"Found helm release to uninstall: {chart}")
            break
    
    K8Helper.triage(
        environment,
        release_exists,
        f"Helm release {dra_driver_release_name} not found for uninstallation",
    )

    # Uninstall DRA driver
    Logger.info(f"Uninstalling DRA driver: {dra_driver_release_name}")
    ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(
        gpu_cluster, dra_driver_release_name, dra_driver_namespace
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to uninstall DRA driver: {ret_stderr}"
    )

    # Wait for pods to terminate
    Logger.info("Waiting for DRA driver pods to terminate...")
    time.sleep(30)

    # Verify pods are gone
    # kubectl equivalent: kubectl get pods -n <namespace>
    ret_code, pods = k8_util.k8_get_pods(dra_driver_namespace)
    if ret_code == 0:
        dra_pods = [p for p in pods if DRA_DRIVER_CHART_NAME in p["metadata"]["name"]]
        K8Helper.triage(
            environment,
            len(dra_pods) == 0,
            f"DRA driver pods still exist after uninstall: {[p['metadata']['name'] for p in dra_pods]}",
        )

    # Verify AMD GPU DeviceClass is deleted
    amd_gpu_class_exists, message = check_amd_gpu_deviceclass_exists("gpu.amd.com")
    
    if amd_gpu_class_exists:
        Logger.warning(f"AMD GPU DeviceClass 'gpu.amd.com' still exists after uninstall")
    else:
        Logger.info("AMD GPU DeviceClass successfully deleted")

    K8Helper.triage(
        environment,
        not amd_gpu_class_exists,
        "AMD GPU DeviceClass 'gpu.amd.com' was not deleted after uninstall",
    )

    Logger.info("DRA driver successfully uninstalled and cleaned up")
