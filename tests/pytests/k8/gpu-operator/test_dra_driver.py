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
import pprint
import pytest
import sys
import os
import re
import time
import json
import logging
import lib.common as common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.spec_util as spec_util
import lib.dra_util as dra_util
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_dra_driver")
LogPrettyPrinter = pprint.PrettyPrinter(indent=2)

debug_on_failure = K8Helper.triage


@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    """
    Skip module if DRA is not supported.

    This module only checks Kubernetes version / API availability, but it doesn't
    verify that the GPU Operator version under test supports the spec.draDriver operand.
    For operator versions whose DeviceConfig template doesn't include draDriver
    (pre-v1.5.0 per spec_util.py), generate_k8_deviceconfig_cr() will drop the field
    and the tests will fail waiting for DRA pods/DeviceClass.

    Skips when:
    - Kubernetes version < 1.32 (DRA not supported)
    - DRA API not available
    - GPU Operator version < v1.5.0 (draDriver field not in DeviceConfig template)
    """
    global Logger

    # Get Kubernetes version
    ret_code, version_info = k8_util.k8_get_version()
    if ret_code != 0:
        pytest.skip("Failed to get Kubernetes version")

    # Parse version carefully - version_info['minor'] from Kubernetes VersionApi is commonly
    # a string like "32+" (e.g., on GKE/EKS), so int() can raise ValueError.
    # Parse only the numeric prefix (e.g., re.match(r'\d+', minor)) before converting to int.
    try:
        major_str = str(version_info.get("major", "0"))
        minor_str = str(version_info.get("minor", "0"))

        # Extract numeric prefix from version strings
        major_match = re.match(r"(\d+)", major_str)
        minor_match = re.match(r"(\d+)", minor_str)

        major = int(major_match.group(1)) if major_match else 0
        minor = int(minor_match.group(1)) if minor_match else 0

        Logger.info(
            f"Parsed Kubernetes version: {major}.{minor} (raw: {major_str}.{minor_str})"
        )
    except (ValueError, AttributeError) as e:
        Logger.error(f"Failed to parse Kubernetes version from {version_info}: {e}")
        pytest.skip(f"Failed to parse Kubernetes version: {e}")

    # DRA requires K8s 1.32+
    if major < 1 or (major == 1 and minor < 32):
        pytest.skip(
            f"DRA requires Kubernetes 1.32+, but cluster is running {major}.{minor}"
        )

    # Check DRA API availability
    dra_available, error_msg, api_version = dra_util.check_dra_api_available()
    if not dra_available:
        pytest.skip(f"DRA API not available: {error_msg}")

    Logger.info(f"DRA API version detected: {api_version}")

    # Check GPU Operator version supports draDriver field
    # draDriver was added in v1.5.0 (see spec_util.py device_config_template_v1_5_0)
    gpu_operator_version = getattr(environment, "gpu_operator_version", None)
    if gpu_operator_version:
        # Parse version string (e.g., "v1.5.0" -> (1, 5, 0))
        from packaging import version

        try:
            # Remove 'v' prefix if present
            version_str = gpu_operator_version.lstrip("v")
            parsed_version = version.parse(version_str)
            min_version = version.parse("1.5.0")

            if parsed_version < min_version:
                pytest.skip(
                    f"DRA driver operand requires GPU Operator v1.5.0+, "
                    f"but testing with {gpu_operator_version}. "
                    f"DeviceConfig template for this version does not include spec.draDriver field."
                )
            Logger.info(
                f"GPU Operator version {gpu_operator_version} supports DRA driver operand"
            )
        except Exception as e:
            Logger.warning(
                f"Failed to parse GPU Operator version '{gpu_operator_version}': {e}. "
                f"Proceeding with test (may fail if version < v1.5.0)"
            )
    else:
        Logger.warning(
            "GPU Operator version not found in environment. "
            "Proceeding with test (may fail if version < v1.5.0)"
        )

    return


@pytest.fixture(scope="session")
def dra_api_version(environment):
    """Detect and cache DRA API version"""
    global Logger

    # Check if already cached in environment
    if hasattr(environment, "dra_api_version"):
        Logger.debug(f"Using cached DRA API version: {environment.dra_api_version}")
        return environment.dra_api_version

    # Detect DRA API version
    dra_available, error_msg, api_version = dra_util.check_dra_api_available()
    if not dra_available:
        pytest.fail(f"DRA API not available: {error_msg}")

    # Cache in environment for reuse
    setattr(environment, "dra_api_version", api_version)
    Logger.info(f"DRA API version validated and cached: {api_version}")

    return api_version


@pytest.fixture(scope="module")
def deviceconfig_install(
    gpu_cluster, images, gpu_operator_install, environment, dra_api_version
):
    """
    Install DeviceConfig with DRA driver enabled.
    This fixture creates a DeviceConfig that enables DRA driver and disables device plugin.
    """
    global Logger

    # cleanup - remove any existing deviceconfigs
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(
            environment.gpu_operator_namespace, devcfg_name
        )
        if ret_code != 0:
            Logger.error(
                f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}"
            )
    time.sleep(10)

    class DeviceConfigCRInfo(object):
        pass

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(
        environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster"
    )
    debug_on_failure(
        environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster"
    )

    # Configure DeviceConfig with DRA driver enabled and device plugin disabled
    # Note: DRA driver requires GPU driver to be installed for GPU discovery
    test_config = {
        "metadata.namespace": environment.gpu_operator_namespace,
        "driver.enable": True,
        "devicePlugin.enableDevicePlugin": False,  # Disable device plugin (mutually exclusive with DRA)
        "draDriver.enable": True,  # Enable DRA driver
        "metricsExporter.enable": False,
        "testRunner.enable": False,
    }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(
        test_config, gpu_nodes, "dra_driver", environment.amdgpu_driver_spec
    )
    devicecfg_list = []

    for spec_name, tcfg in test_cfg_map.items():
        cr_spec = spec_util.generate_k8_deviceconfig_cr(
            environment.gpu_operator_version, tcfg
        )
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)
        debug_on_failure(
            environment,
            (ret_code == 0),
            f"Failed to create deviceconfig, stderr: {ret_stderr}",
        )
        devicecfg_list.append(tcfg["metadata.name"])

    # Check for corresponding deviceconfig created
    K8Helper.check_deviceconfig_status(environment, devicecfg_list)

    # Wait for driver installation (DRA driver always requires out-of-tree GPU driver)
    Logger.info("Waiting for GPU driver installation via KMM (required for DRA driver)")
    driver_deployment = environment.amdgpu_driver_spec.get(
        "driver-deployment", "deviceconfig"
    )

    if driver_deployment == "inbox":
        Logger.info("Using inbox amdgpu driver - skipping KMM verification")
    else:
        # Wait for KMM worker completion for each deviceconfig
        for devcfg in devicecfg_list:
            try:
                K8Helper.wait_kmm_worker_completion(environment, devcfg)
            except Exception as e:
                # If KMM label check fails, verify driver is actually loaded as fallback
                Logger.warning(f"KMM worker completion check failed: {e}")
                Logger.info(
                    "Verifying if amdgpu driver is loaded on GPU nodes as fallback check..."
                )

                ret_code, gpu_nodes_list = k8_util.k8_get_gpu_nodes()
                if ret_code == 0 and len(gpu_nodes_list) > 0:
                    driver_loaded_count = 0
                    for node in gpu_nodes_list:
                        # Extract node name from node object (dict)
                        node_name = node.get("metadata", {}).get("name")
                        if not node_name:
                            Logger.warning(
                                f"Failed to get node name from node object: {node}"
                            )
                            continue

                        # Check if amdgpu module is loaded
                        # Use shell wrapper to execute command with pipes/redirects
                        ret_code, output = k8_util.run_command_on_node(
                            gpu_cluster,
                            node_name,
                            ["bash", "-c", "lsmod | grep amdgpu || echo 'not loaded'"],
                        )
                        if (
                            ret_code == 0
                            and output.strip()
                            and "amdgpu" in output
                            and "not loaded" not in output
                        ):
                            driver_loaded_count += 1
                            Logger.info(f"amdgpu driver is loaded on node {node_name}")
                        else:
                            Logger.error(
                                f"amdgpu driver NOT loaded on node {node_name}"
                            )

                    if driver_loaded_count == len(gpu_nodes_list):
                        Logger.info(
                            f"amdgpu driver verified loaded on all {driver_loaded_count} GPU nodes - "
                            f"continuing despite missing kmm.ready labels"
                        )
                    else:
                        debug_on_failure(
                            environment,
                            False,
                            f"GPU driver not loaded on all nodes ({driver_loaded_count}/{len(gpu_nodes_list)}). "
                            f"Original error: {e}",
                        )
                else:
                    debug_on_failure(
                        environment, False, f"Failed to verify driver status: {e}"
                    )

    K8Helper.update_node_driver_version(gpu_cluster, environment)

    devcfg_info = DeviceConfigCRInfo()
    setattr(devcfg_info, "test_cfg_map", test_cfg_map)
    setattr(devcfg_info, "devicecfg_list", devicecfg_list)

    # Wait for DRA driver pods to be ready
    devicecfg_pods = [
        common.PodInfo("dra-driver", len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(
        environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20
    )
    debug_on_failure(
        environment,
        (not failed_pods),
        f"One or more pods are not ready - {failed_pods}",
    )

    yield devcfg_info

    # Cleanup
    device_cfg_info = k8_util.k8_get_deviceconfigs_info(
        environment.gpu_operator_namespace, None
    )
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(
            environment.gpu_operator_namespace, devcfg_name
        )

    # Clean up ResourceClaims if any
    dra_util.cleanup_resource_claims(environment.gpu_operator_namespace)
    return


@pytest.mark.level1
def test_deviceconfig_dra_driver_deploy(
    deviceconfig_install, gpu_cluster, environment, dra_api_version
):
    """
    Test DRA driver operand deployment.
    Verifies that DRA driver pods are running and DeviceClass/ResourceSlices are created.
    """
    global Logger

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(
        environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster"
    )
    debug_on_failure(
        environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster"
    )

    # Watch for DRA driver pod creation
    devicecfg_pods = [
        common.PodInfo("dra-driver", len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(
        environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20
    )
    debug_on_failure(
        environment,
        (not failed_pods),
        f"One or more DRA driver pods are not ready - {failed_pods}",
    )

    Logger.info("DRA driver pods are running successfully")

    # Verify DeviceClass exists
    ret_code, device_classes, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io", version=dra_api_version, plural="deviceclasses"
    )
    debug_on_failure(
        environment, (ret_code == 0), f"Failed to get DeviceClasses: {err}"
    )

    # Check for gpu.amd.com DeviceClass
    device_class_names = (
        [dc["metadata"]["name"] for dc in device_classes] if device_classes else []
    )
    debug_on_failure(
        environment,
        ("gpu.amd.com" in device_class_names),
        f"DeviceClass 'gpu.amd.com' not found. Available DeviceClasses: {device_class_names}",
    )

    Logger.info("DeviceClass 'gpu.amd.com' exists")

    # Verify ResourceSlices are published
    ret_code, resource_slices, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io", version=dra_api_version, plural="resourceslices"
    )
    debug_on_failure(
        environment, (ret_code == 0), f"Failed to get ResourceSlices: {err}"
    )

    # Filter ResourceSlices for gpu.amd.com driver
    gpu_resource_slices = (
        [
            rs
            for rs in resource_slices
            if rs.get("spec", {}).get("driver") == "gpu.amd.com"
        ]
        if resource_slices
        else []
    )
    debug_on_failure(
        environment,
        (len(gpu_resource_slices) > 0),
        f"No ResourceSlices found for driver 'gpu.amd.com'. Total ResourceSlices: {len(resource_slices)}",
    )

    Logger.info(
        f"Found {len(gpu_resource_slices)} ResourceSlices published by DRA driver"
    )

    # Log ResourceSlice details for debugging
    for rs in gpu_resource_slices:
        rs_name = rs["metadata"]["name"]
        node_name = rs.get("spec", {}).get("nodeName", "N/A")
        pool = rs.get("spec", {}).get("pool", {})
        Logger.info(
            f"ResourceSlice: {rs_name}, Node: {node_name}, Pool: {pool.get('name', 'N/A')}"
        )


@pytest.mark.level1
def test_deviceconfig_dra_driver_disable(
    deviceconfig_install, environment, dra_api_version
):
    """
    Test disabling and re-enabling DRA driver operand.
    Verifies that DRA driver pods are terminated when disabled and restarted when enabled.
    """
    global Logger

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(
        environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster"
    )
    debug_on_failure(
        environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster"
    )

    # Disable DRA driver
    Logger.info("Disabling DRA driver via DeviceConfig")
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg["draDriver.enable"] = False
        cr_spec = spec_util.generate_k8_deviceconfig_cr(
            environment.gpu_operator_version, tcfg
        )
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(
            environment,
            (ret_code == 0),
            "Failed to modify deviceconfig CR to disable DRA driver",
        )

    # Verify DRA driver pods are terminated
    dra_pods = [
        common.PodInfo("dra-driver", 1, 1),
    ]
    running_pods = k8_util.k8_check_pod_terminated(
        environment.gpu_operator_namespace, dra_pods
    )
    debug_on_failure(
        environment,
        not running_pods,
        f"DRA driver pods are still running after disabling - {running_pods}",
    )

    Logger.info("DRA driver pods terminated successfully after disabling")

    # Verify ResourceSlices are deleted after disabling DRA driver
    # ResourceSlices may take some time to be cleaned up, so retry with timeout
    Logger.info("Verifying ResourceSlices are cleaned up after disabling DRA driver")
    max_retries = 12  # 12 retries * 5 seconds = 60 seconds max wait
    retry_interval = 5
    resource_slices_deleted = False

    for attempt in range(max_retries):
        ret_code, resource_slices, err = k8_util.k8_get_custom_resource_objects(
            group="resource.k8s.io", version=dra_api_version, plural="resourceslices"
        )

        if ret_code != 0:
            Logger.warning(
                f"Failed to get ResourceSlices (attempt {attempt + 1}/{max_retries}): {err}"
            )
            time.sleep(retry_interval)
            continue

        # Filter for AMD GPU ResourceSlices
        gpu_resource_slices = (
            [
                rs
                for rs in resource_slices
                if rs.get("spec", {}).get("driver") == "gpu.amd.com"
            ]
            if resource_slices
            else []
        )

        if len(gpu_resource_slices) == 0:
            Logger.info(
                f"ResourceSlices successfully deleted (attempt {attempt + 1}/{max_retries})"
            )
            resource_slices_deleted = True
            break
        else:
            Logger.info(
                f"Waiting for ResourceSlices deletion (attempt {attempt + 1}/{max_retries}): "
                f"{len(gpu_resource_slices)} AMD GPU ResourceSlices still exist"
            )
            time.sleep(retry_interval)

    debug_on_failure(
        environment,
        resource_slices_deleted,
        f"ResourceSlices were not deleted after disabling DRA driver within {max_retries * retry_interval} seconds",
    )

    # Re-enable DRA driver
    Logger.info("Re-enabling DRA driver via DeviceConfig")
    for spec_name, tcfg in deviceconfig_install.test_cfg_map.items():
        tcfg["draDriver.enable"] = True
        cr_spec = spec_util.generate_k8_deviceconfig_cr(
            environment.gpu_operator_version, tcfg
        )
        ret_code, ret_stdout, ret_stderr = k8_util.k8_modify_deviceconfig_cr(cr_spec)
        debug_on_failure(
            environment,
            (ret_code == 0),
            "Failed to modify deviceconfig CR to re-enable DRA driver",
        )

    # Verify DRA driver pods are running again
    devicecfg_pods = [
        common.PodInfo("dra-driver", len(gpu_nodes), 1),
    ]
    failed_pods = k8_util.k8_check_pod_running(
        environment.gpu_operator_namespace, devicecfg_pods, sleep_time=20
    )
    debug_on_failure(
        environment,
        (not failed_pods),
        f"One or more DRA driver pods are not ready after re-enabling - {failed_pods}",
    )

    Logger.info("DRA driver pods restarted successfully after re-enabling")

    # Verify ResourceSlices are published again
    ret_code, resource_slices, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io", version=dra_api_version, plural="resourceslices"
    )
    debug_on_failure(
        environment,
        (ret_code == 0),
        f"Failed to get ResourceSlices after re-enabling: {err}",
    )

    gpu_resource_slices = (
        [
            rs
            for rs in resource_slices
            if rs.get("spec", {}).get("driver") == "gpu.amd.com"
        ]
        if resource_slices
        else []
    )
    debug_on_failure(
        environment,
        (len(gpu_resource_slices) > 0),
        f"No ResourceSlices found after re-enabling DRA driver",
    )

    Logger.info(
        f"ResourceSlices restored after re-enabling: {len(gpu_resource_slices)} slices found"
    )


@pytest.mark.level2
def test_deviceconfig_dra_driver_mutual_exclusion(
    gpu_cluster, images, gpu_operator_install, environment, dra_api_version
):
    """
    Test mutual exclusion between DRA driver and device plugin.
    Verifies that enabling both DRA driver and device plugin simultaneously is rejected
    or that only one operand runs at a time.
    """
    global Logger

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    debug_on_failure(
        environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster"
    )
    debug_on_failure(
        environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster"
    )

    # Cleanup - remove any existing deviceconfigs
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(
            environment.gpu_operator_namespace, devcfg_name
        )
        if ret_code != 0:
            Logger.error(
                f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}"
            )
    time.sleep(10)

    # Attempt to create DeviceConfig with both DRA driver and device plugin enabled
    Logger.info(
        "Attempting to create DeviceConfig with both DRA driver and device plugin enabled"
    )

    test_config = {
        "metadata.namespace": environment.gpu_operator_namespace,
        "metadata.name": "test-mutual-exclusion",
        "driver.enable": True,
        "devicePlugin.enableDevicePlugin": True,  # Enable device plugin
        "draDriver.enable": True,  # Enable DRA driver (should conflict)
        "metricsExporter.enable": False,
        "testRunner.enable": False,
    }
    test_config.update(images)

    # Generate DeviceConfig
    test_cfg_map = spec_util.build_deviceconfig_cr_template(
        test_config, gpu_nodes, "mutual_exclusion", environment.amdgpu_driver_spec
    )

    deviceconfig_created = False
    for spec_name, tcfg in test_cfg_map.items():
        cr_spec = spec_util.generate_k8_deviceconfig_cr(
            environment.gpu_operator_version, tcfg
        )

        # Try to create the DeviceConfig
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)

        if ret_code == 0:
            deviceconfig_created = True
            Logger.info(
                "DeviceConfig created successfully despite both operands enabled. "
                "Checking operator behavior..."
            )

            # Wait a bit for operator to reconcile
            time.sleep(30)

            # Check which pods are actually created by name patterns
            # Device plugin pods typically have "device-plugin" in the name
            ret_code, all_pods = k8_util.k8_get_pods(environment.gpu_operator_namespace)
            debug_on_failure(
                environment,
                ret_code == 0,
                "Failed to get pods for mutual exclusion check",
            )

            # Filter pods by name patterns (regardless of phase - we want to ensure NO pods exist)
            # Checking only Running pods would give false pass if pods are Pending/CrashLoopBackOff
            device_plugin_pods = [
                p
                for p in all_pods
                if "device-plugin" in p.get("metadata", {}).get("name", "")
            ]

            dra_driver_pods = [
                p
                for p in all_pods
                if "dra-driver" in p.get("metadata", {}).get("name", "")
            ]

            # Log pod details including phases for debugging
            device_plugin_exists = len(device_plugin_pods) > 0
            dra_driver_exists = len(dra_driver_pods) > 0

            if device_plugin_exists:
                phases = [
                    p.get("status", {}).get("phase", "Unknown")
                    for p in device_plugin_pods
                ]
                Logger.info(
                    f"Device plugin pods found: {len(device_plugin_pods)} "
                    f"(phases: {phases})"
                )
            else:
                Logger.info("Device plugin pods: 0 (none created)")

            if dra_driver_exists:
                phases = [
                    p.get("status", {}).get("phase", "Unknown") for p in dra_driver_pods
                ]
                Logger.info(
                    f"DRA driver pods found: {len(dra_driver_pods)} "
                    f"(phases: {phases})"
                )
            else:
                Logger.info("DRA driver pods: 0 (none created)")

            # Verify mutual exclusion: BOTH should NOT run simultaneously
            # Valid enforcement strategies:
            # 1. API-level rejection: DeviceConfig rejected, NO pods created
            # 2. Runtime enforcement: Only ONE operand runs (policy/precedence)
            # Invalid: Both operands running at the same time
            debug_on_failure(
                environment,
                not (device_plugin_exists and dra_driver_exists),
                f"Mutual exclusion violation: Both device plugin AND DRA driver pods exist simultaneously. "
                f"Device plugin pods: {len(device_plugin_pods)}, DRA driver pods: {len(dra_driver_pods)}. "
                f"Valid enforcement: reject configuration (no pods) OR allow only one operand (precedence).",
            )

            # Log which enforcement strategy was used
            if not device_plugin_exists and not dra_driver_exists:
                Logger.info(
                    "Mutual exclusion enforced via API-level rejection: "
                    "NO pods created for either operand"
                )
            elif device_plugin_exists and not dra_driver_exists:
                Logger.info(
                    "Mutual exclusion enforced via runtime precedence: "
                    f"Device plugin running ({len(device_plugin_pods)} pods), DRA driver suppressed"
                )
            elif dra_driver_exists and not device_plugin_exists:
                Logger.info(
                    "Mutual exclusion enforced via runtime precedence: "
                    f"DRA driver running ({len(dra_driver_pods)} pods), device plugin suppressed"
                )

            # Cleanup
            Logger.info("Cleaning up test DeviceConfig")
            k8_util.k8_delete_deviceconfig_cr(
                environment.gpu_operator_namespace, tcfg["metadata.name"]
            )
            time.sleep(10)

        else:
            Logger.info(
                f"DeviceConfig creation rejected as expected (mutual exclusion enforced at admission): {ret_stderr}"
            )
            # This is actually a valid outcome - admission webhook or validation rejected it
            Logger.info(
                "Mutual exclusion successfully enforced: DeviceConfig with both operands was rejected"
            )

    if not deviceconfig_created:
        Logger.info(
            "Test passed: System correctly rejected DeviceConfig with both DRA driver and device plugin enabled"
        )
    else:
        Logger.info(
            "Test passed: System allowed DeviceConfig creation but enforced mutual exclusion at runtime"
        )


# ============================================================================
# Helper Fixtures for Resource Allocation Tests (migrated from standalone tests)
# ============================================================================


@pytest.fixture(scope="function")
def dra_resource_tracker(environment):
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
            """Cleanup all tracked resources and verify deletion"""
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

                # Wait and verify ResourceClaims are fully deleted
                Logger.info("Waiting for ResourceClaims to be fully deleted...")
                time.sleep(10)

                # Verify all claims are gone
                remaining_claims = dra_util.list_resource_claims(self.namespace)
                remaining_names = [c["metadata"]["name"] for c in remaining_claims]

                for claim_name in self.claims:
                    if claim_name in remaining_names:
                        Logger.warning(f"ResourceClaim {claim_name} still exists after deletion, waiting longer...")
                        time.sleep(10)
                        break

                Logger.info("✓ ResourceClaim cleanup completed and verified")

    tracker = ResourceTracker(environment.gpu_operator_namespace)
    yield tracker

    # Automatic cleanup on test completion (pass or fail)
    tracker.cleanup()


@pytest.fixture(scope="module")
def available_gpu_count(deviceconfig_install, gpu_cluster, environment, dra_api_version):
    """
    Get the actual number of GPUs available on a single node from ResourceSlices.

    With DRA, GPUs are advertised via ResourceSlices, not node capacity.
    This fixture discovers the actual GPU count to use for parameterized testing.

    Depends on deviceconfig_install to ensure DRA driver is deployed and
    ResourceSlices are published before querying.
    """
    global Logger

    # Get ResourceSlices for DRA driver
    ret_code, resource_slices, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io",
        version=dra_api_version,
        plural="resourceslices",
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to get ResourceSlices: {err}")
    K8Helper.triage(environment, resource_slices is not None and len(resource_slices) > 0, "No ResourceSlices found")

    # Find ResourceSlice for gpu.amd.com driver
    gpu_count = 0
    selected_node = None
    failed_nodes = []

    Logger.info(f"Processing {len(resource_slices)} ResourceSlice(s)...")
    for idx, rs in enumerate(resource_slices, 1):
        rs_name = rs.get("metadata", {}).get("name", "unknown")
        driver = rs.get("spec", {}).get("driver", "")
        Logger.info(f"[{idx}/{len(resource_slices)}] Processing ResourceSlice: {rs_name}, driver: {driver}")

        if driver == "gpu.amd.com":
            node_name = rs.get("spec", {}).get("nodeName", "unknown")
            Logger.info(f"Found gpu.amd.com ResourceSlice for node: {node_name}")

            # Get devices safely - handle None case
            devices = rs.get("spec", {}).get("devices")
            Logger.debug(f"  Devices type: {type(devices)}, value: {devices}")

            if devices is None:
                # Check if this is a control-plane node using gpu_cluster node info
                is_control_plane = False
                for node in gpu_cluster.cluster_nodes:
                    if node.node_name == node_name and node.node_type == "master":
                        is_control_plane = True
                        break

                # Track failed node with type information
                node_type = "control-plane" if is_control_plane else "worker"
                failed_nodes.append({
                    "name": node_name,
                    "type": node_type,
                    "resource_slice": rs_name
                })
                continue

            node_gpu_count = len(devices)

            Logger.info(
                f"ResourceSlice for node {node_name}: {node_gpu_count} GPU device(s)"
            )

            # Use the first node with GPUs
            if node_gpu_count > 0 and gpu_count == 0:
                gpu_count = node_gpu_count
                selected_node = node_name

    # Report all failed nodes at the end
    if failed_nodes:
        error_details = []
        for failed in failed_nodes:
            error_details.append(
                f"  - {failed['type']} node '{failed['name']}' (ResourceSlice: {failed['resource_slice']})"
            )
        error_msg = (
            f"Found {len(failed_nodes)} node(s) with devices=None in ResourceSlices for gpu.amd.com driver. "
            f"This indicates a problem with DRA driver or ResourceSlice generation:\n" +
            "\n".join(error_details)
        )
        Logger.error(error_msg)
        K8Helper.triage(environment, False, error_msg)

    Logger.info(
        f"Selected node {selected_node} with {gpu_count} AMD GPU(s) from ResourceSlice"
    )
    K8Helper.triage(environment, gpu_count > 0, "No AMD GPUs found in ResourceSlices")

    return gpu_count


def _verify_pod_gpu_allocation(
    claim_name: str,
    pod_name: str,
    namespace: str,
    device_class_gpu: str,
    expected_gpu_count: int,
    should_allocate: bool,
    environment,
    node_selector=None,
):
    """
    Helper function to verify Pod GPU allocation with ResourceClaim.

    NOTE: This function does NOT cleanup resources (Pod, ResourceClaim).
    The caller is responsible for cleanup.

    Args:
        claim_name: Name for the ResourceClaim to create
        pod_name: Name for the Pod to create
        namespace: Namespace for test resources
        device_class_gpu: DeviceClass name to use
        expected_gpu_count: Number of GPUs to request
        should_allocate: True if allocation should succeed, False if it should wait
        environment: Test environment fixture
        node_selector: Optional node selector dict for Pod (e.g., {"kubernetes.io/hostname": "node-name"})

    Returns:
        Tuple of (success: bool, actual_gpu_count: int)
    """
    global Logger

    node_info = f" on node {node_selector.get('kubernetes.io/hostname')}" if node_selector else ""
    Logger.info(
        f"Testing {pod_name} with {claim_name} requesting {expected_gpu_count} GPU(s){node_info}, should_allocate={should_allocate}"
    )

    try:
        # Create ResourceClaim requesting specified number of GPUs
        ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
            name=claim_name,
            namespace=namespace,
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
            namespace=namespace,
            resource_claim_name=claim_name,
            image="rocm/rocm-terminal:latest",
            command=["sh", "-c", gpu_list_cmd],
            node_selector=node_selector,
        )
        K8Helper.triage(
            environment,
            ret_code == 0,
            f"Failed to create Pod with ResourceClaim: {ret_stderr}",
        )

        # Wait for ResourceClaim allocation
        Logger.info("Waiting for ResourceClaim allocation...")
        allocated = dra_util.wait_for_resource_claim_allocation(
            claim_name, namespace, timeout=60
        )

        if should_allocate:
            # Should successfully allocate
            K8Helper.triage(
                environment,
                allocated,
                f"ResourceClaim should have allocated {expected_gpu_count} GPU(s) but failed",
            )

            # Wait for Pod to reach Running state before getting logs
            Logger.info("Waiting for Pod to reach Running state...")
            pod_running = False
            max_wait = 120  # Wait up to 120 seconds for pod to start
            start_time = time.time()

            while time.time() - start_time < max_wait:
                ret_code, pods = k8_util.k8_get_pods(
                    namespace, pod_name_pattern=pod_name
                )
                if ret_code == 0 and len(pods) > 0:
                    pod_phase = pods[0].get("status", {}).get("phase", "Unknown")
                    Logger.info(f"Pod status: {pod_phase}")

                    if pod_phase == "Running":
                        pod_running = True
                        Logger.info("Pod is running")
                        break
                    elif pod_phase in ["Failed", "Unknown"]:
                        K8Helper.triage(
                            environment,
                            False,
                            f"Pod reached unexpected phase: {pod_phase}"
                        )
                time.sleep(5)

            K8Helper.triage(
                environment,
                pod_running,
                f"Pod did not reach Running state within {max_wait}s"
            )

            # Wait a bit for container to execute and generate logs
            Logger.info("Waiting for container to generate logs...")
            time.sleep(10)

            # Get Pod logs to verify GPU count
            # Retry getting logs in case they're not ready yet
            logs = ""
            max_log_retries = 6
            for attempt in range(max_log_retries):
                ret_code, logs, error = k8_util.k8_get_pod_logs(pod_name, namespace)
                if ret_code == 0 and logs.strip():
                    Logger.info(f"Got pod logs (attempt {attempt + 1}/{max_log_retries})")
                    break
                Logger.info(f"Logs not ready yet (attempt {attempt + 1}/{max_log_retries}), retrying...")
                time.sleep(5)
            else:
                Logger.warning(f"Could not get valid pod logs after {max_log_retries} attempts")

            actual_gpu_count = 0

            if logs.strip():
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
                K8Helper.triage(
                    environment,
                    False,
                    f"Failed to get valid pod logs after waiting for Running state and {max_log_retries} retries"
                )
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
                namespace, pod_name_pattern=pod_name
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


# ============================================================================
# Resource Allocation Tests (migrated from standalone DRA driver tests)
# ============================================================================


@pytest.mark.parametrize("gpu_count_type", ["single", "available", "over_request"])
def test_pod_with_resource_claim(
    deviceconfig_install,
    available_gpu_count,
    gpu_count_type,
    environment,
    dra_resource_tracker,
    dra_api_version,
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
        namespace=environment.gpu_operator_namespace,
        device_class_gpu="gpu.amd.com",
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
    node_gpu_count: int,
    target_node: str,
    namespace: str,
    device_class_gpu: str,
    environment,
):
    """
    Perform one complete CRUD iteration for ResourceClaims and Pods.

    This function encapsulates the full lifecycle:
    - CREATE: Create 3 ResourceClaims and Pods with different GPU counts on a single node
    - READ: Verify all ResourceClaims exist and are listed correctly
    - DELETE: Clean up all resources
    - VERIFY: Ensure resources are fully deleted

    Args:
        iteration: Current iteration number (1-based)
        total_iterations: Total number of iterations
        node_gpu_count: Number of GPUs on the target node
        target_node: Name of the node to pin ResourceClaims to
        namespace: Namespace for DRA driver resources
        device_class_gpu: DeviceClass name to use
        environment: Test environment fixture
    """
    global Logger

    Logger.info("\n" + "=" * 70)
    Logger.info(f"ITERATION {iteration}/{total_iterations}: Starting CRUD cycle on node {target_node}")
    Logger.info("=" * 70)

    # Define claim configurations: claim1=2 GPUs, claim2=2 GPUs, claim3=remaining
    # All pods pinned to target_node to ensure ResourceClaims allocate from same node
    claim_configs = [
        {"name": f"test-gpu-claim-2gpu-a-iter{iteration}", "device_count": 2},
        {"name": f"test-gpu-claim-2gpu-b-iter{iteration}", "device_count": 2},
        {"name": f"test-gpu-claim-remaining-iter{iteration}", "device_count": node_gpu_count - 4},
    ]

    # Create node selector to pin all pods to target_node
    # This ensures DRA allocates GPUs from the target node's ResourceSlice
    node_selector = {
        "kubernetes.io/hostname": target_node
    }

    Logger.info(f"Iteration {iteration}: Creating 3 ResourceClaims on node {target_node}: 2 GPUs, 2 GPUs, {node_gpu_count - 4} GPUs")

    # Track resources for this iteration
    iteration_pods = []
    iteration_claims = []

    # CREATE and VERIFY phase
    for i, config in enumerate(claim_configs):
        claim_name = config["name"]
        device_count = config["device_count"]
        pod_name = f"test-pod-multi-iter{iteration}-{i}"

        Logger.info(f"\nIteration {iteration} - CREATE: Verifying claim {i+1}/3: {claim_name} ({device_count} GPU(s)) on node {target_node}")

        # Track resources for cleanup
        iteration_pods.append(pod_name)
        iteration_claims.append(claim_name)

        # Use helper to create claim, pod, and verify allocation with node selector
        success, actual_count = _verify_pod_gpu_allocation(
            claim_name=claim_name,
            pod_name=pod_name,
            namespace=namespace,
            device_class_gpu=device_class_gpu,
            expected_gpu_count=device_count,
            should_allocate=True,  # All should allocate successfully
            environment=environment,
            node_selector=node_selector,
        )

        K8Helper.triage(
            environment,
            success,
            f"Iteration {iteration}: Failed to verify {claim_name} with {device_count} GPU(s)",
        )

        Logger.info(f"✓ Iteration {iteration} - Claim {i+1}/3 verified: {claim_name} allocated {device_count} GPU(s)")

    # READ phase - Verify we can list all ResourceClaims
    Logger.info(f"\nIteration {iteration} - READ: Verifying ResourceClaim listing...")
    claims = dra_util.list_resource_claims(namespace)
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
        k8_util.k8_delete_pod(pod_name, namespace)

    # Wait for Pods to terminate
    time.sleep(5)

    # Delete ResourceClaims
    for claim_name in iteration_claims:
        Logger.info(f"Iteration {iteration}: Deleting ResourceClaim {claim_name}...")
        ret_code, _, stderr = dra_util.delete_resource_claim(claim_name, namespace)
        K8Helper.triage(
            environment,
            ret_code == 0,
            f"Iteration {iteration}: Failed to delete ResourceClaim {claim_name}: {stderr}",
        )

    # Wait for ResourceClaims to be fully deleted
    Logger.info(f"Iteration {iteration}: Waiting for resources to be fully deleted...")
    time.sleep(5)

    # Verify cleanup - check that our claims are gone
    claims_after_delete = dra_util.list_resource_claims(namespace)
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
    Logger.info(f"  - CREATED 3 ResourceClaims with different GPU counts on node {target_node}")
    Logger.info(f"  - VERIFIED GPU allocations in Pods: 2 + 2 + {node_gpu_count - 4} = {node_gpu_count}")
    Logger.info(f"  - READ: Verified ResourceClaim listing API")
    Logger.info(f"  - DELETED: All resources cleaned up successfully")
    Logger.info("=" * 70)


def test_multiple_resource_claims(
    deviceconfig_install,
    available_gpu_count,
    gpu_cluster,
    environment,
    dra_resource_tracker,
    dra_api_version,
):
    """
    Test multiple ResourceClaims with different GPU counts and Pod verification.
    Repeats the CRUD process 3 times to ensure resource lifecycle works correctly.

    Each iteration creates 3 claims:
    - Claim 1: 2 GPUs
    - Claim 2: 2 GPUs
    - Claim 3: Remaining GPUs (total - 4)

    Requires at least 5 GPUs on a single node to run this test (validates 2+2+remaining allocation).
    """
    global Logger

    # Clean up any leftover test resources from previous runs
    Logger.info("Cleaning up any leftover test resources from previous runs...")
    ret_code, pods = k8_util.k8_get_pods(environment.gpu_operator_namespace, pod_name_pattern="test-pod-multi-iter")
    if ret_code == 0 and len(pods) > 0:
        Logger.info(f"Found {len(pods)} leftover test pod(s), deleting...")
        for pod in pods:
            pod_name = pod.get("metadata", {}).get("name")
            k8_util.k8_delete_pod(pod_name, environment.gpu_operator_namespace)
        time.sleep(5)

    # Clean up leftover ResourceClaims
    claims = dra_util.list_resource_claims(environment.gpu_operator_namespace)
    leftover_claims = [c for c in claims if "test-gpu-claim" in c["metadata"]["name"] and "iter" in c["metadata"]["name"]]
    if leftover_claims:
        Logger.info(f"Found {len(leftover_claims)} leftover ResourceClaim(s), deleting...")
        for claim in leftover_claims:
            claim_name = claim["metadata"]["name"]
            dra_util.delete_resource_claim(claim_name, environment.gpu_operator_namespace)
        time.sleep(5)

    Logger.info("Pre-test cleanup completed")

    # Get total GPU count from ResourceSlices
    ret_code, resource_slices, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io",
        version=dra_api_version,
        plural="resourceslices",
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to get ResourceSlices: {err}")
    K8Helper.triage(environment, resource_slices is not None, f"ResourceSlices is None: {err}")

    Logger.info(f"Finding node with >4 GPUs from {len(resource_slices)} ResourceSlice(s)...")
    selected_node = None
    node_gpu_count = 0
    failed_nodes = []

    for idx, rs in enumerate(resource_slices, 1):
        driver = rs.get("spec", {}).get("driver", "")
        if driver == "gpu.amd.com":
            rs_name = rs.get("metadata", {}).get("name", "unknown")
            node_name = rs.get("spec", {}).get("nodeName", "unknown")
            Logger.info(f"[{idx}/{len(resource_slices)}] Processing ResourceSlice: {rs_name}, node: {node_name}")

            # Get devices safely - handle None case
            devices = rs.get("spec", {}).get("devices")
            if devices is None:
                # Check if this is a control-plane node using gpu_cluster node info
                is_control_plane = False
                for node in gpu_cluster.cluster_nodes:
                    if node.node_name == node_name and node.node_type == "master":
                        is_control_plane = True
                        break

                # Track failed node with type information
                node_type = "control-plane" if is_control_plane else "worker"
                failed_nodes.append({
                    "name": node_name,
                    "type": node_type,
                    "resource_slice": rs_name
                })
                continue

            gpu_count_for_node = len(devices)
            Logger.info(f"  Node {node_name}: {gpu_count_for_node} GPU(s)")

            # Select first node with >4 GPUs if we haven't selected one yet
            if gpu_count_for_node > 4 and selected_node is None:
                selected_node = node_name
                node_gpu_count = gpu_count_for_node
                Logger.info(f"  ✓ Selected node {selected_node} with {node_gpu_count} GPUs for testing")

    # Report all failed nodes at the end
    if failed_nodes:
        error_details = []
        for failed in failed_nodes:
            error_details.append(
                f"  - {failed['type']} node '{failed['name']}' (ResourceSlice: {failed['resource_slice']})"
            )
        error_msg = (
            f"Found {len(failed_nodes)} node(s) with devices=None in ResourceSlices for gpu.amd.com driver. "
            f"This indicates a problem with DRA driver or ResourceSlice generation:\n" +
            "\n".join(error_details)
        )
        Logger.error(error_msg)
        K8Helper.triage(environment, False, error_msg)

    # Validate we found a suitable node
    K8Helper.triage(
        environment,
        selected_node is not None,
        "No node found with >4 GPUs for testing"
    )
    Logger.info(f"Using node {selected_node} with {node_gpu_count} AMD GPU(s) for all test iterations")

    # Repeat CRUD process 3 times
    NUM_ITERATIONS = 3

    for iteration in range(1, NUM_ITERATIONS + 1):
        _perform_crud_iteration(
            iteration=iteration,
            total_iterations=NUM_ITERATIONS,
            node_gpu_count=node_gpu_count,
            target_node=selected_node,
            namespace=environment.gpu_operator_namespace,
            device_class_gpu="gpu.amd.com",
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


def test_non_amd_device_class_should_not_allocate_amd_gpu(
    deviceconfig_install,
    dra_resource_tracker,
    environment,
    dra_api_version,
):
    """
    Negative test: Verify that requesting non-AMD device class (gpu.xxx.com) does NOT
    allocate AMD GPUs. This ensures the DRA driver only responds to gpu.amd.com resources.
    """
    global Logger

    Logger.info("\n" + "=" * 70)
    Logger.info("TEST: Non-AMD DeviceClass should NOT allocate AMD GPUs")
    Logger.info("=" * 70)

    # Create a custom DeviceClass that is NOT gpu.amd.com
    non_amd_device_class = "gpu.xxx.com"

    # Delete old DeviceClass if it exists from previous test run
    Logger.info(f"Cleaning up any existing DeviceClass: {non_amd_device_class}")
    ret_code, stdout, stderr = k8_util.k8_delete_custom_resource(
        group="resource.k8s.io",
        version=dra_api_version,
        plural="deviceclasses",
        namespace="",
        name=non_amd_device_class,
    )
    if ret_code == 0:
        Logger.info(f"Deleted existing DeviceClass: {non_amd_device_class}")
        time.sleep(5)  # Wait for deletion to complete

    Logger.info(f"Creating non-AMD DeviceClass: {non_amd_device_class}")

    # Create DeviceClass using Kubernetes client directly (k8_create_custom_resource has wrong plural)
    from kubernetes import client
    custom_objects_api = client.CustomObjectsApi()

    device_class_body = {
        "apiVersion": f"resource.k8s.io/{dra_api_version}",
        "kind": "DeviceClass",
        "metadata": {"name": non_amd_device_class},
        "spec": {
            "selectors": [
                {
                    "cel": {
                        "expression": f"device.driver == '{non_amd_device_class}'"
                    }
                }
            ]
        },
    }

    try:
        custom_objects_api.create_cluster_custom_object(
            group="resource.k8s.io",
            version=dra_api_version,
            plural="deviceclasses",  # Correct plural, not "deviceclasss"
            body=device_class_body,
        )
        Logger.info(f"Successfully created DeviceClass: {non_amd_device_class}")
    except Exception as e:
        K8Helper.triage(
            environment,
            False,
            f"Failed to create non-AMD DeviceClass: {e}",
        )

    try:
        # Use the helper function to verify non-AMD device class does NOT allocate
        claim_name = "test-non-amd-claim"
        pod_name = "test-non-amd-pod"

        # Register resources for cleanup
        dra_resource_tracker.add_claim(claim_name)
        dra_resource_tracker.add_pod(pod_name)

        # Verify that non-AMD DeviceClass should NOT allocate AMD GPUs
        _verify_pod_gpu_allocation(
            claim_name=claim_name,
            pod_name=pod_name,
            namespace=environment.gpu_operator_namespace,
            device_class_gpu=non_amd_device_class,
            expected_gpu_count=1,
            should_allocate=False,  # Should NOT allocate AMD GPUs
            environment=environment,
        )

        Logger.info(
            f"✓ Verified: Non-AMD DeviceClass {non_amd_device_class} does NOT allocate AMD GPUs"
        )

    finally:
        # Cleanup custom DeviceClass
        Logger.info(f"Cleaning up non-AMD DeviceClass: {non_amd_device_class}")
        ret_code, stdout, stderr = k8_util.k8_delete_custom_resource(
            group="resource.k8s.io",
            version=dra_api_version,
            plural="deviceclasses",
            namespace="",
            name=non_amd_device_class,
        )
        if ret_code != 0:
            Logger.warning(f"Failed to delete DeviceClass: {stderr}")

    Logger.info("=" * 70)
    Logger.info("✓ Negative test completed: Non-AMD device class correctly rejected")
    Logger.info("=" * 70)
