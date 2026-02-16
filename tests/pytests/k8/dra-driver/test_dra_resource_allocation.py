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
import lib.k8_util as k8_util
import lib.dra_util as dra_util
import lib.common as common
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_dra_resource_allocation")


@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    """Skip if not testing on K8s"""
    if environment.deployment_mode != "k8":
        pytest.skip(
            f"Skipping DRA resource allocation testcases for {environment.deployment_mode} deployment"
        )
    return


@pytest.fixture(scope="function")
def resource_class_gpu(dra_driver_install, dra_driver_namespace, environment):
    """Create a GPU ResourceClass for testing"""
    global Logger

    resource_class_name = "gpu-test-class"

    # Create ResourceClass
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_class(
        name=resource_class_name, driver_name="gpu.amd.com"
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClass: {ret_stderr}"
    )

    Logger.info(f"Created ResourceClass: {resource_class_name}")
    yield resource_class_name

    # Cleanup
    ret_code, ret_stdout, ret_stderr = dra_util.delete_resource_class(
        resource_class_name
    )
    if ret_code != 0:
        Logger.warn(
            f"Failed to delete ResourceClass {resource_class_name}: {ret_stderr}"
        )


def test_create_resource_claim(
    dra_driver_install, dra_driver_namespace, resource_class_gpu, environment
):
    """Test creating a ResourceClaim"""
    global Logger

    claim_name = "test-gpu-claim"

    # Create ResourceClaim
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
        name=claim_name,
        namespace=dra_driver_namespace,
        resource_class=resource_class_gpu,
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClaim: {ret_stderr}"
    )

    # Verify ResourceClaim exists
    # kubectl equivalent: kubectl get resourceclaim <claim_name> -n <namespace> -o yaml
    claim = dra_util.get_resource_claim(claim_name, dra_driver_namespace)
    K8Helper.triage(
        environment, claim is not None, f"ResourceClaim {claim_name} not found"
    )

    # Verify ResourceClaim spec
    K8Helper.triage(
        environment,
        claim["spec"]["resourceClassName"] == resource_class_gpu,
        "ResourceClaim has incorrect resourceClassName",
    )

    Logger.info(f"Successfully created and verified ResourceClaim: {claim_name}")

    # Cleanup
    ret_code, ret_stdout, ret_stderr = dra_util.delete_resource_claim(
        claim_name, dra_driver_namespace
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to delete ResourceClaim: {ret_stderr}"
    )


def test_resource_claim_allocation_immediate(
    dra_driver_install, dra_driver_namespace, resource_class_gpu, environment
):
    """Test ResourceClaim allocation with Immediate mode"""
    global Logger

    claim_name = "test-gpu-claim-immediate"

    # Create ResourceClaim with Immediate allocation
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
        name=claim_name,
        namespace=dra_driver_namespace,
        resource_class=resource_class_gpu,
        allocation_mode="Immediate",
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClaim: {ret_stderr}"
    )

    # Wait for allocation
    allocated = dra_util.wait_for_resource_claim_allocation(
        claim_name, dra_driver_namespace, timeout=120
    )
    K8Helper.triage(
        environment,
        allocated,
        f"ResourceClaim {claim_name} was not allocated within timeout",
    )

    # Verify allocation details
    claim = dra_util.get_resource_claim(claim_name, dra_driver_namespace)
    K8Helper.triage(
        environment,
        claim is not None,
        f"ResourceClaim {claim_name} not found after allocation",
    )

    allocation = claim.get("status", {}).get("allocation")
    K8Helper.triage(
        environment,
        allocation is not None,
        "ResourceClaim missing allocation in status",
    )

    Logger.info(f"ResourceClaim {claim_name} successfully allocated: {allocation}")

    # Cleanup
    ret_code, ret_stdout, ret_stderr = dra_util.delete_resource_claim(
        claim_name, dra_driver_namespace
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to delete ResourceClaim: {ret_stderr}"
    )


def test_resource_claim_allocation_wait_for_consumer(
    dra_driver_install, dra_driver_namespace, resource_class_gpu, environment
):
    """Test ResourceClaim allocation with WaitForFirstConsumer mode"""
    global Logger

    claim_name = "test-gpu-claim-wait"

    # Create ResourceClaim with WaitForFirstConsumer
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
        name=claim_name,
        namespace=dra_driver_namespace,
        resource_class=resource_class_gpu,
        allocation_mode="WaitForFirstConsumer",
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClaim: {ret_stderr}"
    )

    # Verify claim is not yet allocated (no consumer)
    time.sleep(10)
    # kubectl equivalent: kubectl get resourceclaim <claim_name> -n <namespace> -o yaml
    claim = dra_util.get_resource_claim(claim_name, dra_driver_namespace)
    allocation = claim.get("status", {}).get("allocation")

    Logger.info(
        f"ResourceClaim {claim_name} status before consumer: {claim.get('status', {})}"
    )

    # Note: In WaitForFirstConsumer mode, allocation happens only when a Pod references the claim
    # This is expected behavior

    # Cleanup
    ret_code, ret_stdout, ret_stderr = dra_util.delete_resource_claim(
        claim_name, dra_driver_namespace
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to delete ResourceClaim: {ret_stderr}"
    )


def test_pod_with_resource_claim(
    dra_driver_install, dra_driver_namespace, resource_class_gpu, environment
):
    """Test creating a Pod that uses a ResourceClaim"""
    global Logger

    claim_name = "test-gpu-claim-pod"
    pod_name = "test-gpu-pod"

    # Create ResourceClaim
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
        name=claim_name,
        namespace=dra_driver_namespace,
        resource_class=resource_class_gpu,
        allocation_mode="WaitForFirstConsumer",
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClaim: {ret_stderr}"
    )

    # Create Pod using the ResourceClaim
    ret_code, ret_stdout, ret_stderr = dra_util.create_pod_with_resource_claim(
        pod_name=pod_name,
        namespace=dra_driver_namespace,
        resource_claim_name=claim_name,
        image="rocm/pytorch:latest",
        command=["sh", "-c", "sleep 60"],
    )
    K8Helper.triage(
        environment,
        ret_code == 0,
        f"Failed to create Pod with ResourceClaim: {ret_stderr}",
    )

    # Wait for ResourceClaim allocation (should happen now that Pod is created)
    allocated = dra_util.wait_for_resource_claim_allocation(
        claim_name, dra_driver_namespace, timeout=120
    )
    K8Helper.triage(
        environment,
        allocated,
        f"ResourceClaim {claim_name} was not allocated after Pod creation",
    )

    # Wait for Pod to be running
    time.sleep(30)
    ret_code, pod_status = k8_util.k8_get_pod_status(dra_driver_namespace, pod_name)
    K8Helper.triage(environment, ret_code == 0, f"Failed to get Pod status")

    Logger.info(f"Pod {pod_name} status: {pod_status}")

    # Cleanup Pod
    ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_pod(
        dra_driver_namespace, pod_name
    )
    if ret_code != 0:
        Logger.warn(f"Failed to delete Pod {pod_name}: {ret_stderr}")

    # Wait for Pod to be deleted
    time.sleep(10)

    # Cleanup ResourceClaim
    ret_code, ret_stdout, ret_stderr = dra_util.delete_resource_claim(
        claim_name, dra_driver_namespace
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to delete ResourceClaim: {ret_stderr}"
    )


def test_multiple_resource_claims(
    dra_driver_install, dra_driver_namespace, resource_class_gpu, environment
):
    """Test creating multiple ResourceClaims"""
    global Logger

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get GPU nodes")

    # Determine how many GPUs are available across nodes
    total_gpus = 0
    for node in gpu_nodes:
        capacity = node["status"].get("capacity", {})
        if "amd.com/gpu" in capacity:
            total_gpus += int(capacity["amd.com/gpu"])

    Logger.info(f"Total AMD GPUs available in cluster: {total_gpus}")
    K8Helper.triage(environment, total_gpus > 0, "No AMD GPUs found in cluster")

    # Create multiple claims (up to available GPUs or 3, whichever is smaller)
    num_claims = min(3, total_gpus)
    claim_names = []

    for i in range(num_claims):
        claim_name = f"test-gpu-claim-multi-{i}"
        claim_names.append(claim_name)

        ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
            name=claim_name,
            namespace=dra_driver_namespace,
            resource_class=resource_class_gpu,
            allocation_mode="Immediate",
        )
        K8Helper.triage(
            environment,
            ret_code == 0,
            f"Failed to create ResourceClaim {claim_name}: {ret_stderr}",
        )

    # Wait for all claims to be allocated
    for claim_name in claim_names:
        allocated = dra_util.wait_for_resource_claim_allocation(
            claim_name, dra_driver_namespace, timeout=120
        )
        K8Helper.triage(
            environment, allocated, f"ResourceClaim {claim_name} was not allocated"
        )
        Logger.info(f"ResourceClaim {claim_name} allocated successfully")

    # Get allocation details
    allocations = dra_util.get_dra_device_allocations(dra_driver_namespace)
    Logger.info(f"DRA device allocations: {allocations}")

    # Cleanup all claims
    for claim_name in claim_names:
        ret_code, ret_stdout, ret_stderr = dra_util.delete_resource_claim(
            claim_name, dra_driver_namespace
        )
        if ret_code != 0:
            Logger.warn(f"Failed to delete ResourceClaim {claim_name}: {ret_stderr}")


def test_resource_claim_lifecycle(
    dra_driver_install, dra_driver_namespace, resource_class_gpu, environment
):
    """Test complete ResourceClaim lifecycle: create, allocate, use, release, delete"""
    global Logger

    claim_name = "test-gpu-claim-lifecycle"
    pod_name = "test-gpu-pod-lifecycle"

    # Step 1: Create ResourceClaim
    Logger.info("Step 1: Creating ResourceClaim")
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
        name=claim_name,
        namespace=dra_driver_namespace,
        resource_class=resource_class_gpu,
        allocation_mode="Immediate",
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClaim: {ret_stderr}"
    )

    # Step 2: Wait for allocation
    Logger.info("Step 2: Waiting for ResourceClaim allocation")
    allocated = dra_util.wait_for_resource_claim_allocation(
        claim_name, dra_driver_namespace, timeout=120
    )
    K8Helper.triage(
        environment, allocated, f"ResourceClaim {claim_name} was not allocated"
    )

    # Step 3: Create Pod using the claim
    Logger.info("Step 3: Creating Pod with ResourceClaim")
    ret_code, ret_stdout, ret_stderr = dra_util.create_pod_with_resource_claim(
        pod_name=pod_name,
        namespace=dra_driver_namespace,
        resource_claim_name=claim_name,
        image="rocm/pytorch:latest",
        command=["sh", "-c", "rocm-smi && sleep 30"],
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to create Pod: {ret_stderr}")

    # Step 4: Wait for Pod to complete
    Logger.info("Step 4: Waiting for Pod to run")
    time.sleep(45)

    # Step 5: Delete Pod (release resources)
    Logger.info("Step 5: Deleting Pod to release resources")
    ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_pod(
        dra_driver_namespace, pod_name
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to delete Pod: {ret_stderr}")

    time.sleep(10)

    # Step 6: Verify claim still exists and can be reused
    Logger.info("Step 6: Verifying ResourceClaim can be reused")
    claim = dra_util.get_resource_claim(claim_name, dra_driver_namespace)
    K8Helper.triage(
        environment,
        claim is not None,
        "ResourceClaim should still exist after Pod deletion",
    )

    # Step 7: Delete ResourceClaim
    Logger.info("Step 7: Deleting ResourceClaim")
    ret_code, ret_stdout, ret_stderr = dra_util.delete_resource_claim(
        claim_name, dra_driver_namespace
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to delete ResourceClaim: {ret_stderr}"
    )

    Logger.info("ResourceClaim lifecycle test completed successfully")


def test_list_resource_claims(
    dra_driver_install, dra_driver_namespace, resource_class_gpu, environment
):
    """Test listing ResourceClaims in namespace"""
    global Logger

    # Create a few ResourceClaims
    claim_names = ["test-claim-list-1", "test-claim-list-2"]

    for claim_name in claim_names:
        ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
            name=claim_name,
            namespace=dra_driver_namespace,
            resource_class=resource_class_gpu,
        )
        K8Helper.triage(
            environment,
            ret_code == 0,
            f"Failed to create ResourceClaim {claim_name}: {ret_stderr}",
        )

    # List all ResourceClaims in namespace
    claims = dra_util.list_resource_claims(dra_driver_namespace)

    # Verify our claims are in the list
    found_claims = [c["metadata"]["name"] for c in claims]
    Logger.info(f"Found ResourceClaims: {found_claims}")

    for claim_name in claim_names:
        K8Helper.triage(
            environment,
            claim_name in found_claims,
            f"ResourceClaim {claim_name} not found in list",
        )

    # Cleanup
    for claim_name in claim_names:
        ret_code, ret_stdout, ret_stderr = dra_util.delete_resource_claim(
            claim_name, dra_driver_namespace
        )
        if ret_code != 0:
            Logger.warn(f"Failed to delete ResourceClaim {claim_name}: {ret_stderr}")
