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


def collect_gpu_hardware_info_by_lspci(k8_cluster, node_name: str) -> Dict:
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
            ]
        }
    """
    hardware_info = {"gpus": []}

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

    Logger.info(f"Node {node_name}: Found {len(hardware_info['gpus'])} AMD GPU(s)")

    return hardware_info


def collect_gpu_hardware_info_by_sysfs(k8_cluster, node_name: str) -> Dict:
    """
    Collect GPU hardware information from sysfs for each PCI device.

    Reads from /sys/module/amdgpu/drivers/pci:amdgpu/<pci_addr>/ for each GPU.
    Collects partition configuration attributes.

    Optimized to use a single run_command_on_node call instead of multiple calls.

    Args:
        k8_cluster: K8 cluster object
        node_name: Name of the node

    Returns:
        Dict with GPU information organized by PCI address:
        {
            "gpus": {
                "0000:06:00.0": {
                    "pci_address": "0000:06:00.0",
                    "available_compute_partition": "SPX, DPX, QPX, CPX",
                    "available_memory_partition": "NPS1, NPS2",
                    "current_compute_partition": "SPX",
                    "current_memory_partition": "NPS1",
                    ...
                }
            }
        }
    """
    Logger.info(f"Collecting GPU sysfs information for node: {node_name}")
    sysfs_info = {"gpus": {}}

    # List of sysfs attributes to collect for each GPU
    attributes_to_read = [
        "available_compute_partition",
        "available_memory_partition",
        "current_compute_partition",
        "current_memory_partition",
        "device",
        "vendor",
        "product_name",
        "product_number",
        "serial_number",
        "vbios_version",
    ]

    Logger.debug(f"Attributes to collect per GPU: {', '.join(attributes_to_read)}")
    Logger.debug(
        "Also collecting: cardIndex, renderIndex, driverVersion, driverSrcVersion"
    )

    # Build a single shell script that collects all data in one execution
    # This reduces the number of pod creations from 1+(N*10) to just 1
    # Output format: PCI_ADDR|attr1|value1|attr2|value2|...
    Logger.info("Building optimized sysfs collection script (single pod execution)")
    script = f"""
    set -e
    base_dir="/sys/module/amdgpu/drivers/pci:amdgpu"

    if [ ! -d "$base_dir" ]; then
        echo "ERROR: Directory $base_dir not found"
        exit 1
    fi

    cd "$base_dir"

    # Get driver version info (global, same for all GPUs)
    driver_version=$(cat /sys/module/amdgpu/version 2>/dev/null || echo "")
    driver_src_version=$(cat /sys/module/amdgpu/srcversion 2>/dev/null || echo "")

    # Find all PCI addresses (format: 0000:06:00.0)
    for pci_addr in $(ls -1 | grep -E '^[0-9a-fA-F]{{4}}:[0-9a-fA-F]{{2}}:[0-9a-fA-F]{{2}}\\.[0-9]$'); do
        echo "GPU_START|$pci_addr"

        # Read each attribute from PCI device directory
        {' '.join([f'''
        if [ -f "$pci_addr/{attr}" ]; then
            value=$(cat "$pci_addr/{attr}" 2>/dev/null || echo "")
            echo "ATTR|{attr}|$value"
        else
            echo "ATTR|{attr}|"
        fi''' for attr in attributes_to_read])}

        # Add driver version (same for all GPUs on this node)
        echo "ATTR|driverVersion|$driver_version"
        echo "ATTR|driverSrcVersion|$driver_src_version"

        # Find cardIndex by matching PCI address in /sys/class/drm/cardX/device symlink
        card_index=""
        for drm_card in /sys/class/drm/card*; do
            if [ -L "$drm_card/device" ]; then
                card_pci=$(readlink -f "$drm_card/device" 2>/dev/null || echo "")
                if [[ "$card_pci" == *"$pci_addr"* ]]; then
                    card_index=$(basename "$drm_card" | sed 's/card//')
                    break
                fi
            fi
        done
        echo "ATTR|cardIndex|$card_index"

        # Find renderIndex by matching PCI address in /sys/class/drm/renderDX/device symlink
        render_index=""
        for drm_render in /sys/class/drm/renderD*; do
            if [ -L "$drm_render/device" ]; then
                render_pci=$(readlink -f "$drm_render/device" 2>/dev/null || echo "")
                if [[ "$render_pci" == *"$pci_addr"* ]]; then
                    render_index=$(basename "$drm_render" | sed 's/renderD//')
                    break
                fi
            fi
        done
        echo "ATTR|renderIndex|$render_index"

        echo "GPU_END|$pci_addr"
    done
    """

    # Execute the script once
    Logger.info(f"Executing sysfs collection on node {node_name}")
    cmd = ["bash", "-c", script]
    result = k8_util.run_command_on_node(k8_cluster, node_name, cmd)

    if result is None:
        Logger.error(
            f"run_command_on_node returned None for node {node_name} (all retries failed)"
        )
        return sysfs_info

    ret_code, output = result

    if ret_code != 0:
        Logger.error(
            f"Failed to collect sysfs data on node {node_name}: exit code {ret_code}"
        )
        Logger.debug(f"Output: {output}")
        return sysfs_info

    if not output:
        Logger.info(
            f"Node {node_name} does not have any AMD GPU devices in amdgpu driver"
        )
        return sysfs_info

    # Parse the output
    Logger.info(f"Parsing sysfs data from node {node_name}")
    current_gpu = None
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        if line.startswith("GPU_START|"):
            pci_addr = line.split("|", 1)[1]
            current_gpu = {"pci_address": pci_addr}
            Logger.debug(f"Processing GPU: {pci_addr}")

        elif line.startswith("ATTR|") and current_gpu is not None:
            parts = line.split("|", 2)
            if len(parts) == 3:
                attr_name = parts[1]
                attr_value = parts[2].strip() if parts[2] else None
                current_gpu[attr_name] = attr_value

        elif line.startswith("GPU_END|") and current_gpu is not None:
            pci_addr = current_gpu["pci_address"]
            sysfs_info["gpus"][pci_addr] = current_gpu
            current_gpu = None

        elif line.startswith("ERROR:"):
            Logger.warning(f"Node {node_name}: {line}")

    Logger.info(
        f"Node {node_name}: Collected sysfs info for {len(sysfs_info['gpus'])} GPU(s)"
    )

    return sysfs_info


def collect_gpu_hardware_info(k8_cluster, node_name: str) -> Dict:
    """
    Collect comprehensive GPU hardware information from both lspci and sysfs.

    Combines data from:
    1. lspci - PCI device detection, device IDs, GPU series
    2. sysfs - Partition configuration, product info, versions

    This is the main method that should be used by tests. It provides complete
    GPU hardware information by merging both data sources based on PCI address.

    Args:
        k8_cluster: K8 cluster object
        node_name: Name of the node

    Returns:
        Dict with merged hardware information:
        {
            "gpus": [  # merged list of GPUs
                {
                    "pci_address": "06:00.0",  # short format from lspci
                    "pci_address_full": "0000:06:00.0",  # long format from sysfs
                    "device_id": "74a1",  # from lspci
                    "gpu_series": "MI300X",  # from lspci
                    "current_compute_partition": "SPX",  # from sysfs
                    "current_memory_partition": "NPS1",  # from sysfs
                    "available_compute_partition": "SPX, DPX, QPX, CPX",  # from sysfs
                    "available_memory_partition": "NPS1, NPS2",  # from sysfs
                    "product_name": "...",  # from sysfs
                    ...
                }
            ]
        }

        Use len(result["gpus"]) to get GPU count.
        Use [gpu["device_id"] for gpu in result["gpus"]] to get device IDs.
    """
    # Collect from lspci
    lspci_info = collect_gpu_hardware_info_by_lspci(k8_cluster, node_name)

    # Collect from sysfs
    sysfs_info = collect_gpu_hardware_info_by_sysfs(k8_cluster, node_name)

    # Merge data based on PCI address
    merged_gpus = []

    for lspci_gpu in lspci_info.get("gpus", []):
        # Get short PCI address from lspci (e.g., "06:00.0")
        short_pci = lspci_gpu.get("pci_address", "")

        # Convert short format to long format for sysfs lookup
        # Short: "06:00.0" -> Long: "0000:06:00.0"
        # Short: "0000:06:00.0" -> Long: "0000:06:00.0" (already long)
        if short_pci:
            if short_pci.count(":") == 1:
                # Short format, add domain prefix
                long_pci = f"0000:{short_pci}"
            else:
                # Already long format
                long_pci = short_pci
        else:
            long_pci = None

        # Start with lspci data
        merged_gpu = lspci_gpu.copy()

        # Merge sysfs data if available
        if long_pci and long_pci in sysfs_info.get("gpus", {}):
            sysfs_gpu = sysfs_info["gpus"][long_pci]
            merged_gpu["pci_address_full"] = long_pci

            # Add all sysfs attributes except pci_address (keep short format from lspci)
            for key, value in sysfs_gpu.items():
                if key != "pci_address":
                    merged_gpu[key] = value

            Logger.debug(
                f"Merged data for PCI {short_pci}: lspci + sysfs ({len(sysfs_gpu)} sysfs attrs)"
            )
        else:
            Logger.debug(
                f"No sysfs data found for PCI address {short_pci} (tried {long_pci})"
            )

        merged_gpus.append(merged_gpu)

    # Return merged results
    combined_info = {
        "gpus": merged_gpus,
    }

    Logger.info(
        f"Node {node_name}: Combined hardware info - {len(combined_info['gpus'])} GPU(s) with merged lspci+sysfs data"
    )

    return combined_info


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
    if len(hw_info["gpus"]) > 0:
        # Use first GPU's info (same as gather_device_info)
        first_gpu = hw_info["gpus"][0]
        cluster_node.device_id = first_gpu["device_id"]
        cluster_node.gpu_series = first_gpu["gpu_series"]
        cluster_node.num_gpus = len(hw_info["gpus"])
    else:
        cluster_node.device_id = None
        cluster_node.gpu_series = None
        cluster_node.num_gpus = 0


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
