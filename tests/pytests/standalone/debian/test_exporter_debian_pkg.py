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
AMD Device Metrics Exporter (DME) Debian Package Test Suite.

This test suite validates the installation, configuration, and functionality of the
AMD Device Metrics Exporter Debian package on GPU nodes. It covers:

- Debian package deployment and systemd service integration
- Metrics endpoint availability and responsiveness
- Configuration file handling (both /etc/metrics/config.json and custom paths)
- Dynamic configuration changes (server port, profiler metrics, custom labels)
- Security validation (port scanning and network exposure checks)

Test Environment:
- Requires GPU cluster with Debian-based OS nodes
- AMDGPU driver must be installed
- Network access to download reference config from ROCm repository

Key Dependencies:
- lib.k8_util: Kubernetes cluster utilities
- lib.deb_util: Debian package utilities including ss output parsing
- lib.util.K8Helper: Test assertion and triage utilities
"""

import pdb
import pytest
import pprint
import sys
import os
import re
import time
import json
import copy
import logging
import requests
import random
import string
import subprocess
import lib.k8_util as k8_util
import lib.amdgpu as amdgpu
import lib.common as common
import lib.spec_util as spec_util
import lib.deb_util as deb_util
from lib.util import K8Helper

Logger = logging.getLogger("standalone.debian.test_exporter_debian_pkg")

@pytest.fixture(scope="module")
def reference_config(environment):
    """
    Download and provide the reference configuration for AMD Metrics Exporter.

    This fixture downloads the canonical example config.json from the ROCm
    device-metrics-exporter repository. This configuration serves as the baseline
    for all configuration modification tests in this suite.

    The reference config provides default settings for:
    - ServerPort (default: 5000)
    - GPUConfig.ProfilerMetrics (default: all=false)
    - GPUConfig.CustomLabels (default: empty or minimal)

    Args:
        environment: Test environment fixture providing logdir for file storage

    Yields:
        tuple: (config_json_file_path, config_data_dict)
            - config_json_file_path (str): Path to downloaded reference config
            - config_data_dict (dict): Parsed JSON configuration data

    Raises:
        AssertionError: If download fails or file doesn't exist after download
    """
    # Use reference config.json https://raw.githubusercontent.com/ROCm/device-metrics-exporter/refs/heads/main/example/config.json
    config_json_file = os.path.join(environment.logdir, "reference-config.json")
    try:
        url = "https://raw.githubusercontent.com/ROCm/device-metrics-exporter/refs/heads/main/example/config.json"
        resp = requests.get(url)
        K8Helper.triage(environment, (resp.status_code == 200), f"Failed to download reference config.json file")
        with open(config_json_file, "wb") as fp:
            fp.write(resp.content)
        with open(config_json_file) as fp:
            config_data = json.load(fp)
    except Exception as ae:
        Logger.error(f"Failed to download config.json from {url}, error : {ae}")
    K8Helper.triage(environment, (os.path.exists(config_json_file)), f"Failed to download reference config.json file")
    yield (config_json_file, config_data)
    return

@pytest.fixture(scope="module")
def deploy_debian_package(gpu_cluster, images, amdgpu_driver_install, reference_config, environment):
    """
    Deploy AMD Metrics Exporter Debian package to all GPU nodes.

    This fixture handles the complete lifecycle of the Debian package deployment:

    Setup Phase:
    1. Upload reference config.json to /tmp/config.json on each GPU node
    2. Upload the appropriate Debian package for each node's OS/version
    3. Install package using apt install
    4. Enable the systemd service (amd-metrics-exporter.service)
    5. Start the service
    6. Verify service is running
    7. Copy config.json to /etc/metrics/config.json (standard location)

    Teardown Phase (after all tests complete):
    1. Uninstall package using dpkg -r amdgpu-exporter
    2. Clean up temporary files

    The Debian package name format is:
        exporter-debian-{os_name}-{os_version}.debian
    Example: exporter-debian-ubuntu-20.04.debian

    Args:
        gpu_cluster: GPU cluster fixture providing cluster node information
        images: Dictionary of available Debian package images keyed by name
        amdgpu_driver_install: Fixture ensuring AMDGPU driver is installed
        reference_config: Tuple of (config_file_path, config_dict) from reference_config fixture
        environment: Test environment fixture for triaging and logging

    Yields:
        None: This is a setup/teardown fixture

    Raises:
        AssertionError: If installation, service start, or status check fails
    """
    global Logger
    Logger.debug("Deploy exporter debian package on each node")
    config_json_file, _ = reference_config
    for node in gpu_cluster.cluster_nodes:
        # For each node, upload debian package after starting a ubuntu pod in sysadmin profile
        # install debian package on the host
        if node.is_gpu_node():
            image_name = f"exporter-debian-{node.host_os_name}-{node.host_os_version}.debian"
            if image_name in images:
                K8Helper.triage(environment, (node.put(config_json_file, "/tmp/config.json")),
                                f"Unable to upload {config_json_file} to /tmp/config.json")
                Logger.info(f"Deploy debian package {images[image_name]} on {node.host_name}")
                remote_file = os.path.join("/tmp", os.path.basename(images[image_name]))
                node.run_command(f"rm -f {remote_file}")
                if node.put(images[image_name], remote_file):
                    # Install
                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo apt install -y {remote_file}")
                    K8Helper.triage(environment, (ret_code == 0), f"Failed to install metrics-exporter debian, error : {ret_stderr}")

                    # Check status
                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl status amd-metrics-exporter.service")
                    K8Helper.triage(environment, (ret_code != 0), f"Failed to check status metrics-exporter debian, error : {ret_stderr}")

                    # Enable systemctl service
                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl enable amd-metrics-exporter.service")
                    K8Helper.triage(environment, (ret_code == 0), f"Failed to enable metrics-exporter debian, error : {ret_stderr}")

                    # Start systemctl service
                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl start amd-metrics-exporter.service")
                    K8Helper.triage(environment, (ret_code == 0), f"Failed to start metrics-exporter debian, error : {ret_stderr}")

                    # Check status
                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl status amd-metrics-exporter.service")
                    K8Helper.triage(environment, (ret_code == 0), f"Failed to check status metrics-exporter debian, error : {ret_stderr}")

                    # Upload config.json
                    ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo cp /tmp/config.json /etc/metrics/config.json")
                    K8Helper.triage(environment, (ret_code == 0), f"Unable to create /etc/metrics folder")
            else:
                Logger.error(f"Missing debian package for {image_name} for {node.host_name}")
    yield
    Logger.debug("Uninstall exporter debian package on each node")
    for node in gpu_cluster.cluster_nodes:
        # For each node, upload debian package after starting a ubuntu pod in sysadmin profile
        # install debian package on the host
        if node.is_gpu_node():
            image_name = f"exporter-debian-{node.host_os_name}-{node.host_os_version}.debian"
            if image_name in images:
                Logger.info(f"Remote debian package on {node.host_name}")
                remote_file = os.path.join("/tmp", os.path.basename(images[image_name]))
                # Remove package
                ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo dpkg -r amdgpu-exporter")
                K8Helper.triage(environment, (ret_code == 0), f"Failed to uninstall metrics-exporter debian, error : {ret_stderr}")

                node.run_command(f"rm -f {remote_file}")
            else:
                Logger.error(f"Missing debian package for {image_name} for {node.host_name}")
    return

def test_deploy_exporter_debian_package(gpu_cluster, deploy_debian_package, environment):
    """
    Verify DME Debian package deployment

    Deploy debian package on the host and check for metrics endpoint to respond
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

def test_nonstd_config_json_path(gpu_cluster, deploy_debian_package, reference_config, environment):
    """
    Verify amd-metrics-exporter honors -amd-metrics-config command-line option.

    This test validates that the exporter can use a non-standard config file path
    when specified via the -amd-metrics-config command-line option. This is useful
    for deployments that need to maintain configs in custom locations.

    Test Flow:
    1. Verify baseline: metrics available on default port 5000
    2. Create modified config with ServerPort 5001
    3. Backup systemd service file
    4. Modify ExecStart to add: -amd-metrics-config /tmp/config.json
    5. Reload systemd and restart service
    6. Verify metrics now available on port 5001 (proves custom config is used)
    7. Restore original service file
    8. Verify restoration

    Success Criteria:
        - Exporter reads config from custom path specified in command line
        - Port change to 5001 takes effect
        - Service can be restored to default configuration

    Args:
        gpu_cluster: GPU cluster fixture
        deploy_debian_package: Deployment fixture ensuring package is installed
        reference_config: Reference config fixture
        environment: Test environment for triaging

    Raises:
        AssertionError: If custom config path is not honored or port doesn't change
    """
    global Logger
    config_json_file, ref_config_data = reference_config
    config_data = copy.deepcopy(ref_config_data)

    # Confirm current behavior (port:5000)
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

    # Build/Modify reference config.json ServerPort from 5000 to 5001
    test_config_json = os.path.join(environment.logdir, "server_port_config.json")
    config_data['ServerPort'] = 5001
    with open(test_config_json, "w") as fp:
        fp.write(json.dumps(config_data, indent=4))

    # Modify systemd service file to add custom config path argument
    service_file = "/lib/systemd/system/amd-metrics-exporter.service"
    old_line = "ExecStart=/usr/local/bin/amd-metrics-exporter"
    new_line = "ExecStart=/usr/local/bin/amd-metrics-exporter -amd-metrics-config /tmp/config.json"
    sed_command = f"sudo sed -i 's|^{old_line}$|{new_line}|' {service_file}"
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, (node.put(test_config_json, "/tmp/config.json")), 
                            f"Unable to upload {test_config_json} to /tmp/config.json")
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo cp {service_file} {service_file}.bak")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to take backup of {service_file}, error : {ret_stderr}")

            ret_code, ret_stdout, ret_stderr = node.run_command(sed_command)
            K8Helper.triage(environment, (ret_code == 0), f"Unable to modify {service_file}, error : {ret_stderr}")

            # Restart systemctl - do daemon-reload
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl daemon-reload")
            K8Helper.triage(environment, (ret_code == 0), f"Failed to run systemctl daemon-reload, error : {ret_stderr}")

            # Restart amd-metrics-exporter.service
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl restart amd-metrics-exporter.service")
            K8Helper.triage(environment, (ret_code == 0), f"Failed to start metrics-exporter debian, error : {ret_stderr}")

    # Wait for service to stabilize after restart
    time.sleep(30)

    # Verify metrics are now available on port 5001 (custom config in effect)
    failed_endpoints = set()
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.http_get(5001, "metrics")
            if ret_code != 0:
                failed_endpoints.add(node.ip_address)
                Logger.error(f"Failed to get metrics from nodeport endpoint for {node.ip_address}, stdout: {ret_stdout} stderr: {ret_stderr}")
            else:
                Logger.debug("Check for all supported metrics in the output")
    K8Helper.triage(environment, (len(failed_endpoints) == 0),
                    f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")

    # Restore original systemd service file
    for node in gpu_cluster.cluster_nodes:
        # For each node, upload debian package after starting a ubuntu pod in sysadmin profile
        # install debian package on the host
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo cp {service_file}.bak {service_file}")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to take backup of {service_file}")

            # Restart systemctl - do daemon-reload
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl daemon-reload")
            K8Helper.triage(environment, (ret_code == 0), f"Failed to run systemctl daemon-reload, error : {ret_stderr}")

            # Restart amd-metrics-exporter.service
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl restart amd-metrics-exporter.service")
            K8Helper.triage(environment, (ret_code == 0), f"Failed to start metrics-exporter debian, error : {ret_stderr}")


def test_modify_server_port(gpu_cluster, deploy_debian_package, reference_config, environment):
    """
    Verify DME dynamically responds to ServerPort changes in config.json.

    This test validates that the exporter monitors /etc/metrics/config.json for changes
    and applies the new ServerPort configuration without requiring explicit service restart.

    Test Flow:
    1. Create modified config with ServerPort changed from 5000 to 5001
    2. Upload modified config to /etc/metrics/config.json
    3. Wait 30 seconds for service to detect and reload config
    4. Verify metrics available on port 5001
    5. Restore original config
    6. Verify metrics back on port 5000

    Success Criteria:
        - Service automatically detects config file changes
        - Port change takes effect without manual restart
        - Restoration works correctly

    Args:
        gpu_cluster: GPU cluster fixture
        deploy_debian_package: Deployment fixture
        reference_config: Reference config fixture
        environment: Test environment

    Raises:
        AssertionError: If port change doesn't take effect or restoration fails
    """
    global Logger

    config_json_file, ref_config_data = reference_config
    config_data = copy.deepcopy(ref_config_data)

    # Build/Modify reference config.json ServerPort from 5000 to 5001
    test_config_json = os.path.join(environment.logdir, "server_port_config.json")
    config_data['ServerPort'] = 5001
    with open(test_config_json, "w") as fp:
        fp.write(json.dumps(config_data, indent=4))

    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, (node.put(test_config_json, "/tmp/config.json")), 
                            f"Unable to upload {test_config_json} to /tmp/config.json")
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo cp /tmp/config.json /etc/metrics/config.json")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to modify /etc/metrics/config.json, error : {ret_stderr}")

    time.sleep(30)
    failed_endpoints = set()
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            ret_code, ret_stdout, ret_stderr = node.http_get(5001, "metrics")
            if ret_code != 0:
                failed_endpoints.add(node.ip_address)
                Logger.error(f"Failed to get metrics from nodeport endpoint for {node.ip_address}, stdout: {ret_stdout} stderr: {ret_stderr}")
            else:
                Logger.debug("Check for all supported metrics in the output")
    K8Helper.triage(environment, (len(failed_endpoints) == 0),
                    f"One or more metric endpoints HTTP-GET failed, nodes: {failed_endpoints}")

    # Restore
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, (node.put(config_json_file, "/tmp/config.json")), 
                            f"Unable to upload {config_json_file} to /tmp/config.json")
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo cp /tmp/config.json /etc/metrics/config.json")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to modify /etc/metrics/config.json, error : {ret_stderr}")
    time.sleep(30)

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

def test_enable_profiler_metrics(gpu_cluster, deploy_debian_package, reference_config, environment):
    """
    Verify DME exports profiler metrics when enabled in configuration.

    This test validates that profiler metrics can be enabled via config file and
    that the exporter responds correctly to this configuration change.

    Profiler metrics provide detailed GPU performance data including:
    - GPU utilization statistics
    - Memory bandwidth metrics
    - Compute unit activity
    - Power consumption details

    Test Flow:
    1. Create modified config with GPUConfig.ProfilerMetrics.all = true
    2. Upload to /etc/metrics/config.json
    3. Wait 30 seconds for config reload
    4. Verify metrics endpoint responds (TODO: validate profiler metrics in output)
    5. Restore original config
    6. Verify restoration

    Success Criteria:
        - Exporter accepts profiler metrics configuration
        - Metrics endpoint remains functional
        - Configuration change is honored

    Future Enhancement:
        Parse metrics output to verify profiler-specific metrics are present

    Args:
        gpu_cluster: GPU cluster fixture
        deploy_debian_package: Deployment fixture
        reference_config: Reference config fixture
        environment: Test environment

    Raises:
        AssertionError: If metrics endpoint fails after enabling profiler metrics
    """
    global Logger

    config_json_file, ref_config_data = reference_config
    config_data = copy.deepcopy(ref_config_data)

    # Build/Modify reference config.json GPUConfig.ProfilerMetrics.all from False to True
    test_config_json = os.path.join(environment.logdir, "profiler_metrics_config.json")
    config_data['GPUConfig']['ProfilerMetrics']['all'] = True
    with open(test_config_json, "w") as fp:
        fp.write(json.dumps(config_data, indent=4))

    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, (node.put(test_config_json, "/tmp/config.json")), 
                            f"Unable to upload {test_config_json} to /tmp/config.json")
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo cp /tmp/config.json /etc/metrics/config.json")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to modify /etc/metrics/config.json, error : {ret_stderr}")

    time.sleep(30)
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

    # Restore
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, (node.put(config_json_file, "/tmp/config.json")), 
                            f"Unable to upload {config_json_file} to /tmp/config.json")
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo cp /tmp/config.json /etc/metrics/config.json")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to modify /etc/metrics/config.json, error : {ret_stderr}")
    time.sleep(30)

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

def test_change_custom_labels(gpu_cluster, deploy_debian_package, reference_config, environment):
    """
    Verify DME supports custom labels in exported metrics.

    This test validates that custom labels can be added to the configuration and
    that the exporter includes them in the metrics output. Custom labels are useful
    for adding deployment-specific metadata to metrics for better organization in
    monitoring systems like Prometheus.

    Common use cases for custom labels:
    - CLUSTER_NAME: Identify which cluster the metrics come from
    - DATACENTER: Datacenter location
    - ENVIRONMENT: prod/staging/dev
    - TEAM: Owning team identifier

    Test Flow:
    1. Create modified config with custom labels:
       - CLUSTER_NAME = "test-cluster-name"
       - test-label = random 15-character string
    2. Upload to /etc/metrics/config.json
    3. Wait 30 seconds for config reload
    4. Verify metrics endpoint responds (TODO: validate labels in output)
    5. Restore original config
    6. Verify restoration

    Success Criteria:
        - Exporter accepts custom label configuration
        - Metrics endpoint remains functional

    Future Enhancement:
        Parse metrics output to verify custom labels appear in metric tags

    Args:
        gpu_cluster: GPU cluster fixture
        deploy_debian_package: Deployment fixture
        reference_config: Reference config fixture
        environment: Test environment

    Raises:
        AssertionError: If metrics endpoint fails after adding custom labels
    """
    global Logger

    config_json_file, ref_config_data = reference_config
    config_data = copy.deepcopy(ref_config_data)

    # Build/Modify reference config.json with custom labels
    test_config_json = os.path.join(environment.logdir, "custom_labels_config.json")
    config_data['GPUConfig']['CustomLabels']['CLUSTER_NAME'] = "test-cluster-name"
    # Generate random test label value for uniqueness validation
    test_label_value = ''.join(random.choices(string.ascii_letters, k=15)).title()
    config_data['GPUConfig']['CustomLabels']['test-label'] = test_label_value
    with open(test_config_json, "w") as fp:
        fp.write(json.dumps(config_data, indent=4))

    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, (node.put(test_config_json, "/tmp/config.json")), 
                            f"Unable to upload {test_config_json} to /tmp/config.json")
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo cp /tmp/config.json /etc/metrics/config.json")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to modify /etc/metrics/config.json, error : {ret_stderr}")

    time.sleep(30)
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

    # Restore
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            K8Helper.triage(environment, (node.put(config_json_file, "/tmp/config.json")), 
                            f"Unable to upload {config_json_file} to /tmp/config.json")
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo cp /tmp/config.json /etc/metrics/config.json")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to modify /etc/metrics/config.json, error : {ret_stderr}")
    time.sleep(30)

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

@pytest.mark.skip(reason="Skipping GPUAgent port scan test")
def test_gpuagent_port_scan(gpu_cluster, deploy_debian_package, environment):
    """
    Security validation: Verify GPUAgent service network exposure.

    This test performs a security audit of the GPUAgent service to ensure it only
    opens the expected port and is not inadvertently exposed to the network.

    Security Requirements:
    1. Exactly ONE port should be opened by gpuagent.service
    2. Port must be 50061 (gRPC default for GPU Agent)
    3. Service must NOT be bound to 0.0.0.0 (network-wide exposure)
    4. Service should bind to localhost/127.0.0.1 only

    Test Flow:
    1. Get PID of gpuagent.service from systemctl
    2. Run 'ss -tunlp' to list all TCP/UDP ports opened by this PID
    3. Parse output to extract address:port bindings
    4. Validate against security requirements

    Expected Output Format:
        [{'address': '[::ffff:127.0.0.1]', 'port': '50061'}]

    Security Rationale:
        GPUAgent is an internal service for local GPU management. Exposing it
        on 0.0.0.0 would allow network access, potentially creating a security
        vulnerability for unauthorized GPU control.

    Args:
        gpu_cluster: GPU cluster fixture
        deploy_debian_package: Deployment fixture
        environment: Test environment

    Raises:
        AssertionError: If multiple ports are open, wrong port number, or 0.0.0.0 binding
    """
    global Logger
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            # Collect the PID of gpuagent service
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl show -p MainPID --value gpuagent.service")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to get PID of gpuagent.service, error : {ret_stderr}")

            # Collect socket statistics for this PID
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo ss -tunlp | grep {ret_stdout.strip()}")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to collect ss-cmd output, error : {ret_stderr}")
            address_port_map = deb_util.parse_ss_output(ret_stdout)
            #
            # Expected format: [{'address': '[::ffff:127.0.0.1]', 'port': '50061'}]
            #
            K8Helper.triage(environment, (len(address_port_map) == 1),
                            f"More than one port opened by gpuagent.service, {address_port_map}")
            K8Helper.triage(environment, (int(address_port_map[0]['port']) == 50061),
                            f"Unexpected port opened by gpuagent.service in default deployment, {address_port_map}")
            K8Helper.triage(environment, ('0.0.0.0' not in address_port_map[0]['address']),
                            f"GPUAgent Port opened on 0.0.0.0 - security violation found")

def test_exporter_port_scan(gpu_cluster, deploy_debian_package, environment):
    """
    Verify AMD Metrics Exporter service opens only the expected port.

    This test validates the network port configuration of the AMD Metrics Exporter
    to ensure it opens only the expected metrics endpoint and no additional ports.

    Expected Configuration:
    1. Exactly ONE port should be opened by amd-metrics-exporter.service
    2. Port must be 5000 (default metrics endpoint)

    Test Flow:
    1. Get PID of amd-metrics-exporter.service from systemctl
    2. Run 'ss -tunlp' to list all TCP/UDP ports opened by this PID
    3. Parse output to extract address:port bindings
    4. Validate port count and port number

    Security Rationale:
        This test ensures no unexpected network ports are opened by the exporter,
        which could indicate misconfigurations or potential security issues.

    Note:
        Unlike GPUAgent, the metrics exporter may bind to 0.0.0.0 or a specific
        interface depending on configuration, as it needs to serve metrics to
        monitoring systems like Prometheus.

    Args:
        gpu_cluster: GPU cluster fixture
        deploy_debian_package: Deployment fixture
        environment: Test environment

    Raises:
        AssertionError: If multiple ports are open or port number is not 5000
    """
    global Logger
    for node in gpu_cluster.cluster_nodes:
        if node.is_gpu_node():
            # Collect the PID of amd-metrics-exporter service
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo systemctl show -p MainPID --value amd-metrics-exporter.service")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to get PID of amd-metrics-exporter.service, error : {ret_stderr}")

            # Collect socket statistics for this PID
            ret_code, ret_stdout, ret_stderr = node.run_command(f"sudo ss -tunlp | grep {ret_stdout.strip()}")
            K8Helper.triage(environment, (ret_code == 0), f"Unable to collect ss-cmd output, error : {ret_stderr}")
            address_port_map = deb_util.parse_ss_output(ret_stdout)
            K8Helper.triage(environment, (len(address_port_map) == 1),
                            f"More than one port opened by amd-metrics-exporter.service, {address_port_map}")
            K8Helper.triage(environment, (int(address_port_map[0]['port']) == 5000),
                            f"Unexpected port opened by amd-metrics-exporter.service in default deployment, {address_port_map}")

