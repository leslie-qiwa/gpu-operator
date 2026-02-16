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
Shared helper functions for collecting node GPU information.

This module is used by:
- conftest.py gather_device_info fixture (pytest tests)
- scripts/collect_node_gpu_data.py (standalone script)
- DRA driver tests (for comparison and validation)

By centralizing this logic, we ensure consistency between test fixture
data collection and standalone script data collection.
"""

import re
import logging
from typing import Dict, List, Tuple

import lib.k8_util as k8_util
import lib.amdgpu as amdgpu_util

Logger = logging.getLogger("lib.node_gpu_collector")


def collect_gpu_hardware_info(k8_cluster, node_name: str) -> Dict:
    """
    Collect GPU hardware information using lspci.

    This is the same logic used by gather_device_info fixture.

    Args:
        k8_cluster: K8 cluster object
        node_name: Name of the node

    Returns:
        Dict with GPU hardware information:
        {
            "gpus": [
                {
                    "pci_address": "83:00.0",
                    "device_id": "74a1",
                    "gpu_series": "MI300X",
                    "description": "...",
                    "full_line": "..."
                }
            ],
            "device_ids": ["74a1"],
            "total_gpus": 8
        }
    """
    hardware_info = {"gpus": [], "device_ids": set(), "total_gpus": 0}

    # Run lspci to find AMD devices (vendor ID 1002)
    # This is exactly what gather_device_info does
    cmd = ["lspci", "-nn", "-d", "1002:"]
    result = k8_util.run_command_on_node(k8_cluster, node_name, cmd)

    # Handle case where run_command_on_node returns None (all retries failed)
    if result is None:
        Logger.error(
            f"run_command_on_node returned None for node {node_name} (all retries failed)"
        )
        return hardware_info

    ret_code, output = result

    if ret_code != 0:
        Logger.error(f"Failed to run lspci on node {node_name}: exit code {ret_code}")
        return hardware_info

    if not output:
        Logger.debug(f"Node {node_name} does not have any AMD GPU devices")
        return hardware_info

    # Parse lspci output
    # Pattern used by gather_device_info fixture
    pattern = r"(?:Processing accelerators|Display controller).*1002:([0-9a-fA-F]{4})"

    # Enhanced pattern for full parsing
    full_pattern = r"([0-9a-fA-F:\.]+)\s+(.*?):\s+(.*)\[1002:([0-9a-fA-F]{4})\]"

    for line in output.split("\n"):
        if not line:
            continue

        # Try full pattern first
        match = re.search(full_pattern, line)
        if match:
            pci_addr = match.group(1)
            device_type = match.group(2).strip()
            description = match.group(3).strip()
            device_id = match.group(4)

            # Only include GPUs (same filter as gather_device_info)
            if (
                "Processing accelerators" in device_type
                or "Display controller" in device_type
            ):
                # Get GPU series using same utility as gather_device_info
                gpu_series = amdgpu_util.get_amdgpu_device_series(device_id)

                hardware_info["gpus"].append(
                    {
                        "pci_address": pci_addr,
                        "device_id": device_id,
                        "device_type": device_type,
                        "description": description,
                        "gpu_series": gpu_series,
                        "full_line": line.strip(),
                    }
                )

                hardware_info["device_ids"].add(device_id)
                hardware_info["total_gpus"] += 1
        else:
            # Fallback to simple pattern (gather_device_info pattern)
            match = re.search(pattern, line)
            if match:
                device_id = match.group(1)
                gpu_series = amdgpu_util.get_amdgpu_device_series(device_id)

                hardware_info["gpus"].append(
                    {
                        "device_id": device_id,
                        "gpu_series": gpu_series,
                        "full_line": line.strip(),
                    }
                )

                hardware_info["device_ids"].add(device_id)
                hardware_info["total_gpus"] += 1

    hardware_info["device_ids"] = list(hardware_info["device_ids"])

    Logger.info(f"Node {node_name}: Found {hardware_info['total_gpus']} AMD GPU(s)")

    return hardware_info


def collect_gpu_partition_profiles(k8_cluster, node_name: str) -> List[str]:
    """
    Collect GPU partition profiles from sysfs.

    Args:
        k8_cluster: K8 cluster object
        node_name: Name of the node

    Returns:
        List of partition profile strings (e.g., ["spx_8gb", "spx_16gb"])
    """
    partition_profiles = []

    # Find partition profile files
    cmd = ["find", "/sys/class/drm", "-name", "partition_profile", "-type", "f"]
    result = k8_util.run_command_on_node(k8_cluster, node_name, cmd)

    # Handle case where run_command_on_node returns None
    if result is None:
        Logger.error(
            f"run_command_on_node returned None for node {node_name} (all retries failed)"
        )
        return partition_profiles

    ret_code, output = result

    if ret_code != 0 or not output or not output.strip():
        Logger.debug(f"Node {node_name}: No partition profile files found")
        return partition_profiles

    # Read each partition profile file
    for part_file in output.strip().split("\n"):
        if part_file.strip():
            cmd = ["cat", part_file.strip()]
            result = k8_util.run_command_on_node(k8_cluster, node_name, cmd)

            # Handle None return
            if result is None:
                Logger.warning(
                    f"Failed to read partition file {part_file} on node {node_name}"
                )
                continue

            ret_code, content = result

            if ret_code == 0 and content and content.strip():
                partition_profiles.append(content.strip())

    Logger.info(
        f"Node {node_name}: Found {len(partition_profiles)} partition profile(s): {partition_profiles}"
    )

    return partition_profiles


def populate_cluster_node_with_gpu_info(k8_cluster, cluster_node, node_name: str):
    """
    Populate a cluster_node object with GPU information.

    This replicates what gather_device_info fixture does.
    Used by the fixture and can be used by other code.

    Args:
        k8_cluster: K8 cluster object
        cluster_node: cluster_node object to populate
        node_name: Name of the node
    """
    # Collect hardware info
    hw_info = collect_gpu_hardware_info(k8_cluster, node_name)

    # Populate cluster_node (same as gather_device_info does)
    if hw_info["total_gpus"] > 0:
        # Use first GPU's info (same as gather_device_info)
        first_gpu = hw_info["gpus"][0]
        cluster_node.device_id = first_gpu["device_id"]
        cluster_node.gpu_series = first_gpu["gpu_series"]
        cluster_node.num_gpus = hw_info["total_gpus"]
    else:
        cluster_node.device_id = None
        cluster_node.gpu_series = None
        cluster_node.num_gpus = 0


def extend_cluster_node_with_partition_info(k8_cluster, cluster_node, node_name: str):
    """
    Extend a cluster_node object with partition profile information.

    This can be used to augment the data from gather_device_info.

    Args:
        k8_cluster: K8 cluster object
        cluster_node: cluster_node object to extend
        node_name: Name of the node
    """
    partition_profiles = collect_gpu_partition_profiles(k8_cluster, node_name)

    # Add partition_profiles attribute to cluster_node
    cluster_node.partition_profiles = partition_profiles


def populate_all_cluster_nodes_with_gpu_info(k8_cluster):
    """
    Populate all cluster nodes with GPU and OS information.

    This encapsulates the entire collection loop from gather_device_info fixture.
    Can be used by both pytest fixtures and standalone scripts.

    Args:
        k8_cluster: K8 cluster object with nodes

    Returns:
        Tuple of (success: bool, error_message: str)
    """
    # Get all Kubernetes nodes
    ret_code, k8_nodes = k8_util.k8_get_nodes()
    if ret_code != 0:
        return False, "Unable to collect node information from k8-cluster"

    if len(k8_nodes) == 0:
        return False, "No nodes found in k8-cluster"

    # Populate each cluster node
    for node in k8_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        node_ip = k8_util.k8_get_node_address(node)
        os_type, os_name, os_version = k8_util.k8_get_node_os_info(node)

        # Find corresponding cluster_node object
        cluster_node = k8_cluster.find_node_by_ip(node_ip)

        # Populate OS information
        cluster_node.host_name = node_name
        cluster_node.host_os_type = os_type
        cluster_node.host_os_name = os_name
        cluster_node.host_os_version = os_version

        # Populate GPU information using shared helper
        populate_cluster_node_with_gpu_info(k8_cluster, cluster_node, node_name)

    Logger.info(f"Populated {len(k8_nodes)} cluster node(s) with GPU information")

    return True, ""
