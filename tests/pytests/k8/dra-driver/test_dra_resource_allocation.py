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


@pytest.fixture(scope="module")
def device_class_gpu(dra_driver_install):
    """
    Use the default DeviceClass created by DRA driver Helm chart.

    The DRA driver installation creates a DeviceClass named 'gpu.amd.com' by default.
    Tests should use this existing DeviceClass rather than creating their own.
    """
    global Logger

    device_class_name = "gpu.amd.com"
    Logger.info(f"Using default DeviceClass: {device_class_name}")

    yield device_class_name

    # No cleanup needed - DeviceClass is managed by Helm installation


@pytest.fixture(scope="function")
def dra_resource_tracker(dra_driver_namespace):
    """
    Fixture to track DRA resources (Pods, ResourceClaims) for automatic cleanup.

    Usage:
        def test_example(dra_resource_tracker):
            # Register resources as they're created
            dra_resource_tracker.add_pod("my-pod")
            dra_resource_tracker.add_claim("my-claim")
            # ... test logic ...
            # Cleanup happens automatically via yield

    This ensures cleanup even if test fails.
    """
    global Logger

    class ResourceTracker:
        def __init__(self, namespace):
            self.namespace = namespace
            self.pods = []
            self.claims = []

        def add_pod(self, pod_name: str):
            """Register a Pod for cleanup"""
            self.pods.append(pod_name)

        def add_claim(self, claim_name: str):
            """Register a ResourceClaim for cleanup"""
            self.claims.append(claim_name)

        def cleanup(self):
            """Cleanup all tracked resources"""
            # Cleanup Pods first
            if self.pods:
                Logger.info(f"Cleaning up {len(self.pods)} Pod(s)...")
                for pod_name in self.pods:
                    k8_util.k8_delete_pod(pod_name, self.namespace)
                time.sleep(5)

            # Then cleanup ResourceClaims
            if self.claims:
                Logger.info(f"Cleaning up {len(self.claims)} ResourceClaim(s)...")
                for claim_name in self.claims:
                    ret_code, _, stderr = dra_util.delete_resource_claim(
                        claim_name, self.namespace
                    )
                    if ret_code != 0:
                        Logger.warning(f"Failed to delete ResourceClaim {claim_name}: {stderr}")

    tracker = ResourceTracker(dra_driver_namespace)
    yield tracker

    # Automatic cleanup on test completion (pass or fail)
    tracker.cleanup()


@pytest.fixture(scope="module")
def available_gpu_count(gpu_cluster, environment):
    """
    Get the actual number of GPUs available on a single node from ResourceSlices.

    With DRA, GPUs are advertised via ResourceSlices, not node capacity.
    This fixture discovers the actual GPU count to use for parameterized testing.
    """
    global Logger

    # Get ResourceSlices for DRA driver
    ret_code, resource_slices, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io",
        version=dra_util.get_dra_api_version(),
        plural="resourceslices",
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to get ResourceSlices: {err}")
    K8Helper.triage(environment, len(resource_slices) > 0, "No ResourceSlices found")

    # Find ResourceSlice for gpu.amd.com driver
    gpu_count = 0
    selected_node = None

    for rs in resource_slices:
        driver = rs.get("spec", {}).get("driver", "")
        if driver == "gpu.amd.com":
            node_name = rs.get("spec", {}).get("nodeName", "unknown")
            devices = rs.get("spec", {}).get("devices", [])
            node_gpu_count = len(devices)

            Logger.info(
                f"ResourceSlice for node {node_name}: {node_gpu_count} GPU device(s)"
            )

            # Use the first node with GPUs
            if node_gpu_count > 0 and gpu_count == 0:
                gpu_count = node_gpu_count
                selected_node = node_name

    Logger.info(
        f"Selected node {selected_node} with {gpu_count} AMD GPU(s) from ResourceSlice"
    )
    K8Helper.triage(environment, gpu_count > 0, "No AMD GPUs found in ResourceSlices")

    return gpu_count


def _verify_pod_gpu_allocation(
    claim_name: str,
    pod_name: str,
    dra_driver_namespace: str,
    device_class_gpu: str,
    expected_gpu_count: int,
    should_allocate: bool,
    environment,
):
    """
    Helper function to verify Pod GPU allocation with ResourceClaim.

    NOTE: This function does NOT cleanup resources (Pod, ResourceClaim).
    The caller is responsible for cleanup.

    Args:
        claim_name: Name for the ResourceClaim to create
        pod_name: Name for the Pod to create
        dra_driver_namespace: Namespace for test resources
        device_class_gpu: DeviceClass name to use
        expected_gpu_count: Number of GPUs to request
        should_allocate: True if allocation should succeed, False if it should wait
        environment: Test environment fixture

    Returns:
        Tuple of (success: bool, actual_gpu_count: int)
    """
    global Logger

    Logger.info(
        f"Testing {pod_name} with {claim_name} requesting {expected_gpu_count} GPU(s), should_allocate={should_allocate}"
    )

    try:
        # Create ResourceClaim requesting specified number of GPUs
        ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
            name=claim_name,
            namespace=dra_driver_namespace,
            resource_class=device_class_gpu,
            device_count=expected_gpu_count,
        )
        K8Helper.triage(
            environment, ret_code == 0, f"Failed to create ResourceClaim: {ret_stderr}"
        )

        # Create Pod using the ResourceClaim with command to list GPUs
        gpu_list_cmd = (
            "echo 'Listing allocated GPU devices:' && "
            "ls -la /dev/dri/ && "
            "GPU_COUNT=$(ls -1 /dev/dri/renderD* 2>/dev/null | wc -l) && "
            'echo "Found $GPU_COUNT GPU device(s)" && '
            "sleep 30"
        )

        ret_code, ret_stdout, ret_stderr = dra_util.create_pod_with_resource_claim(
            pod_name=pod_name,
            namespace=dra_driver_namespace,
            resource_claim_name=claim_name,
            image="rocm/rocm-terminal:latest",
            command=["sh", "-c", gpu_list_cmd],
        )
        K8Helper.triage(
            environment,
            ret_code == 0,
            f"Failed to create Pod with ResourceClaim: {ret_stderr}",
        )

        # Wait for ResourceClaim allocation
        Logger.info("Waiting for ResourceClaim allocation...")
        allocated = dra_util.wait_for_resource_claim_allocation(
            claim_name, dra_driver_namespace, timeout=60
        )

        if should_allocate:
            # Should successfully allocate
            K8Helper.triage(
                environment,
                allocated,
                f"ResourceClaim should have allocated {expected_gpu_count} GPU(s) but failed",
            )

            # Wait for Pod to run and get logs
            Logger.info("Waiting for Pod to start...")
            time.sleep(15)

            # Get pod phase
            ret_code, pods = k8_util.k8_get_pods(
                dra_driver_namespace, pod_name_pattern=pod_name
            )
            pod_phase = "Unknown"
            if ret_code == 0 and len(pods) > 0:
                pod_phase = pods[0].get("status", {}).get("phase", "Unknown")
            Logger.info(f"Pod status: {pod_phase}")

            # Get Pod logs to verify GPU count
            ret_code, logs, _ = k8_util.k8_get_pod_logs(pod_name, dra_driver_namespace)
            actual_gpu_count = 0

            if ret_code == 0:
                Logger.info(f"Pod logs:\n{logs}")

                # Parse GPU count from logs
                import re

                for line in logs.split("\n"):
                    if "Found" in line and "GPU device" in line:
                        match = re.search(r"Found (\d+) GPU device", line)
                        if match:
                            actual_gpu_count = int(match.group(1))
                            break

                Logger.info(
                    f"Expected: {expected_gpu_count} GPU(s), Actual: {actual_gpu_count} GPU(s)"
                )
                K8Helper.triage(
                    environment,
                    actual_gpu_count == expected_gpu_count,
                    f"GPU count mismatch: expected {expected_gpu_count}, found {actual_gpu_count}",
                )
                Logger.info(f"✓ GPU allocation verified: {actual_gpu_count} GPU(s)")
                return True, actual_gpu_count
            else:
                Logger.warn(f"Could not get Pod logs: {logs}")
                return False, 0

        else:
            # Should NOT allocate (insufficient GPUs)
            K8Helper.triage(
                environment,
                not allocated,
                f"ResourceClaim should NOT allocate (requested {expected_gpu_count} GPUs but not enough available)",
            )

            # Verify Pod is in Pending state
            time.sleep(10)
            ret_code, pods = k8_util.k8_get_pods(
                dra_driver_namespace, pod_name_pattern=pod_name
            )
            pod_phase = "Unknown"
            if ret_code == 0 and len(pods) > 0:
                pod_phase = pods[0].get("status", {}).get("phase", "Unknown")
            Logger.info(f"Pod status (should be Pending): {pod_phase}")

            K8Helper.triage(
                environment,
                pod_phase in ["Pending", "Waiting"],
                f"Pod should be Pending/Waiting when requesting more GPUs than available, but got: {pod_phase}",
            )

            Logger.info(
                f"✓ Pod correctly waiting for {expected_gpu_count} GPU(s) (not enough available)"
            )
            return True, 0

    except Exception as e:
        Logger.error(f"Exception during GPU allocation verification: {e}")
        raise


@pytest.mark.parametrize("gpu_count_type", ["single", "available", "over_request"])
def test_pod_with_resource_claim(
    dra_driver_install,
    dra_driver_namespace,
    device_class_gpu,
    available_gpu_count,
    gpu_count_type,
    environment,
    dra_resource_tracker,
):
    """
    Parameterized test for Pod GPU allocation with different GPU counts.

    Tests up to three scenarios:
    1. single: Request 1 GPU (should allocate successfully)
    2. available: Request all available GPUs on a node (should allocate successfully)
       - Skipped if available_gpu_count == 1 (would be duplicate of single)
    3. over_request: Request more GPUs than available (should wait/not allocate)
    """
    global Logger

    # Skip "available" scenario if it's the same as "single"
    if gpu_count_type == "available" and available_gpu_count == 1:
        pytest.skip(
            f"Skipping 'available' scenario: available_gpu_count ({available_gpu_count}) equals 'single' scenario"
        )

    # Determine expected GPU count and allocation behavior based on test type
    if gpu_count_type == "single":
        expected_gpu_count = 1
        should_allocate = True
        test_desc = "single GPU allocation"
    elif gpu_count_type == "available":
        expected_gpu_count = available_gpu_count
        should_allocate = True
        test_desc = f"all available GPUs ({available_gpu_count}) allocation"
    else:  # over_request
        expected_gpu_count = available_gpu_count + 1
        should_allocate = False
        test_desc = (
            f"over-request ({expected_gpu_count} > {available_gpu_count}) - should wait"
        )

    Logger.info("=" * 70)
    Logger.info(f"Test scenario: {test_desc}")
    Logger.info("=" * 70)

    claim_name = f"test-gpu-claim-{expected_gpu_count}gpu"
    pod_name = f"test-gpu-pod-{expected_gpu_count}gpu"

    # Register resources for automatic cleanup
    dra_resource_tracker.add_pod(pod_name)
    dra_resource_tracker.add_claim(claim_name)

    # Run the verification
    success, actual_count = _verify_pod_gpu_allocation(
        claim_name=claim_name,
        pod_name=pod_name,
        dra_driver_namespace=dra_driver_namespace,
        device_class_gpu=device_class_gpu,
        expected_gpu_count=expected_gpu_count,
        should_allocate=should_allocate,
        environment=environment,
    )

    K8Helper.triage(environment, success, f"Test failed for scenario: {test_desc}")
    Logger.info(f"✓ Test passed: {test_desc}")

    # Cleanup handled automatically by fixture


def _perform_crud_iteration(
    iteration: int,
    total_iterations: int,
    total_gpus: int,
    dra_driver_namespace: str,
    device_class_gpu: str,
    environment,
):
    """
    Perform one complete CRUD iteration for ResourceClaims and Pods.

    This function encapsulates the full lifecycle:
    - CREATE: Create 3 ResourceClaims and Pods with different GPU counts
    - READ: Verify all ResourceClaims exist and are listed correctly
    - DELETE: Clean up all resources
    - VERIFY: Ensure resources are fully deleted

    Args:
        iteration: Current iteration number (1-based)
        total_iterations: Total number of iterations
        total_gpus: Total number of GPUs available in cluster
        dra_driver_namespace: Namespace for DRA driver resources
        device_class_gpu: DeviceClass name to use
        environment: Test environment fixture
    """
    global Logger

    Logger.info("\n" + "=" * 70)
    Logger.info(f"ITERATION {iteration}/{total_iterations}: Starting CRUD cycle")
    Logger.info("=" * 70)

    # Define claim configurations: claim1=2 GPUs, claim2=2 GPUs, claim3=remaining
    claim_configs = [
        {"name": f"test-gpu-claim-2gpu-a-iter{iteration}", "device_count": 2},
        {"name": f"test-gpu-claim-2gpu-b-iter{iteration}", "device_count": 2},
        {"name": f"test-gpu-claim-remaining-iter{iteration}", "device_count": total_gpus - 4},
    ]

    Logger.info(f"Iteration {iteration}: Creating 3 ResourceClaims: 2 GPUs, 2 GPUs, {total_gpus - 4} GPUs")

    # Track resources for this iteration
    iteration_pods = []
    iteration_claims = []

    # CREATE and VERIFY phase
    for i, config in enumerate(claim_configs):
        claim_name = config["name"]
        device_count = config["device_count"]
        pod_name = f"test-pod-multi-iter{iteration}-{i}"

        Logger.info(f"\nIteration {iteration} - CREATE: Verifying claim {i+1}/3: {claim_name} ({device_count} GPU(s))")

        # Track resources for cleanup
        iteration_pods.append(pod_name)
        iteration_claims.append(claim_name)

        # Use helper to create claim, pod, and verify allocation
        success, actual_count = _verify_pod_gpu_allocation(
            claim_name=claim_name,
            pod_name=pod_name,
            dra_driver_namespace=dra_driver_namespace,
            device_class_gpu=device_class_gpu,
            expected_gpu_count=device_count,
            should_allocate=True,  # All should allocate successfully
            environment=environment,
        )

        K8Helper.triage(
            environment,
            success,
            f"Iteration {iteration}: Failed to verify {claim_name} with {device_count} GPU(s)",
        )

        Logger.info(f"✓ Iteration {iteration} - Claim {i+1}/3 verified: {claim_name} allocated {device_count} GPU(s)")

    # READ phase - Verify we can list all ResourceClaims
    Logger.info(f"\nIteration {iteration} - READ: Verifying ResourceClaim listing...")
    claims = dra_util.list_resource_claims(dra_driver_namespace)
    claim_names_in_list = [c["metadata"]["name"] for c in claims]

    Logger.info(f"Iteration {iteration}: Found {len(claims)} ResourceClaim(s) in namespace: {claim_names_in_list}")

    # Verify the count matches expected
    expected_claim_count = len(claim_configs)  # Should be 3
    K8Helper.triage(
        environment,
        len(claims) >= expected_claim_count,
        f"Iteration {iteration}: Expected at least {expected_claim_count} ResourceClaims, but found {len(claims)}",
    )
    Logger.info(f"✓ Iteration {iteration} - ResourceClaim count verified: {len(claims)} >= {expected_claim_count}")

    # Verify all our claims are in the list
    for config in claim_configs:
        claim_name = config["name"]
        K8Helper.triage(
            environment,
            claim_name in claim_names_in_list,
            f"Iteration {iteration}: ResourceClaim {claim_name} not found in list_resource_claims output",
        )

    Logger.info(f"✓ Iteration {iteration} - All ResourceClaims found in list")

    # DELETE phase - Cleanup resources before next iteration
    Logger.info(f"\nIteration {iteration} - DELETE: Cleaning up resources...")

    # Delete Pods first
    for pod_name in iteration_pods:
        Logger.info(f"Iteration {iteration}: Deleting Pod {pod_name}...")
        k8_util.k8_delete_pod(pod_name, dra_driver_namespace)

    # Wait for Pods to terminate
    time.sleep(5)

    # Delete ResourceClaims
    for claim_name in iteration_claims:
        Logger.info(f"Iteration {iteration}: Deleting ResourceClaim {claim_name}...")
        ret_code, _, stderr = dra_util.delete_resource_claim(claim_name, dra_driver_namespace)
        K8Helper.triage(
            environment,
            ret_code == 0,
            f"Iteration {iteration}: Failed to delete ResourceClaim {claim_name}: {stderr}",
        )

    # Wait for ResourceClaims to be fully deleted
    Logger.info(f"Iteration {iteration}: Waiting for resources to be fully deleted...")
    time.sleep(5)

    # Verify cleanup - check that our claims are gone
    claims_after_delete = dra_util.list_resource_claims(dra_driver_namespace)
    claim_names_after_delete = [c["metadata"]["name"] for c in claims_after_delete]

    for claim_name in iteration_claims:
        K8Helper.triage(
            environment,
            claim_name not in claim_names_after_delete,
            f"Iteration {iteration}: ResourceClaim {claim_name} should have been deleted but still exists",
        )

    Logger.info(f"✓ Iteration {iteration} - DELETE: All resources cleaned up successfully")

    Logger.info("\n" + "=" * 70)
    Logger.info(f"✓ ITERATION {iteration}/{total_iterations} COMPLETED:")
    Logger.info(f"  - CREATED 3 ResourceClaims with different GPU counts")
    Logger.info(f"  - VERIFIED GPU allocations in Pods: 2 + 2 + {total_gpus - 4} = {total_gpus}")
    Logger.info(f"  - READ: Verified ResourceClaim listing API")
    Logger.info(f"  - DELETED: All resources cleaned up successfully")
    Logger.info("=" * 70)


def test_multiple_resource_claims(
    dra_driver_install,
    dra_driver_namespace,
    device_class_gpu,
    available_gpu_count,
    environment,
    dra_resource_tracker,
):
    """
    Test multiple ResourceClaims with different GPU counts and Pod verification.
    Repeats the CRUD process 3 times to ensure resource lifecycle works correctly.

    Each iteration creates 3 claims:
    - Claim 1: 2 GPUs
    - Claim 2: 2 GPUs
    - Claim 3: Remaining GPUs (total - 4)

    Requires at least 6 GPUs to run this test.
    """
    global Logger

    # Get total GPU count from ResourceSlices
    ret_code, resource_slices, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io",
        version=dra_util.get_dra_api_version(),
        plural="resourceslices",
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to get ResourceSlices: {err}")

    total_gpus = sum(
        len(rs.get("spec", {}).get("devices", []))
        for rs in resource_slices
        if rs.get("spec", {}).get("driver") == "gpu.amd.com"
    )

    Logger.info(f"Total AMD GPUs available in cluster: {total_gpus}")

    # Skip test if less than 6 GPUs
    if total_gpus < 6:
        pytest.skip(f"Test requires at least 6 GPUs, but only {total_gpus} available")

    # Repeat CRUD process 3 times
    NUM_ITERATIONS = 3

    for iteration in range(1, NUM_ITERATIONS + 1):
        _perform_crud_iteration(
            iteration=iteration,
            total_iterations=NUM_ITERATIONS,
            total_gpus=total_gpus,
            dra_driver_namespace=dra_driver_namespace,
            device_class_gpu=device_class_gpu,
            environment=environment,
        )

    # Final summary
    Logger.info("\n" + "=" * 70)
    Logger.info(f"✓ ALL {NUM_ITERATIONS} CRUD ITERATIONS COMPLETED SUCCESSFULLY:")
    Logger.info(f"  - Each iteration created, verified, and deleted 3 ResourceClaims")
    Logger.info(f"  - Total ResourceClaims tested: {NUM_ITERATIONS * 3}")
    Logger.info(f"  - All CRUD operations validated across {NUM_ITERATIONS} iterations")
    Logger.info("=" * 70)

    # No additional cleanup needed - all resources already deleted in iterations
