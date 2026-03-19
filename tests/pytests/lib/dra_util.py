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

"""
DRA (Dynamic Resource Allocation) utility functions for AMD GPU Kubernetes testing
"""

import os
import pdb
import time
import json
import logging
import pytest
import yaml
from typing import List, Dict, Optional, Tuple
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import lib.k8_util as k8_util

Logger = logging.getLogger("lib.dra_util")

# DRA API Group
DRA_API_GROUP = "resource.k8s.io"

# We only support structured API: v1beta1 (K8s 1.32-1.33) or v1 (K8s 1.34+)
# The older opaque API (v1alpha2, v1alpha3) is not supported
# API version is determined dynamically at runtime via check_dra_api_available()


def check_feature_gate_enabled(
    component_names: List[str],
) -> Tuple[bool, Dict[str, bool], str]:
    """
    Check if DynamicResourceAllocation feature gate is enabled on Kubernetes components.

    This checks the command-line arguments of control plane components to verify
    that --feature-gates=DynamicResourceAllocation=true is set.

    Equivalent kubectl commands:
        kubectl get pod kube-apiserver-<node> -n kube-system -o yaml
        kubectl get pod kube-scheduler-<node> -n kube-system -o yaml
        kubectl get pod kube-controller-manager-<node> -n kube-system -o yaml

    Args:
        component_names: List of component names to check (e.g., ['kube-apiserver', 'kube-scheduler'])

    Returns:
        Tuple of (all_enabled, status_dict, error_message):
            - all_enabled: True if feature gate is enabled on all components
            - status_dict: Dict mapping component name to enabled status
            - error_message: Error message if any component is missing the feature gate
    """
    global Logger

    status = {}
    errors = []

    try:
        v1 = client.CoreV1Api()

        # Get all pods in kube-system namespace
        pods = v1.list_namespaced_pod(namespace="kube-system")

        for component in component_names:
            found = False
            enabled = False

            # Find pods matching the component name
            for pod in pods.items:
                if pod.metadata.name.startswith(component):
                    found = True

                    # Check command-line arguments
                    if pod.spec.containers:
                        container = pod.spec.containers[0]
                        command_args = container.command or []
                        command_args.extend(container.args or [])

                        # Look for --feature-gates argument
                        for arg in command_args:
                            if arg.startswith("--feature-gates="):
                                feature_gates_str = arg.split("=", 1)[1]
                                # Parse feature gates (format: "Gate1=true,Gate2=false,...")
                                feature_gates = {}
                                for gate in feature_gates_str.split(","):
                                    if "=" in gate:
                                        gate_name, gate_value = gate.split("=", 1)
                                        feature_gates[gate_name.strip()] = (
                                            gate_value.strip().lower() == "true"
                                        )

                                # Check if DynamicResourceAllocation is enabled
                                if feature_gates.get(
                                    "DynamicResourceAllocation", False
                                ):
                                    enabled = True
                                    Logger.info(
                                        f"Feature gate DynamicResourceAllocation is enabled on {component}"
                                    )
                                else:
                                    Logger.warning(
                                        f"Feature gate DynamicResourceAllocation not found or disabled on {component}"
                                    )
                                break
                    break

            if not found:
                Logger.warning(
                    f"Could not find {component} pod in kube-system namespace"
                )
                errors.append(f"{component} pod not found")
            elif not enabled:
                errors.append(
                    f"{component} missing --feature-gates=DynamicResourceAllocation=true"
                )

            status[component] = enabled

        all_enabled = all(status.values())
        error_msg = "; ".join(errors) if errors else ""

        return all_enabled, status, error_msg

    except ApiException as e:
        error_msg = f"Failed to check feature gates: {e}"
        Logger.error(error_msg)
        return False, {}, error_msg
    except Exception as e:
        error_msg = f"Unexpected error checking feature gates: {e}"
        Logger.error(error_msg)
        return False, {}, error_msg


def check_dra_api_available() -> Tuple[bool, str, str]:
    """
    Check if DRA structured API is available in the cluster and return the version.

    We only support the newer "structured API" DRA which went beta in K8s 1.32
    and GA in K8s 1.34. The older "opaque API" is not supported.

    This function queries the Kubernetes API server directly to determine which
    DRA API version is available, preferring v1 (GA) over v1beta1 (beta).

    Equivalent kubectl commands:
        # Check available API versions
        kubectl api-versions | grep resource.k8s.io

        # List API resources for the group
        kubectl api-resources --api-group=resource.k8s.io

        # List DeviceClasses to validate
        kubectl get deviceclasses.resource.k8s.io

    Returns:
        tuple: (bool, str, str) - (success, error_message, api_version)
            - success: True if DRA API is available, False otherwise
            - error_message: Error message if not available, empty string otherwise
            - api_version: DRA API version (v1beta1 or v1), empty string if not available
    """
    global Logger

    try:
        # Query available API groups and versions from the cluster
        # This is more robust than inferring from K8s version
        api_client = client.ApiClient()
        apis_api = client.ApisApi(api_client)
        api_groups = apis_api.get_api_versions()

        # Look for resource.k8s.io group and check available versions
        dra_versions = []
        for group in api_groups.groups:
            if group.name == DRA_API_GROUP:
                dra_versions = [v.version for v in group.versions]
                Logger.info(
                    f"Found DRA API group '{DRA_API_GROUP}' with versions: {dra_versions}"
                )
                break

        if not dra_versions:
            # Get K8s version to provide helpful error message
            ret_code, version_info = k8_util.k8_get_version()
            if ret_code == 0:
                major = version_info.get("major", "?")
                minor = version_info.get("minor", "?")
                error_msg = f"DRA API group '{DRA_API_GROUP}' not found in cluster (K8s {major}.{minor}). "

                # Provide version-specific guidance
                try:
                    if int(str(minor)) >= 32 and int(str(minor)) <= 33:
                        error_msg += "For K8s 1.32-1.33, ensure DynamicResourceAllocation feature gate is enabled on all components (kube-apiserver, kube-controller-manager, kube-scheduler, kubelet) with --feature-gates=DynamicResourceAllocation=true and --runtime-config=resource.k8s.io/v1beta1=true"
                    elif int(str(minor)) < 32:
                        error_msg += "DRA requires Kubernetes 1.32+ (currently using older version)"
                    else:
                        error_msg += "DRA should be available by default in this version. Check cluster configuration."
                except (ValueError, TypeError):
                    pass
            else:
                error_msg = f"DRA API group '{DRA_API_GROUP}' not found in cluster"

            Logger.error(error_msg)
            return False, error_msg, ""

        # Prefer v1 (GA) over v1beta1 (beta)
        # Filter to only structured API versions we support
        if "v1" in dra_versions:
            dra_api_version = "v1"
            Logger.info("Using DRA v1 API (GA, enabled by default in K8s 1.34+)")
        elif "v1beta1" in dra_versions:
            dra_api_version = "v1beta1"
            Logger.warning(
                "Using DRA v1beta1 API (Beta). Note: In K8s 1.32-1.33, DynamicResourceAllocation feature gate must be explicitly enabled on all components."
            )

            # Verify feature gate is actually enabled on control plane components
            components_to_check = [
                "kube-apiserver",
                "kube-scheduler",
                "kube-controller-manager",
            ]
            all_enabled, status, gate_error = check_feature_gate_enabled(
                components_to_check
            )

            if not all_enabled:
                error_msg = (
                    f"DRA v1beta1 API requires DynamicResourceAllocation feature gate enabled. "
                    f"Feature gate check failed: {gate_error}. Component status: {status}. "
                    f"Ensure --feature-gates=DynamicResourceAllocation=true is set."
                )
                Logger.error(error_msg)
                return False, error_msg, ""
            else:
                Logger.info(
                    f"Verified DynamicResourceAllocation feature gate is enabled on components: {list(status.keys())}"
                )
        else:
            # Check if only older opaque API versions are available
            unsupported_msg = f"Only unsupported DRA API versions found: {dra_versions}. Requires v1beta1 (K8s 1.32+) or v1 (K8s 1.34+)"
            Logger.error(unsupported_msg)
            return False, unsupported_msg, ""

        Logger.info(f"Using DRA API version {dra_api_version}")

        # Validate by trying to list DeviceClasses
        # kubectl equivalent: kubectl get deviceclasses.resource.k8s.io
        ret_code, device_classes, err = k8_util.k8_get_custom_resource_objects(
            group=DRA_API_GROUP, version=dra_api_version, plural="deviceclasses"
        )

        if ret_code != 0:
            error_msg = f"Failed to list DeviceClasses with {dra_api_version}: {err}"
            Logger.error(error_msg)
            return False, error_msg, ""

        Logger.info(
            f"DRA API (DeviceClass) is available and validated with version {dra_api_version}"
        )
        return True, "", dra_api_version

    except Exception as e:
        error_msg = f"Failed to query DRA API availability: {e}"
        Logger.error(error_msg)
        return False, error_msg, ""


def get_dra_api_version(environment=None) -> str:
    """
    Determine the DRA structured API version available in the cluster.

    This function checks for a cached version in the environment object first
    (if provided), otherwise detects it by calling check_dra_api_available().

    Args:
        environment: Optional test environment object that may have cached dra_api_version

    Returns: API version string (v1beta1 or v1), or empty string if not available
    """
    # Check if version is cached in environment
    if environment and hasattr(environment, "dra_api_version"):
        return environment.dra_api_version

    # Otherwise detect it
    _, _, api_version = check_dra_api_available()
    return api_version


def create_resource_class(
    name: str, driver_name: str = "gpu.amd.com", parameters: Optional[Dict] = None
) -> Tuple[int, str, str]:
    """
    Create a ResourceClass for DRA

    Equivalent kubectl command:
        kubectl apply -f - <<EOF
        apiVersion: resource.k8s.io/<version>
        kind: ResourceClass
        metadata:
          name: <name>
        driverName: <driver_name>
        EOF

    Args:
        name: Name of the ResourceClass
        driver_name: DRA driver name (default: gpu.amd.com)
        parameters: Optional parameters for the ResourceClass

    Returns:
        Tuple of (return_code, stdout, stderr)
    """
    global Logger

    resource_class = {
        "apiVersion": f"{DRA_API_GROUP}/{get_dra_api_version()}",
        "kind": "ResourceClass",
        "metadata": {"name": name},
        "driverName": driver_name,
    }

    if parameters:
        resource_class["parametersRef"] = parameters

    # Use existing k8_util helper for creating custom resources
    ret_code, stdout, stderr = k8_util.k8_create_custom_resource(resource_class)
    if ret_code == 0:
        Logger.info(f"Created ResourceClass: {name}")
    else:
        Logger.error(f"Failed to create ResourceClass {name}: {stderr}")
    return ret_code, stdout, stderr


def delete_resource_class(name: str) -> Tuple[int, str, str]:
    """
    Delete a ResourceClass

    Equivalent kubectl command:
        kubectl delete resourceclass <name>

    Args:
        name: Name of the ResourceClass to delete

    Returns:
        Tuple of (return_code, stdout, stderr)
    """
    global Logger

    # Use existing k8_util helper for deleting custom resources
    ret_code, stdout, stderr = k8_util.k8_delete_custom_resource(
        group=DRA_API_GROUP,
        version=get_dra_api_version(),
        plural="resourceclasses",
        namespace=None,  # ResourceClass is cluster-scoped
        name=name,
    )
    if ret_code == 0:
        Logger.info(f"Deleted ResourceClass: {name}")
    else:
        Logger.error(f"Failed to delete ResourceClass {name}: {stderr}")
    return ret_code, stdout, stderr


def create_resource_claim(
    name: str,
    namespace: str,
    resource_class: str,
    allocation_mode: str = "WaitForFirstConsumer",
) -> Tuple[int, str, str]:
    """
    Create a ResourceClaim

    Equivalent kubectl command:
        kubectl apply -f - <<EOF
        apiVersion: resource.k8s.io/<version>
        kind: ResourceClaim
        metadata:
          name: <name>
          namespace: <namespace>
        spec:
          resourceClassName: <resource_class>
          allocationMode: <allocation_mode>
        EOF

    Args:
        name: Name of the ResourceClaim
        namespace: Namespace for the ResourceClaim
        resource_class: Name of the ResourceClass to use
        allocation_mode: Allocation mode (WaitForFirstConsumer or Immediate)

    Returns:
        Tuple of (return_code, stdout, stderr)
    """
    global Logger

    resource_claim = {
        "apiVersion": f"{DRA_API_GROUP}/{get_dra_api_version()}",
        "kind": "ResourceClaim",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "resourceClassName": resource_class,
            "allocationMode": allocation_mode,
        },
    }

    # Use existing k8_util helper for creating custom resources
    ret_code, stdout, stderr = k8_util.k8_create_custom_resource(resource_claim)
    if ret_code == 0:
        Logger.info(f"Created ResourceClaim: {name} in namespace {namespace}")
    else:
        Logger.error(f"Failed to create ResourceClaim {name}: {stderr}")
    return ret_code, stdout, stderr


def get_resource_claim(name: str, namespace: str) -> Optional[Dict]:
    """
    Get ResourceClaim details

    Equivalent kubectl command:
        kubectl get resourceclaim <name> -n <namespace> -o yaml

    Args:
        name: Name of the ResourceClaim
        namespace: Namespace of the ResourceClaim

    Returns:
        ResourceClaim object or None
    """
    global Logger

    ret_code, result, err = k8_util.k8_get_namespaced_custom_resource(
        group=DRA_API_GROUP,
        version=get_dra_api_version(),
        namespace=namespace,
        plural="resourceclaims",
        name=name,
    )

    if ret_code == 0:
        return result
    else:
        Logger.error(f"Failed to get ResourceClaim {name}: {err}")
        return None


def delete_resource_claim(name: str, namespace: str) -> Tuple[int, str, str]:
    """
    Delete a ResourceClaim

    Equivalent kubectl command:
        kubectl delete resourceclaim <name> -n <namespace>

    Args:
        name: Name of the ResourceClaim
        namespace: Namespace of the ResourceClaim

    Returns:
        Tuple of (return_code, stdout, stderr)
    """
    global Logger

    # Use existing k8_util helper for deleting custom resources
    ret_code, stdout, stderr = k8_util.k8_delete_custom_resource(
        group=DRA_API_GROUP,
        version=get_dra_api_version(),
        plural="resourceclaims",
        namespace=namespace,
        name=name,
    )
    if ret_code == 0:
        Logger.info(f"Deleted ResourceClaim: {name} from namespace {namespace}")
    else:
        Logger.error(f"Failed to delete ResourceClaim {name}: {stderr}")
    return ret_code, stdout, stderr


def list_resource_claims(namespace: str = None) -> List[Dict]:
    """
    List ResourceClaims in a namespace or cluster-wide

    Equivalent kubectl command:
        kubectl get resourceclaims -n <namespace>          # for specific namespace
        kubectl get resourceclaims --all-namespaces        # for all namespaces

    Args:
        namespace: Namespace to list claims from (None for all namespaces)

    Returns:
        List of ResourceClaim objects
    """
    global Logger

    try:
        if namespace:
            # For namespaced resources, use direct API call
            custom_api = client.CustomObjectsApi()
            result = custom_api.list_namespaced_custom_object(
                group=DRA_API_GROUP,
                version=get_dra_api_version(),
                namespace=namespace,
                plural="resourceclaims",
            )
            return result.get("items", [])
        else:
            # For cluster-wide resources, use k8_util method
            ret_code, items, err = k8_util.k8_get_custom_resource_objects(
                group=DRA_API_GROUP,
                version=get_dra_api_version(),
                plural="resourceclaims",
            )
            if ret_code == 0:
                return items
            else:
                Logger.error(f"Failed to list ResourceClaims: {err}")
                return []
    except ApiException as e:
        Logger.error(f"Failed to list ResourceClaims: {e}")
        return []


def cleanup_resource_claims(namespace: str = None) -> None:
    """
    Clean up all ResourceClaims in a namespace

    Equivalent kubectl command:
        kubectl delete resourceclaims --all -n <namespace>           # for specific namespace
        kubectl delete resourceclaims --all --all-namespaces         # for all namespaces

    Args:
        namespace: Namespace to clean up (None for all namespaces)
    """
    global Logger

    claims = list_resource_claims(namespace)
    for claim in claims:
        claim_name = claim["metadata"]["name"]
        claim_namespace = claim["metadata"]["namespace"]
        delete_resource_claim(claim_name, claim_namespace)
        Logger.info(f"Cleaned up ResourceClaim: {claim_name} in {claim_namespace}")


def wait_for_resource_claim_allocation(
    name: str, namespace: str, timeout: int = 120
) -> bool:
    """
    Wait for a ResourceClaim to be allocated
    
    Equivalent kubectl command:
        kubectl wait --for=jsonpath='{.status.allocation}' \
            resourceclaim/<name> -n <namespace> --timeout=<timeout>s

    Args:
        name: Name of the ResourceClaim
        namespace: Namespace of the ResourceClaim
        timeout: Timeout in seconds

    Returns:
        True if allocated, False otherwise
    """
    global Logger

    start_time = time.time()
    while time.time() - start_time < timeout:
        claim = get_resource_claim(name, namespace)
        if claim and claim.get("status", {}).get("allocation"):
            Logger.info(f"ResourceClaim {name} is allocated")
            return True
        time.sleep(5)

    Logger.error(f"ResourceClaim {name} allocation timed out after {timeout}s")
    return False


def create_pod_with_resource_claim(
    pod_name: str,
    namespace: str,
    resource_claim_name: str,
    image: str = "rocm/pytorch:latest",
    command: Optional[List[str]] = None,
    wait_for_running: bool = False,
) -> Tuple[int, str, str]:
    """
    Create a Pod that uses a ResourceClaim

    Equivalent kubectl command:
        kubectl apply -f - <<EOF
        apiVersion: v1
        kind: Pod
        metadata:
          name: <pod_name>
          namespace: <namespace>
        spec:
          resourceClaims:
          - name: gpu-claim
            source:
              resourceClaimName: <resource_claim_name>
          containers:
          - name: gpu-container
            image: <image>
            command: <command>
            resources:
              claims:
              - name: gpu-claim
        EOF

    Args:
        pod_name: Name of the Pod
        namespace: Namespace for the Pod
        resource_claim_name: Name of the ResourceClaim to use
        image: Container image to use
        command: Command to run in the container
        wait_for_running: Wait for pod to reach Running state

    Returns:
        Tuple of (return_code, stdout, stderr)
    """
    global Logger

    if command is None:
        command = ["sleep", "infinity"]

    pod_spec = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": namespace},
        "spec": {
            "restartPolicy": "Never",
            "resourceClaims": [
                {
                    "name": "gpu-claim",
                    "source": {"resourceClaimName": resource_claim_name},
                }
            ],
            "containers": [
                {
                    "name": "gpu-container",
                    "image": image,
                    "command": command,
                    "resources": {"claims": [{"name": "gpu-claim"}]},
                }
            ],
        },
    }

    try:
        v1 = client.CoreV1Api()
        result = v1.create_namespaced_pod(namespace=namespace, body=pod_spec)
        Logger.info(f"Created Pod {pod_name} with ResourceClaim {resource_claim_name}")

        # Optionally wait for pod to be running using k8_util method
        if wait_for_running:
            ret_code = k8_util.k8_check_pod_running(
                namespace=namespace,
                pod_list=[pod_name],
                sleep_time=10,
                total_attempts=30,
            )
            if ret_code != 0:
                Logger.error(f"Pod {pod_name} failed to reach Running state")
                return ret_code, "", "Pod failed to reach Running state"

        return 0, json.dumps(result.to_dict(), default=str), ""
    except ApiException as e:
        Logger.error(f"Failed to create Pod {pod_name}: {e}")
        return -1, "", str(e)


def generate_dra_driver_values(images: Dict, output_file: str) -> bool:
    """
    Generate Helm values.yaml for DRA driver

    Equivalent Helm command:
        helm install <release-name> <chart> --values <output_file>

    Note: This function generates the values file; no direct kubectl equivalent.

    Args:
        images: Image configuration dictionary
        output_file: Path to output values.yaml file

    Returns:
        True if successful, False otherwise
    """
    global Logger

    values = {}

    # Add image configuration
    # The key structure in images dict is based on the 'key' field in YAML
    # For dra-driver-image with key 'image.repository', it becomes 'image.repository.repository'
    image_repo = (
        images.get("image.repository.repository")
        or images.get("dra-driver-image.repository")
        or images.get("dra-driver.image.repository")
    )
    image_tag = (
        images.get("image.repository.version")
        or images.get("dra-driver-image.version")
        or images.get("dra-driver.image.version", "latest")
    )

    if image_repo:
        values["image"] = {
            "repository": image_repo,
            "tag": image_tag,
            "pullPolicy": "IfNotPresent",
        }

    # Add image pull secret if specified
    image_secret = (
        images.get("image.repository.secret")
        or images.get("dra-driver-image.secret")
        or images.get("dra-driver.image.secret")
    )
    if image_secret:
        values["imagePullSecrets"] = [{"name": image_secret}]

    # Add other DRA driver specific configurations
    values["deviceClass"] = {"name": "gpu.amd.com"}

    # Add node selector to only install kubelet plugin on AMD GPU nodes
    # This prevents the DaemonSet from running on controller/master nodes without GPUs
    values["kubeletPlugin"] = {
        "nodeSelector": {
            "feature.node.kubernetes.io/amd-gpu": "true"
        }
    }

    try:
        # Ensure the directory exists
        output_dir = os.path.dirname(output_file)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        with open(output_file, "w") as f:
            yaml.dump(values, f, default_flow_style=False)
        Logger.info(f"Generated DRA driver values.yaml: {output_file}")
        return True
    except Exception as e:
        Logger.error(f"Failed to generate values.yaml: {e}")
        return False


def get_dra_device_allocations(namespace: str = None) -> Dict[str, List[str]]:
    """
    Get GPU device allocations from DRA ResourceClaims
    
    Equivalent kubectl command:
        kubectl get resourceclaims -n <namespace> \
            -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocation.devices}{"\n"}{end}'

    Args:
        namespace: Namespace to check (None for all namespaces)

    Returns:
        Dictionary mapping claim names to allocated device IDs
    """
    global Logger

    allocations = {}
    claims = list_resource_claims(namespace)

    for claim in claims:
        claim_name = claim["metadata"]["name"]
        if claim.get("status", {}).get("allocation"):
            # Extract device information from allocation
            allocation = claim["status"]["allocation"]
            devices = []
            # Parse allocation details (structure depends on DRA driver implementation)
            if "devices" in allocation:
                devices = allocation["devices"]
            allocations[claim_name] = devices

    return allocations


def verify_dra_driver_crds() -> Tuple[bool, List[str]]:
    """
    Verify that DRA resources are available (either as CRDs or built-in)

    Equivalent kubectl command (K8s 1.26-1.33 with CRDs):
        kubectl get crds | grep resource.k8s.io
        kubectl get crd resourceclaims.resource.k8s.io
        kubectl get crd resourceclasses.resource.k8s.io
        kubectl get crd resourceclaimtemplates.resource.k8s.io

    For K8s 1.34+ (built-in resources):
        kubectl api-resources | grep resource.k8s.io
        kubectl get resourceclaims --all-namespaces
        kubectl get deviceclasses  # Both v1beta1 and v1 use DeviceClass

    Returns:
        Tuple of (success, list of unavailable resources)
    """
    global Logger

    # In K8s 1.34+, these are built-in resources, not CRDs
    # We'll check if the API is available instead
    api_version = get_dra_api_version()

    unavailable = []

    # Check ResourceClaims - Use existing k8_util helper
    ret_code, items, err = k8_util.k8_get_custom_resource_objects(
        group=DRA_API_GROUP, version=api_version, plural="resourceclaims"
    )
    if ret_code == 0:
        Logger.info("ResourceClaim API is available")
    else:
        unavailable.append("resourceclaims.resource.k8s.io")
        Logger.error(f"ResourceClaim API not available: {err}")

    # Check DeviceClasses - Both v1beta1 and v1 use "deviceclasses"
    # (The older opaque API used "resourceclasses" but we don't support that)
    ret_code, items, err = k8_util.k8_get_custom_resource_objects(
        group=DRA_API_GROUP, version=api_version, plural="deviceclasses"
    )
    if ret_code == 0:
        Logger.info("DeviceClass API is available")
    else:
        unavailable.append("deviceclasses.resource.k8s.io")
        Logger.error(f"DeviceClass API not available: {err}")

    return len(unavailable) == 0, unavailable
