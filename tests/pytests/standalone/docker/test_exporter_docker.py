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

"""
AMD Device Metrics Exporter (DME) Docker Container Test Suite.

This test suite validates the Docker-based deployment of the AMD Device Metrics
Exporter on GPU nodes. It covers:

- Docker container deployment with proper device access (/dev/dri, /dev/kfd)
- Container registry authentication and image pulling
- Volume mounting for configuration files (/tmp/etc/metrics:/etc/metrics)
- Metrics endpoint availability and responsiveness
- Configuration file handling and dynamic updates
- Profiler metrics enablement

Docker Deployment Model:
- Runs as daemon container with GPU device passthrough
- Exposes metrics on port 5000
- Mounts config from host filesystem
- Requires AMDGPU driver on host

Test Environment:
- Requires GPU cluster with Docker installed
- AMDGPU driver must be installed on host
- Network access to download reference config from ROCm repository
- Optional: Container registry credentials for private images

Key Dependencies:
- lib.k8_util: Kubernetes cluster utilities
- lib.util.K8Helper: Test assertion and triage utilities
- Docker daemon on GPU nodes
"""

import pdb
import pytest
import pprint
import sys
import os
import re
import time
import json
import logging
import requests
import lib.k8_util as k8_util
import lib.amdgpu as amdgpu
import lib.common as common
import lib.spec_util as spec_util
from lib.util import K8Helper

Logger = logging.getLogger("standalone.docker.test_exporter_docker")

@pytest.fixture(scope="module")
def run_exporter_docker_container(gpu_cluster, images, amdgpu_driver_install, environment):
    """
    Deploy AMD Metrics Exporter Docker container to all GPU nodes.

    This fixture handles the complete lifecycle of the Docker container deployment:

    Setup Phase:
    1. Resolve container image name from images dictionary
    2. Extract registry credentials if private registry is used
    3. Download reference config.json from ROCm repository
    4. Create /tmp/etc/metrics directory on each GPU node
    5. Upload config.json to host at /tmp/etc/metrics/config.json
    6. Login to container registry (if credentials provided)
    7. Run container in daemon mode with:
       - GPU device access: --device=/dev/dri --device=/dev/kfd
       - Port mapping: -p 5000:5000
       - Config volume mount: -v /tmp/etc/metrics:/etc/metrics
       - Container name: device-metrics-exporter
    8. Logout from registry (if login was performed)

    Teardown Phase (after all tests complete):
    1. Stop the container: docker stop device-metrics-exporter
    2. Remove the container: docker rm -f device-metrics-exporter

    Docker Run Command Format:
        docker run -d \\
          --device=/dev/dri \\
          --device=/dev/kfd \\
          -p 5000:5000 \\
          -v /tmp/etc/metrics:/etc/metrics \\
          --name device-metrics-exporter \\
          {image_repo}:{image_version}

    Device Passthrough:
        - /dev/dri: Direct Rendering Infrastructure for GPU access
        - /dev/kfd: Kernel Fusion Driver for ROCm/HIP workloads

    Args:
        gpu_cluster: GPU cluster fixture providing cluster node information
        images: Dictionary containing image repository, version, and optional registry secret
                Expected keys:
                - metricsExporter.image.repository: Image repository URL
                - metricsExporter.image.version: Image tag/version
                - metricsExporter.image.secret: (optional) Secret name for registry auth
        amdgpu_driver_install: Fixture ensuring AMDGPU driver is installed on host
        environment: Test environment fixture for triaging and logging

    Yields:
        None: This is a setup/teardown fixture

    Raises:
        AssertionError: If image is missing, container deployment fails, or cleanup fails
        pytest.fail: If metricsExporter.image.repository is not in images dict
    """
    global Logger
    Logger.debug("Deploy exporter docker container on each node")

    # Resolve container image name
    img = None
    if images.get('metricsExporter.image.repository', None):
        img = f"{images['metricsExporter.image.repository']}:{images['metricsExporter.image.version']}"
    else:
        pytest.fail(f"Missing device-metrics-exporter container image")

    # Extract registry credentials if needed
    registry_credentials = None
    if images.get('metricsExporter.image.secret', None):
        secret_name = images['metricsExporter.image.secret']
        for entry in gpu_cluster.k8_secrets["secrets"]:
            if entry['name'] == secret_name:
                registry_credentials = (entry["username"], entry["password"])

    # Download reference config.json from ROCm repository
    config_json_file = os.path.join(environment.logdir, "reference-config.json")
    try:
        url = "https://raw.githubusercontent.com/ROCm/device-metrics-exporter/refs/heads/main/example/config.json"
        resp = requests.get(url)
        K8Helper.triage(environment, (resp.status_code == 200), f"Failed to download reference config.json file")
        with open(config_json_file, "wb") as fp:
            fp.write(resp.content)
    except Exception as ae:
        Logger.error(f"Failed to download config.json from {url}, error : {ae}")

    K8Helper.triage(environment, (os.path.exists(config_json_file)), f"Failed to download reference config.json file")
    remote_file = "/tmp/etc/metrics/config.json"

    # Deploy container on each GPU node
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            # Prepare host directory for config volume mount
            ret_code, ret_stdout, ret_stderr = node.run_command(f"rm -rf /tmp/etc && mkdir -p /tmp/etc/metrics")
            K8Helper.triage(environment, (ret_code == 0), f"Failed init tmp folder /tmp/etc/metrics, error: {ret_stderr}")
            K8Helper.triage(environment, (node.put(config_json_file, remote_file)),
                            f"Unable to upload reference config.json")

            # Login to registry if credentials provided
            if registry_credentials:
                ret_code, ret_stdout, reg_stderr = node.run_command(f"docker login -u {registry_credentials[0]} -p {registry_credentials[1]}")
                Logger.debug(f"Result of docker login - retcode: {ret_code}")

            # Deploy container in daemon mode with GPU device access
            cmd = f"docker run -d --device=/dev/dri --device=/dev/kfd -p 5000:5000 -v /tmp/etc/metrics:/etc/metrics --name device-metrics-exporter {img}"
            ret_code, ret_stdout, ret_stderr = node.run_command(cmd)
            K8Helper.triage(environment, (ret_code == 0), f"Failed to deploy metrics-exporter container, error : {ret_stderr}")

            # Logout from registry
            if registry_credentials:
                ret_code, ret_stdout, reg_stderr = node.run_command(f"docker logout")
                Logger.debug(f"Result of docker logout - retcode: {ret_code}")

    yield

    # Teardown: Stop and remove containers
    Logger.debug("Stop and remove exporter docker container on each node")
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            cmd = f"docker stop device-metrics-exporter"
            ret_code, ret_stdout, ret_stderr = node.run_command(cmd)
            K8Helper.triage(environment, (ret_code == 0), f"Failed to stop metrics-exporter container, error : {ret_stderr}")

            cmd = f"docker rm -f device-metrics-exporter"
            ret_code, ret_stdout, ret_stderr = node.run_command(cmd)
            K8Helper.triage(environment, (ret_code == 0), f"Failed to cleanup metrics-exporter container, error : {ret_stderr}")
    return

def test_deploy_exporter_docker_container(gpu_cluster, run_exporter_docker_container, environment):
    """
    Verify Docker container deployment and metrics endpoint availability.

    This test validates that the AMD Metrics Exporter Docker container is successfully
    deployed and the metrics endpoint is accessible on all GPU nodes.

    Test Flow:
    1. Iterate through all GPU nodes in the cluster
    2. Send HTTP GET request to port 5000 at /metrics endpoint
    3. Verify successful response from each node
    4. Collect any failed endpoints

    Success Criteria:
        - All GPU nodes return metrics successfully without errors
        - No failed endpoints reported

    This is the basic smoke test to verify:
    - Container is running
    - GPU device access is working (/dev/dri, /dev/kfd)
    - Port mapping is correct (5000:5000)
    - Metrics exporter application started successfully inside container
    - Network connectivity to metrics endpoint

    Args:
        gpu_cluster: GPU cluster fixture
        run_exporter_docker_container: Deployment fixture ensuring container is running
        environment: Test environment for triaging

    Raises:
        AssertionError: If any GPU node fails to respond with metrics
    """
    global Logger
    failed_endpoints = set()
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.http_get(5000, "metrics")
            if ret_code != 0:
                failed_endpoints.add(node.ip_address)
                Logger.error(f"Failed to get metrics from nodeport endpoint for {node.ip_address}, stdout: {ret_stdout} stderr: {ret_stderr}")
    K8Helper.triage(environment, (len(failed_endpoints) == 0),
                    f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")

def test_apply_exporter_config(gpu_cluster, run_exporter_docker_container, environment):
    """
    Verify container uses reference configuration correctly.

    This test validates that the Docker container properly loads and applies the
    reference configuration file that was mounted via volume.

    Configuration Loading Mechanism:
        - Host config: /tmp/etc/metrics/config.json
        - Container mount: -v /tmp/etc/metrics:/etc/metrics
        - Container reads: /etc/metrics/config.json
        - Reference config URL: https://raw.githubusercontent.com/ROCm/device-metrics-exporter/refs/heads/main/example/config.json

    Test Flow:
    1. Query metrics endpoint on all GPU nodes
    2. Verify successful response from each node
    3. Log confirmation to check for supported metrics
    4. Collect any failed endpoints

    Success Criteria:
        - All GPU nodes return metrics successfully
        - Configuration is properly loaded from mounted volume
        - Default metrics from reference config are available

    Future Enhancement:
        Parse metrics output to verify specific metrics defined in reference config

    Args:
        gpu_cluster: GPU cluster fixture
        run_exporter_docker_container: Deployment fixture with reference config
        environment: Test environment for triaging

    Raises:
        AssertionError: If any GPU node fails to respond with metrics
    """
    global Logger

    # Verify metrics with reference config.json
    # https://raw.githubusercontent.com/ROCm/device-metrics-exporter/refs/heads/main/example/config.json
    failed_endpoints = set()
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.http_get(5000, "metrics")
            if ret_code != 0:
                failed_endpoints.add(node.ip_address)
                Logger.error(f"Failed to get metrics from nodeport endpoint for {node.ip_address}, stdout: {ret_stdout} stderr: {ret_stderr}")
            else:
                Logger.debug("Check for all supported metrics in the output")
    K8Helper.triage(environment, (len(failed_endpoints) == 0),
                    f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")

def test_enable_profiler_metrics(gpu_cluster, run_exporter_docker_container, environment):
    """
    Verify profiler metrics can be enabled via configuration file update.

    This test validates that the Docker container detects configuration file changes
    in the mounted volume and applies the profiler metrics configuration dynamically.

    Profiler Metrics:
        Profiler metrics provide detailed GPU performance data including:
        - GPU utilization statistics
        - Memory bandwidth metrics
        - Compute unit activity
        - Power consumption details
        - Shader engine utilization

    Configuration Changes:
        Creates a new config with:
        - CommonConfig.HealthService.Enable = false
        - GPUConfig.ProfilerMetrics.all = true

    Test Flow:
    1. Create profiler-metrics-config.json with profiler enabled
    2. Upload modified config to /tmp/etc/metrics/config.json on each node
       (This updates the file in the volume mounted by the container)
    3. Container detects file change and reloads configuration
    4. Query metrics endpoint to verify it still responds
    5. (Future: Validate profiler-specific metrics are present in output)
    6. Restore original reference config
    7. Upload restored config back to nodes

    Volume Mount Behavior:
        Since the container mounts -v /tmp/etc/metrics:/etc/metrics, any changes
        to /tmp/etc/metrics/config.json on the host are immediately visible to
        the container, allowing dynamic configuration updates without restart.

    Success Criteria:
        - Config file can be updated via mounted volume
        - Container accepts profiler metrics configuration
        - Metrics endpoint continues to function
        - Configuration restoration works

    Future Enhancement:
        Parse metrics output to verify profiler-specific metrics are present

    Args:
        gpu_cluster: GPU cluster fixture
        run_exporter_docker_container: Container deployment fixture
        environment: Test environment for triaging

    Raises:
        AssertionError: If config upload fails or metrics endpoint fails
    """
    global Logger

    # Build config.json with profiler metrics enabled
    config_json_file = os.path.join(environment.logdir, "profiler-metrics-config.json")
    config_map = {
        "CommonConfig": {
            "HealthService": {
                "Enable": False,
            },
        },
        "GPUConfig": {
            "ProfilerMetrics": {
                "all": True,
            }
        },
    }
    with open(config_json_file, "w") as fp:
        fp.write(json.dumps(config_map, indent=4))

    K8Helper.triage(environment, (os.path.exists(config_json_file)), f"Failed to create profiler-metrics-config.json file")

    # Upload modified config to mounted volume location
    remote_file = "/tmp/etc/metrics/config.json"
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, (node.put(config_json_file, remote_file)),
                            f"Unable to upload profiler-metrics config.json")

    # Verify metrics endpoint with profiler config
    failed_endpoints = set()
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.http_get(5000, "metrics")
            if ret_code != 0:
                failed_endpoints.add(node.ip_address)
                Logger.error(f"Failed to get metrics from nodeport endpoint for {node.ip_address}, stdout: {ret_stdout} stderr: {ret_stderr}")
            else:
                Logger.debug("Check for all profiler metrics in the output")
    K8Helper.triage(environment, (len(failed_endpoints) == 0),
                    f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")

    # Restore original reference configuration
    reference_cfg_json_file = os.path.join(environment.logdir, "reference-config.json")
    K8Helper.triage(environment, (os.path.exists(reference_cfg_json_file)), f"Failed to download reference config.json file")
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, (node.put(reference_cfg_json_file, remote_file)),
                            f"Unable to upload reference config.json")

def test_exporter_amdgpuhealth_hostpath(gpu_cluster, run_exporter_docker_container, environment):
    global Logger

    # Check if amdgpuhealth utility exists and is executable on each node - /var/lib/amd-metrics-exporter
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            # Check if directory exists
            cmd = "test -d /var/lib/amd-metrics-exporter"
            ret_code, resp_stdout, resp_stderr = node.run_command(cmd)
            K8Helper.triage(environment, ret_code == 0, f"Directory /var/lib/amd-metrics-exporter does not exist")
            Logger.debug(f"Directory /var/lib/amd-metrics-exporter exists")

            # List directory contents
            cmd = "ls -la /var/lib/amd-metrics-exporter"
            ret_code, resp_stdout, resp_stderr = node.run_command(cmd)
            Logger.info(f"Contents of /var/lib/amd-metrics-exporter:\n{resp_stdout}")

            # Check if file exists
            cmd = "test -f /var/lib/amd-metrics-exporter/amdgpuhealth"
            ret_code, resp_stdout, resp_stderr = node.run_command(cmd)
            K8Helper.triage(environment, ret_code == 0, f"File /var/lib/amd-metrics-exporter/amdgpuhealth does not exist")
            Logger.debug(f"File exists check passed for /var/lib/amd-metrics-exporter/amdgpuhealth")

            # Check if file is executable
            cmd = "test -x /var/lib/amd-metrics-exporter/amdgpuhealth"
            ret_code, resp_stdout, resp_stderr = node.run_command(cmd)
            K8Helper.triage(environment, ret_code == 0, f"File /var/lib/amd-metrics-exporter/amdgpuhealth is not executable")
            Logger.debug(f"File executable check passed for /var/lib/amd-metrics-exporter/amdgpuhealth")

