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

# DRA API Group and Version
DRA_API_GROUP = "resource.k8s.io"
DRA_API_VERSION = "v1alpha3"  # Will need to adjust based on K8s version


def get_dra_api_version() -> str:
    """
    Determine the DRA API version available in the cluster
    
    Equivalent kubectl command (K8s 1.26-1.33 with CRDs):
        kubectl get crd resourceclaims.resource.k8s.io -o jsonpath='{.spec.versions[?(@.storage==true)].name}'
    
    For K8s 1.34+ (built-in resources, no CRDs):
        kubectl api-resources | grep resourceclaims
        # Or check API version:
        kubectl get --raw /apis/resource.k8s.io | jq -r '.versions[].version'
    
    Returns: API version string (v1alpha3, v1beta1, v1, etc.)
    """
    global Logger
    
    # First try to get version from CRD (K8s < 1.34)
    api = client.ApiextensionsV1Api()
    try:
        crd = api.read_custom_resource_definition("resourceclaims.resource.k8s.io")
        for version in crd.spec.versions:
            if version.served and version.storage:
                Logger.info(f"Using DRA API version from CRD: {version.name}")
                return version.name
    except ApiException as e:
        Logger.debug(f"Failed to get DRA API version from CRD: {e}")
    
    # CRD not found - try built-in API (K8s >= 1.34)
    # Check if ResourceClaim is available as built-in resource
    # Use existing k8_util helper
    ret_code, items, err = k8_util.k8_get_custom_resource_objects(
        group=DRA_API_GROUP,
        version="v1",
        plural="resourceclaims"
    )
    if ret_code == 0:
        Logger.info(f"Using DRA API version (built-in): v1")
        return "v1"
    
    # Fallback to v1alpha3 for older versions
    Logger.warn(f"Could not determine DRA API version, defaulting to v1alpha3")
    return "v1alpha3"


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
        name=name
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
                plural="resourceclaims"
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
                total_attempts=30
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
        kubectl get deviceclasses  # Note: ResourceClass renamed to DeviceClass in v1

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
        group=DRA_API_GROUP,
        version=api_version,
        plural="resourceclaims"
    )
    if ret_code == 0:
        Logger.info("ResourceClaim API is available")
    else:
        unavailable.append("resourceclaims.resource.k8s.io")
        Logger.error(f"ResourceClaim API not available: {err}")

    # Check DeviceClasses (ResourceClass in older versions) - Use existing k8_util helper
    device_class_plural = "deviceclasses" if api_version == "v1" else "resourceclasses"
    ret_code, items, err = k8_util.k8_get_custom_resource_objects(
        group=DRA_API_GROUP,
        version=api_version,
        plural=device_class_plural
    )
    if ret_code == 0:
        Logger.info(f"{device_class_plural.capitalize()} API is available")
    else:
        unavailable.append(f"{device_class_plural}.resource.k8s.io")
        Logger.error(f"{device_class_plural.capitalize()} API not available: {err}")

    return len(unavailable) == 0, unavailable
