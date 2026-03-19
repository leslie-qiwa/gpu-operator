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
import os
import re
import logging
import time
from lib import common
import lib.helm_util as helm_util
import lib.k8_util as k8_util
import lib.dra_util as dra_util
import lib.spec_util as spec_util
from lib.util import K8Helper

Logger = logging.getLogger("k8.dra-driver.conftest")


def pytest_collection_modifyitems(items):
    """
    Reorder DRA driver tests to ensure proper execution order:
    1. Install tests run first (setup)
    2. Other tests run in the middle
    3. Uninstall tests run last (cleanup)
    """
    # Separate tests into categories
    install_tests = []
    uninstall_tests = []
    other_tests = []

    for item in items:
        # Check if this is a DRA driver test (only reorder tests in this directory)
        if "dra-driver" not in item.nodeid:
            other_tests.append(item)
            continue

        # Install test should run first
        if "test_dra_driver_install" in item.nodeid and "uninstall" not in item.nodeid:
            install_tests.append(item)
        # Uninstall test should run last
        elif "test_dra_driver_uninstall" in item.nodeid:
            uninstall_tests.append(item)
        # All other tests in the middle
        else:
            other_tests.append(item)

    # Reorder: install first, then others, then uninstall last
    items[:] = install_tests + other_tests + uninstall_tests

    # Log the reordering for debugging
    if install_tests or uninstall_tests:
        Logger.info("=" * 70)
        Logger.info("DRA driver test execution order:")
        Logger.info("=" * 70)
        if install_tests:
            Logger.info(f"  FIRST: {[item.name for item in install_tests]}")
        if other_tests:
            Logger.info(
                f"  MIDDLE: {len([i for i in other_tests if 'dra-driver' in i.nodeid])} other DRA tests"
            )
        if uninstall_tests:
            Logger.info(f"  LAST: {[item.name for item in uninstall_tests]}")
        Logger.info("=" * 70)


@pytest.fixture(scope="session")
def dra_api_version(environment):
    """
    Detect and validate DRA API version based on Kubernetes version.

    This fixture runs once per test session and enforces version-specific requirements:
    - K8s 1.34+: Requires DRA v1 API (GA, enabled by default)
    - K8s 1.32-1.33: Requires DRA v1beta1 API with DynamicResourceAllocation feature gate enabled
    - K8s < 1.32: Skips tests (DRA not supported)

    Returns:
        str: DRA API version (v1beta1 or v1)
    """
    global Logger

    # Check if already cached in environment
    if hasattr(environment, "dra_api_version"):
        Logger.debug(f"Using cached DRA API version: {environment.dra_api_version}")
        return environment.dra_api_version

    # Get Kubernetes version
    Logger.info("Detecting Kubernetes version and validating DRA requirements")
    ret_code, version_info = k8_util.k8_get_version()
    if ret_code != 0:
        pytest.fail("Failed to get Kubernetes version")

    major = int(version_info.get("major", 0))
    minor = int(version_info.get("minor", 0))
    Logger.info(f"Kubernetes version: {major}.{minor}")

    # Detect DRA API availability and version
    dra_available, error_msg, api_version = dra_util.check_dra_api_available()

    # K8s 1.34+: Must have v1 API (GA)
    if major > 1 or (major == 1 and minor >= 34):
        if not dra_available:
            pytest.fail(
                f"K8s {major}.{minor} requires DRA v1 API, but DRA is not available: {error_msg}"
            )
        if api_version != "v1":
            pytest.fail(
                f"K8s {major}.{minor} requires DRA v1 API (GA), but found {api_version}. "
                f"DRA should be enabled by default in K8s 1.34+. Check cluster configuration."
            )
        Logger.info(f"✓ K8s {major}.{minor} has DRA v1 API (GA) as expected")

    # K8s 1.32-1.33: Must have v1beta1 API with feature gate enabled
    elif major == 1 and minor >= 32:
        if not dra_available:
            pytest.fail(
                f"K8s {major}.{minor} requires DRA v1beta1 API with feature gate enabled, "
                f"but DRA is not available: {error_msg}"
            )
        if api_version != "v1beta1":
            pytest.fail(
                f"K8s {major}.{minor} requires DRA v1beta1 API (beta), but found {api_version}"
            )

        # Verify feature gate is enabled on control plane components
        Logger.info("Verifying DynamicResourceAllocation feature gate is enabled...")
        components = ["kube-apiserver", "kube-scheduler", "kube-controller-manager"]
        all_enabled, status, gate_error = dra_util.check_feature_gate_enabled(
            components
        )

        if not all_enabled:
            pytest.fail(
                f"K8s {major}.{minor} with DRA v1beta1 requires DynamicResourceAllocation feature gate enabled. "
                f"Feature gate check failed: {gate_error}. Component status: {status}. "
                f"Enable with --feature-gates=DynamicResourceAllocation=true on kube-apiserver, "
                f"kube-scheduler, kube-controller-manager, and kubelet. "
                f"Also ensure --runtime-config=resource.k8s.io/v1beta1=true on kube-apiserver."
            )

        Logger.info(
            f"✓ K8s {major}.{minor} has DRA v1beta1 API with feature gate enabled on: {list(status.keys())}"
        )

    # K8s < 1.32: Skip tests (DRA not supported)
    else:
        pytest.skip(
            f"DRA requires Kubernetes 1.32+ (structured API), but cluster is running {major}.{minor}"
        )

    # Cache in environment for reuse
    setattr(environment, "dra_api_version", api_version)
    Logger.info(f"DRA API version validated and cached: {api_version}")

    return api_version


@pytest.fixture(scope="session")
def dra_driver_release_name(environment):
    return "amd-gpu-dra-driver"


@pytest.fixture(scope="session")
def dra_driver_namespace(environment):
    """DRA driver namespace"""
    return os.getenv("DRA_DRIVER_NAMESPACE", "kube-amd-gpu-dra")


@pytest.fixture(scope="session")
def dra_gpu_operator_install(gpu_cluster, images, environment):
    """
    Session-scoped GPU operator installation for DRA tests only.

    This ensures GPU operator is installed once for all DRA test files,
    avoiding reinstallation issues when running multiple test files.

    This is separate from the module-scoped gpu_operator_install to avoid
    breaking other test suites that depend on module-level cleanup.
    """
    global Logger

    # Import here to access the actual fixture implementation
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from k8.conftest import gpu_operator_release_name

    release_name = "gpu-operator"

    Logger.info("=" * 70)
    Logger.info("DRA session-scoped GPU operator installation")
    Logger.info("=" * 70)

    # Cleanup any existing installation
    devcfg_map = k8_util.k8_get_deviceconfigs_info(environment.gpu_operator_namespace)
    for devcfg_name, _ in devcfg_map.items():
        ret_code, ret_stdout, ret_stderr = k8_util.k8_delete_deviceconfig_cr(
            environment.gpu_operator_namespace, devcfg_name
        )
        if ret_code != 0:
            Logger.error(f"Failed to delete deviceconfig name: {devcfg_name}, error : {ret_stderr}")
    time.sleep(10)

    # Check if already installed
    if helm_util.is_helm_chart_deployed(gpu_cluster, release_name, environment.gpu_operator_namespace):
        Logger.info(f"GPU operator '{release_name}' already deployed - reusing existing installation")
        yield
        # Note: No cleanup - let init_dra_testbed handle it
        return

    # Install GPU operator with proper chart resolution
    Logger.info("=" * 70)
    Logger.info("GPU OPERATOR HELM INSTALLATION - Configuration Details")
    Logger.info("=" * 70)

    gpu_operator_chart = images.get("gpu-operator.helm-chart", None)
    gpu_operator_version = images.get("gpu-operator.version", environment.gpu_operator_version)

    Logger.info(f"Chart location: {gpu_operator_chart}")
    Logger.info(f"Chart version: {gpu_operator_version}")
    Logger.info(f"Release name: {release_name}")
    Logger.info(f"Namespace: {environment.gpu_operator_namespace}")

    # Check chart location type
    if gpu_operator_chart:
        if gpu_operator_chart.startswith("repo://"):
            Logger.info("Chart type: Helm repository reference")
            Logger.info(f"  Repo name: {images.get('gpu-operator.repo-name', 'N/A')}")
            Logger.info(f"  Repo URL: {images.get('gpu-operator.repo', 'N/A')}")
        elif gpu_operator_chart.startswith("file://"):
            Logger.info("Chart type: Local file path")
        elif gpu_operator_chart.startswith("http://") or gpu_operator_chart.startswith("https://"):
            Logger.info("Chart type: HTTP(S) URL")
        else:
            Logger.info("Chart type: Direct path")

    K8Helper.triage(
        environment,
        gpu_operator_chart is not None,
        "GPU operator helm chart not found in image manifest",
    )

    # Add Helm repo if needed (for repo:// locations)
    if images.get("gpu-operator.repo", None):
        repo_name = images.get("gpu-operator.repo-name")
        repo_url = images.get("gpu-operator.repo")
        Logger.info(f"Adding Helm repository: {repo_name} -> {repo_url}")
        ret_code, stdout, stderr = helm_util.helm_add_repo(gpu_cluster, repo_name, repo_url)
        if ret_code == 0:
            Logger.info(f"  ✓ Helm repo '{repo_name}' added successfully")
        else:
            Logger.warning(f"  Helm repo add returned code {ret_code}: {stderr}")

    # Generate values.yaml
    values_yaml = os.path.join(environment.logdir, f"dra_gpu_operator_values_{gpu_operator_version}.yaml")
    Logger.info(f"Generating values.yaml: {values_yaml}")
    if spec_util.generate_helmchart_deployment_config(gpu_operator_version, images, values_yaml):
        Logger.info(f"  ✓ Generated values.yaml successfully")
        # Log values content for debugging
        if os.path.exists(values_yaml):
            with open(values_yaml, 'r') as f:
                values_content = f.read()
                Logger.debug(f"Values.yaml content:\n{values_content}")
    else:
        Logger.info("  Using default chart values (no custom values.yaml)")
        values_yaml = None

    # Install options
    options = {
        "crds.defaultCR.install": "false",
    }
    Logger.info(f"Install options: {options}")

    Logger.info("=" * 70)
    Logger.info(f"Executing: helm install {release_name} {gpu_operator_chart}")
    Logger.info("=" * 70)

    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(
        gpu_cluster,
        release_name,
        environment.gpu_operator_namespace,
        gpu_operator_chart,
        gpu_operator_version,
        values_yaml,
        **options
    )

    if ret_code != 0:
        Logger.error(f"Failed to install GPU operator")
        Logger.error(f"Stdout: {ret_stdout}")
        Logger.error(f"Stderr: {ret_stderr}")

    K8Helper.triage(
        environment,
        ret_code == 0,
        f"Failed to install GPU operator: {ret_stderr}",
    )

    # Wait for operator to be ready
    K8Helper.watch_for_daemon_rollout(environment, environment.gpu_operator_namespace, len(gpu_cluster.cluster_nodes))
    time.sleep(30)

    Logger.info("GPU operator installed successfully for DRA test session")
    yield

    # Note: Cleanup handled by init_dra_testbed session fixture
    Logger.info("DRA GPU operator session complete (cleanup via init_dra_testbed)")


@pytest.fixture(scope="module")
def amdgpu_driver_install(gpu_cluster, images, dra_gpu_operator_install, environment):
    """
    Install AMD GPU driver using DeviceConfig CR.

    Module-scoped fixture that depends on session-scoped dra_gpu_operator_install.
    Ensures GPU operator is deployed (once per session) and driver is loaded.
    """
    global Logger

    # dra_gpu_operator_install is session-scoped - GPU operator already installed
    Logger.info("GPU operator is installed (session-scoped dependency satisfied)")

    # cleanup - remove any deviceconfigs
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

    if environment.amdgpu_driver_spec["driver-deployment"] == "inbox":
        Logger.info(f"Using inbox driver - no need to install device-config")
        yield
        return

    # Setup deviceconfig CR to install amdgpu driver
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(
        environment, (ret_code == 0), "Error while getting gpu-nodes from k8-cluster"
    )
    K8Helper.triage(
        environment, (len(gpu_nodes) > 0), "No nodes with AMD/GPU found in the cluster"
    )

    test_config = {
        "metadata.namespace": environment.gpu_operator_namespace,
        "driver.enable": True,
        "devicePlugin.enableNodeLabeller": False,
        "metricsExporter.enable": False,
    }
    test_config.update(images)

    test_cfg_map = spec_util.build_deviceconfig_cr_template(
        test_config, gpu_nodes, "exporter", environment.amdgpu_driver_spec
    )
    devicecfg_list = []
    for spec_name, tcfg in test_cfg_map.items():
        cr_spec = spec_util.generate_k8_deviceconfig_cr(
            environment.gpu_operator_version, tcfg
        )
        ret_code, ret_stdout, ret_stderr = k8_util.k8_create_deviceconfig_cr(cr_spec)
        K8Helper.triage(
            environment,
            (ret_code == 0),
            f"Failed to create deviceconfig, stderr: {ret_stderr}",
        )
        devicecfg_list.append(tcfg["metadata.name"])

    # Check for corresponding deviceconfig created
    K8Helper.check_deviceconfig_status(environment, devicecfg_list)
    for devcfg in devicecfg_list:
        K8Helper.wait_kmm_worker_completion(environment, devcfg)
    K8Helper.update_node_driver_version(gpu_cluster, environment)

    yield

    device_cfg_info = k8_util.k8_get_deviceconfigs_info(
        environment.gpu_operator_namespace, None
    )
    for devcfg_name, _ in device_cfg_info.items():
        k8_util.k8_delete_deviceconfig_cr(
            environment.gpu_operator_namespace, devcfg_name
        )
    return


@pytest.fixture(scope="session", autouse=True)
def init_dra_testbed(
    request,
    gpu_cluster,
    dra_driver_release_name,
    dra_driver_namespace,
    environment,
    dra_api_version,
):
    """Initialize DRA test environment"""
    global Logger

    def _cleanup_steps():
        # cleanup
        K8Helper.delete_debug_pods([dra_driver_namespace, "default"])

        # remove dra-driver helm-chart
        if helm_util.is_helm_chart_deployed(
            gpu_cluster, dra_driver_release_name, dra_driver_namespace
        ):
            Logger.warning(
                f"helm {dra_driver_release_name} is already deployed - cleanup"
            )
            ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(
                gpu_cluster, dra_driver_release_name, dra_driver_namespace
            )
            if ret_code != 0:
                helm_util.helm_cleanup(
                    gpu_cluster, dra_driver_release_name, dra_driver_namespace
                )

        # Clean up any remaining ResourceClaims
        dra_util.cleanup_resource_claims(dra_driver_namespace)

    # Check if user wants to skip cleanup (useful when running individual tests against existing installation)
    skip_cleanup = os.getenv("DRA_SKIP_CLEANUP", "false").lower() in (
        "true",
        "1",
        "yes",
    )

    if skip_cleanup:
        Logger.info(
            "DRA_SKIP_CLEANUP=true - Skipping cleanup, will use existing DRA driver installation"
        )
    else:
        Logger.info("Cleanup before starting DRA test session")
        _cleanup_steps()

    # Init k8 cluster for DRA testing
    k8_util.k8_init_cluster(gpu_cluster, [dra_driver_namespace])

    # DRA API version is already checked and cached by dra_api_version fixture
    Logger.info(f"DRA API version validated: {dra_api_version}")

    yield
    # NOTE: Session teardown cleanup is intentionally NOT done here to allow manual
    # inspection after running individual tests. Cleanup will happen via:
    # 1. Session setup (above) - cleans before each test session starts
    # 2. test_dra_driver_uninstall test - explicit cleanup when that test runs
    Logger.info(
        "DRA test session complete (no auto-cleanup, run test_dra_driver_uninstall to cleanup)"
    )
    return


@pytest.fixture(scope="module")
def dra_driver_install(
    gpu_cluster,
    dra_driver_release_name,
    dra_driver_namespace,
    images,
    environment,
    dra_api_version,
):
    """Install DRA driver using Helm chart"""
    global Logger

    # Use cached DRA API version from session fixture
    Logger.info(f"Using DRA API version: {dra_api_version}")

    # Check for and clean up any existing DRA installations
    Logger.info("Checking for existing DRA driver installations")
    if helm_util.is_helm_chart_deployed(
        gpu_cluster, dra_driver_release_name, dra_driver_namespace
    ):
        Logger.info(
            f"Found existing helm release '{dra_driver_release_name}' in namespace '{dra_driver_namespace}', uninstalling"
        )
        ret_code, ret_stdout, ret_stderr = helm_util.helm_uninstall(
            gpu_cluster, dra_driver_release_name, dra_driver_namespace
        )
        if ret_code != 0:
            Logger.warning(f"Helm uninstall failed, attempting cleanup: {ret_stderr}")
            helm_util.helm_cleanup(
                gpu_cluster, dra_driver_release_name, dra_driver_namespace
            )
        time.sleep(10)  # Wait for resources to be cleaned up

    # Check for orphaned DeviceClass resources from previous installations
    # kubectl equivalent: kubectl get deviceclasses.resource.k8s.io
    ret_code, device_classes, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io", version=dra_api_version, plural="deviceclasses"
    )

    if ret_code != 0:
        Logger.warning(f"Failed to get DeviceClasses: {err}")
    elif device_classes:
        for dc in device_classes:
            dc_name = dc["metadata"]["name"]
            annotations = dc["metadata"].get("annotations", {})
            # Check if it's from a different helm release
            helm_release = annotations.get("meta.helm.sh/release-name", "")
            helm_namespace = annotations.get("meta.helm.sh/release-namespace", "")
            if helm_release and (
                helm_release != dra_driver_release_name
                or helm_namespace != dra_driver_namespace
            ):
                Logger.warning(
                    f"Found orphaned DeviceClass '{dc_name}' from release '{helm_release}' in namespace '{helm_namespace}', deleting"
                )
                # kubectl equivalent: kubectl delete deviceclass <dc_name>
                ret_code, stdout, stderr = k8_util.k8_delete_custom_resource(
                    group="resource.k8s.io",
                    version=dra_api_version,
                    plural="deviceclasses",
                    namespace=None,  # DeviceClass is cluster-scoped
                    name=dc_name,
                )
                if ret_code != 0:
                    Logger.warning(f"Failed to delete DeviceClass {dc_name}: {stderr}")
                time.sleep(2)

    # Install DRA driver helm chart
    dra_chart = images.get("dra-driver.helm-chart", None)
    dra_version = images.get(
        "dra-driver.version", getattr(environment, "dra_driver_version", "v0.1.0")
    )

    Logger.info(f"DRA driver installation details:")
    Logger.info(f"  Helm chart: {dra_chart}")
    Logger.info(f"  Version: {dra_version}")
    Logger.info(f"  Release name: {dra_driver_release_name}")
    Logger.info(f"  Namespace: {dra_driver_namespace}")

    K8Helper.triage(
        environment,
        dra_chart is not None,
        "DRA driver helm chart not found in image manifest",
    )

    # Generate values.yaml for DRA driver if needed
    values_yaml = None
    # The image key is based on the 'key' field in the YAML, not the artifact name
    if images.get("image.repository.repository") or images.get(
        "dra-driver-image.repository"
    ):
        values_yaml = os.path.join(
            environment.logdir, f"dra_driver_values_{dra_version}.yaml"
        )
        dra_util.generate_dra_driver_values(images, values_yaml)
        Logger.info(f"  Values file: {values_yaml}")
        # Log the values file content
        if os.path.exists(values_yaml):
            with open(values_yaml, "r") as f:
                Logger.info(f"  Values file content:\n{f.read()}")
    else:
        Logger.info(f"  Using default values (no custom values.yaml)")

    Logger.info(f"Installing DRA driver helm chart from: {dra_chart}")
    ret_code, ret_stdout, ret_stderr = helm_util.helm_install(
        gpu_cluster,
        dra_driver_release_name,
        dra_driver_namespace,
        dra_chart,
        dra_version,
        values_yaml,
    )

    if ret_code != 0:
        Logger.error(f"Helm install failed with return code {ret_code}")
        Logger.error(f"stdout: {ret_stdout}")
        Logger.error(f"stderr: {ret_stderr}")
    K8Helper.triage(
        environment,
        ret_code == 0,
        f"Failed to install DRA driver helm chart: {ret_stderr}",
    )

    # Wait for DRA driver pods to be ready
    Logger.info("Waiting for DRA driver pods to be ready")
    time.sleep(30)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get GPU nodes")

    # Pod name format: {release-name}-{chart-name}-kubeletplugin
    pod_name_prefix = f"{dra_driver_release_name}-k8s-gpu-dra-driver-kubeletplugin"
    expected_pods = [
        common.PodInfo(pod_name_prefix, len(gpu_nodes), 1),
    ]

    failed_pods = k8_util.k8_check_pod_running(dra_driver_namespace, expected_pods)
    K8Helper.triage(
        environment, not failed_pods, f"DRA driver pods are not ready - {failed_pods}"
    )

    # Store DRA driver version in environment
    setattr(environment, "dra_driver_version", dra_version)

    yield dra_version

    # NOTE: Cleanup is intentionally NOT done here to allow manual inspection
    # after running individual tests. Cleanup will happen via:
    # 1. init_dra_testbed session fixture (before/after full test session)
    # 2. test_dra_driver_uninstall test (when explicitly run)
    Logger.info("DRA driver installation fixture complete (no auto-cleanup)")
    return
