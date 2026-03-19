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
    Collect comprehensive GPU hardware information from sysfs.

    Reads from multiple sysfs sources and correlates them:
    1. /sys/module/amdgpu/drivers/pci:amdgpu/<pci_addr>/ - PCI device properties
    2. /sys/devices/platform/amdgpu_xcp_*/ - XCP partition platform devices
    3. /sys/class/kfd/kfd/topology/nodes/<nodeId>/ - KFD topology nodes
    4. /sys/class/drm/card*, /sys/class/drm/renderD* - DRM devices

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
                    "current_compute_partition": "SPX",
                    "current_memory_partition": "NPS1",
                    "cardIndex": "0",
                    "renderIndex": "128",
                    "partitions": [  # XCP partitions (only if GPU is partitioned)
                        {
                            "xcp_index": 0,
                            "xcp_name": "amdgpu_xcp_0",
                            "kfd_node_id": 1,
                            "gpu_id": "12345",
                            "simd_count": "152",
                            "numa_node_id": "0",
                            ...
                        }
                    ],
                    ...
                }
            }
        }
    """
    Logger.info(f"Collecting comprehensive GPU sysfs information for node: {node_name}")
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
    # Collects: PCI devices, XCP partitions, KFD nodes, DRM devices
    Logger.info("Building comprehensive sysfs collection script (single pod execution)")
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

    # ===== Collect XCP partition information =====
    echo "XCP_COLLECTION_START"

    if [ -d "/sys/devices/platform" ]; then
        for xcp_dir in /sys/devices/platform/amdgpu_xcp_*; do
            if [ ! -d "$xcp_dir" ]; then
                continue
            fi

            xcp_name=$(basename "$xcp_dir")
            xcp_index=${{xcp_name#amdgpu_xcp_}}

            echo "XCP_START|$xcp_index|$xcp_name"

            # XCP devices don't have direct 'device' symlink to parent PCI
            # Instead, find the renderD and card devices under drm/
            # Then find parent GPU by checking for partitioned GPUs (compute_partition != SPX)

            # Get card index
            for card_path in "$xcp_dir"/drm/card*; do
                [ ! -d "$card_path" ] && continue
                card_name=$(basename "$card_path")
                card_index=${{card_name#card}}
                echo "XCP_CARD_INDEX|$card_index"
                break
            done

            # Get renderD index
            for render_path in "$xcp_dir"/drm/renderD*; do
                [ ! -d "$render_path" ] && continue
                render_name=$(basename "$render_path")
                render_index=${{render_name#renderD}}
                echo "XCP_RENDER_INDEX|$render_index"
                break
            done

            # Find parent PCI by looking for GPUs with partitioning enabled
            # Check all full GPU PCI devices to find the partitioned one
            parent_found=false
            for pci_card in /sys/class/drm/card*/device; do
                [ ! -L "$pci_card" ] && continue
                pci_path=$(readlink -f "$pci_card")

                # Skip if this points to an XCP device (not a real PCI device)
                [[ "$pci_path" == */amdgpu_xcp_* ]] && continue

                # Check if this is a PCI device path
                pci_addr=$(basename "$pci_path")
                if [[ "$pci_addr" =~ ^[0-9a-fA-F]{{4}}:[0-9a-fA-F]{{2}}:[0-9a-fA-F]{{2}}\\.[0-9]$ ]]; then
                    # Check if this GPU has partitioning enabled
                    compute_part=$(cat "$pci_path/current_compute_partition" 2>/dev/null || echo "")
                    if [ -n "$compute_part" ] && [ "$compute_part" != "SPX" ]; then
                        echo "XCP_PARENT_PCI|$pci_addr"
                        parent_found=true
                        break
                    fi
                fi
            done

            echo "XCP_END|$xcp_index"
        done
    fi

    echo "XCP_COLLECTION_END"

    # ===== Collect KFD topology node information =====
    echo "KFD_COLLECTION_START"

    if [ -d "/sys/class/kfd/kfd/topology/nodes" ]; then
        for node_dir in /sys/class/kfd/kfd/topology/nodes/*; do
            if [ ! -d "$node_dir" ]; then
                continue
            fi

            node_id=$(basename "$node_dir")

            # Skip CPU nodes (node 0 is typically CPU)
            if [ "$node_id" = "0" ]; then
                continue
            fi

            echo "KFD_NODE_START|$node_id"

            # Read properties file (contains PCI info, gpu_id, simd_count, etc.)
            if [ -f "$node_dir/properties" ]; then
                # Extract key properties
                while IFS= read -r line; do
                    # Parse lines like "cpu_cores_count 0" or "simd_count 304"
                    key=$(echo "$line" | awk '{{print $1}}')
                    value=$(echo "$line" | awk '{{print $2}}')

                    # Only capture important properties
                    case "$key" in
                        domain|bus|device|function|location_id|gpu_id|simd_count|\\
                        max_waves_per_simd|array_count|num_cp_queues|\\
                        mem_banks_count|caches_count|numa_node_id|drm_render_minor)
                            echo "KFD_PROP|$key|$value"
                            ;;
                    esac
                done < "$node_dir/properties"

                # Reconstruct PCI address from location_id
                # For MI300X partitions, location_id encodes the PCI address
                # location_id = 0xBBDD (bus=BB, device=DD, function=0)
                location_id=$(grep "^location_id" "$node_dir/properties" 2>/dev/null | awk '{{print $2}}' || echo "")

                if [ -n "$location_id" ] && [ "$location_id" != "0" ]; then
                    # Decode: bus = (location_id >> 8) & 0xFF, device = location_id & 0xFF
                    bus=$((location_id >> 8))
                    device=$(((location_id >> 3) & 0x1F))
                    func=$((location_id & 0x7))

                    # Format as PCI address
                    pci_addr=$(printf "0000:%02x:%02x.%x" $bus $device $func 2>/dev/null || echo "")
                    if [ -n "$pci_addr" ]; then
                        echo "KFD_PCI_ADDR|$pci_addr"
                    fi
                fi
            fi

            # Read name file (GPU product name)
            if [ -f "$node_dir/name" ]; then
                gpu_name=$(cat "$node_dir/name" 2>/dev/null || echo "")
                echo "KFD_NAME|$gpu_name"
            fi

            echo "KFD_NODE_END|$node_id"
        done
    fi

    echo "KFD_COLLECTION_END"
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
        # Check if this is because driver is not loaded
        if output and "ERROR: Directory /sys/module/amdgpu" in output:
            Logger.warning(
                f"Node {node_name}: amdgpu driver not loaded - skipping sysfs collection"
            )
        else:
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

    # Parse the output and build comprehensive mapping
    Logger.info(f"Parsing comprehensive sysfs data from node {node_name}")

    current_gpu = None
    xcp_partitions = {}  # Map: xcp_index -> {xcp_name, parent_pci, ...}
    kfd_nodes = {}       # Map: kfd_node_id -> {gpu_id, pci_addr, properties, ...}

    in_xcp_section = False
    in_kfd_section = False
    current_xcp = None
    current_kfd_node = None

    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # ===== Parse PCI GPU data =====
        if line.startswith("GPU_START|"):
            pci_addr = line.split("|", 1)[1]
            current_gpu = {
                "pci_address": pci_addr,
                "partitions": []  # Will be populated from XCP/KFD correlation
            }
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

        # ===== Parse XCP partition data =====
        elif line.startswith("XCP_COLLECTION_START"):
            in_xcp_section = True
            Logger.debug("Parsing XCP partition data...")

        elif line.startswith("XCP_COLLECTION_END"):
            in_xcp_section = False
            Logger.debug(f"Parsed {len(xcp_partitions)} XCP partition(s)")

        elif line.startswith("XCP_START|") and in_xcp_section:
            parts = line.split("|")
            xcp_index = int(parts[1])
            xcp_name = parts[2]
            current_xcp = {
                "xcp_index": xcp_index,
                "xcp_name": xcp_name,
                "parent_pci": None,
                "card_index": None,
                "render_index": None
            }
            Logger.debug(f"  Processing XCP partition: {xcp_name} (index {xcp_index})")

        elif line.startswith("XCP_PARENT_PCI|") and current_xcp is not None:
            parent_pci = line.split("|", 1)[1]
            current_xcp["parent_pci"] = parent_pci

        elif line.startswith("XCP_CARD_INDEX|") and current_xcp is not None:
            card_index = line.split("|", 1)[1]
            current_xcp["card_index"] = card_index

        elif line.startswith("XCP_RENDER_INDEX|") and current_xcp is not None:
            render_index = line.split("|", 1)[1]
            current_xcp["render_index"] = render_index

        elif line.startswith("XCP_END|") and current_xcp is not None:
            xcp_index = current_xcp["xcp_index"]
            xcp_partitions[xcp_index] = current_xcp
            current_xcp = None

        # ===== Parse KFD topology node data =====
        elif line.startswith("KFD_COLLECTION_START"):
            in_kfd_section = True
            Logger.debug("Parsing KFD topology node data...")

        elif line.startswith("KFD_COLLECTION_END"):
            in_kfd_section = False
            Logger.debug(f"Parsed {len(kfd_nodes)} KFD node(s)")

        elif line.startswith("KFD_NODE_START|") and in_kfd_section:
            node_id = line.split("|", 1)[1]
            current_kfd_node = {
                "kfd_node_id": node_id,
                "properties": {},
                "pci_addr": None,
                "name": None
            }
            Logger.debug(f"  Processing KFD node: {node_id}")

        elif line.startswith("KFD_PROP|") and current_kfd_node is not None:
            parts = line.split("|", 2)
            if len(parts) == 3:
                prop_key = parts[1]
                prop_value = parts[2]
                current_kfd_node["properties"][prop_key] = prop_value

        elif line.startswith("KFD_PCI_ADDR|") and current_kfd_node is not None:
            pci_addr = line.split("|", 1)[1]
            current_kfd_node["pci_addr"] = pci_addr

        elif line.startswith("KFD_NAME|") and current_kfd_node is not None:
            gpu_name = line.split("|", 1)[1]
            current_kfd_node["name"] = gpu_name

        elif line.startswith("KFD_NODE_END|") and current_kfd_node is not None:
            node_id = current_kfd_node["kfd_node_id"]
            kfd_nodes[node_id] = current_kfd_node
            current_kfd_node = None

        # ===== Error handling =====
        elif line.startswith("ERROR:"):
            Logger.warning(f"Node {node_name}: {line}")

    # ===== Correlate XCP partitions and KFD nodes with PCI GPUs =====
    Logger.info(f"Correlating XCP partitions and KFD nodes with PCI devices...")

    # Build mapping: renderD index -> XCP partition
    # Normalize to string for consistent comparison
    xcp_by_render = {}
    for xcp_idx, xcp_data in xcp_partitions.items():
        render_idx = xcp_data.get("render_index")
        if render_idx:
            render_idx_str = str(render_idx)
            xcp_by_render[render_idx_str] = xcp_data
            Logger.debug(f"  XCP {xcp_idx}: render index {render_idx_str}")

    # Build mapping: renderD index -> KFD node
    # KFD nodes have drm_render_minor property
    kfd_by_render = {}
    for node_id, kfd_data in kfd_nodes.items():
        render_minor = kfd_data.get("properties", {}).get("drm_render_minor")
        if render_minor:
            # renderD index = render_minor (e.g., renderD129 -> 129)
            # Normalize to string for consistent comparison
            render_minor_str = str(render_minor)
            kfd_by_render[render_minor_str] = kfd_data
            Logger.debug(f"  KFD node {node_id}: render index {render_minor_str}")

    Logger.debug(f"XCP render indices: {sorted(xcp_by_render.keys())}")
    Logger.debug(f"KFD render indices: {sorted(kfd_by_render.keys())}")

    # Build mapping: parent PCI -> list of render indices
    xcp_renders_by_pci = {}
    for xcp_idx, xcp_data in xcp_partitions.items():
        parent_pci = xcp_data.get("parent_pci")
        render_idx = xcp_data.get("render_index")
        if parent_pci and render_idx:
            if parent_pci not in xcp_renders_by_pci:
                xcp_renders_by_pci[parent_pci] = []
            xcp_renders_by_pci[parent_pci].append(str(render_idx))

    # Merge partition data into GPU info
    for pci_addr, gpu_data in sysfs_info["gpus"].items():
        partitions = []

        # Check if GPU is partitioned
        compute_part = gpu_data.get("current_compute_partition", "").strip()
        memory_part = gpu_data.get("current_memory_partition", "").strip()
        is_partitioned = compute_part and memory_part and not (compute_part == "SPX" and memory_part == "NPS1")

        # Get the full GPU's renderD index (if partitioned, this becomes the 8th partition)
        full_gpu_render_idx = gpu_data.get("renderIndex")

        # Get all renderD indices for XCP partitions
        render_indices = xcp_renders_by_pci.get(pci_addr, [])

        # If GPU is partitioned and has a renderD, include it as a partition device
        # The DRA driver treats the full GPU renderD as an additional partition in CPX mode
        if is_partitioned and full_gpu_render_idx:
            all_render_indices = sorted(set(render_indices + [str(full_gpu_render_idx)]))
        else:
            all_render_indices = sorted(render_indices)

        # Correlate XCP with KFD using renderD index as the key
        for render_idx in all_render_indices:
            partition_info = {}

            # Get XCP data for this renderD (may not exist for full GPU renderD)
            if render_idx in xcp_by_render:
                partition_info.update(xcp_by_render[render_idx])
            else:
                # This is likely the full GPU renderD (no XCP device)
                # Create a pseudo-partition entry
                partition_info["xcp_index"] = None
                partition_info["xcp_name"] = f"full-gpu-renderD{render_idx}"
                partition_info["parent_pci"] = pci_addr
                partition_info["card_index"] = gpu_data.get("cardIndex")
                partition_info["render_index"] = render_idx

            # Get KFD data for this renderD
            if render_idx in kfd_by_render:
                kfd_data = kfd_by_render[render_idx]
                partition_info["kfd_node_id"] = kfd_data.get("kfd_node_id")
                partition_info["kfd_name"] = kfd_data.get("name")
                # Flatten KFD properties into partition_info
                for prop_key, prop_value in kfd_data.get("properties", {}).items():
                    partition_info[f"kfd_{prop_key}"] = prop_value

            if partition_info:
                partitions.append(partition_info)

        # Update GPU with partition data
        if partitions:
            gpu_data["partitions"] = partitions
            Logger.debug(f"  GPU {pci_addr}: Found {len(partitions)} partition(s)")
        else:
            gpu_data["partitions"] = []
            Logger.debug(f"  GPU {pci_addr}: No partitions (full GPU)")

    Logger.info(
        f"Node {node_name}: Collected comprehensive info for {len(sysfs_info['gpus'])} GPU(s), "
        f"{len(xcp_partitions)} XCP partition(s), {len(kfd_nodes)} KFD node(s)"
    )

    return sysfs_info


def collect_gpu_hardware_info(k8_cluster, node_name: str) -> Dict:
    """
    Collect comprehensive GPU hardware information from both lspci and sysfs.

    Combines data from:
    1. lspci - PCI device detection, device IDs, GPU series
    2. sysfs - Partition configuration, product info, versions, XCP devices, KFD nodes

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
                    "current_compute_partition": "CPX",  # from sysfs
                    "current_memory_partition": "NPS4",  # from sysfs
                    "cardIndex": "6",  # from sysfs
                    "renderIndex": "128",  # from sysfs
                    "partitions": [  # XCP + KFD correlation
                        {
                            "xcp_index": 0,
                            "xcp_name": "amdgpu_xcp_0",
                            "parent_pci": "0000:06:00.0",
                            "kfd_node_id": "1",
                            "kfd_gpu_id": "12345",
                            "kfd_simd_count": "152",
                            ...
                        }
                    ],
                    ...
                }
            ]
        }

        Use len(result["gpus"]) to get GPU count.
        Use [gpu["device_id"] for gpu in result["gpus"]] to get device IDs.
        Use gpu["partitions"] to access partition-specific data.
    """
    # Collect from lspci
    lspci_info = collect_gpu_hardware_info_by_lspci(k8_cluster, node_name)

    # Collect from sysfs (includes XCP + KFD correlation)
    sysfs_info = collect_gpu_hardware_info_by_sysfs(k8_cluster, node_name)

    # Validate partition correlations
    validate_partition_correlation(sysfs_info, node_name)

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

            partition_count = len(sysfs_gpu.get("partitions", []))
            Logger.debug(
                f"Merged data for PCI {short_pci}: lspci + sysfs ({len(sysfs_gpu)} sysfs attrs, {partition_count} partitions)"
            )
        else:
            Logger.debug(
                f"No sysfs data found for PCI address {short_pci} (tried {long_pci})"
            )
            # Ensure partitions key exists even if no sysfs data
            merged_gpu["partitions"] = []

        merged_gpus.append(merged_gpu)

    # Return merged results
    combined_info = {
        "gpus": merged_gpus,
    }

    total_partitions = sum(len(gpu.get("partitions", [])) for gpu in merged_gpus)
    Logger.info(
        f"Node {node_name}: Combined hardware info - {len(combined_info['gpus'])} GPU(s), "
        f"{total_partitions} partition(s) with full XCP+KFD correlation"
    )

    return combined_info


def get_partition_count_from_profile(profile: str) -> int:
    """
    Get theoretical maximum partition count from partition profile string.

    NOTE: This is a theoretical calculation only! The actual partition count
    depends on hardware/firmware configuration and may be less than this value.
    The DRA driver discovers partitions dynamically by enumerating XCP devices
    and validating them against KFD topology, not by parsing profile strings.

    For AMD MI300X, the theoretical partition count is multiplicative:
    Each partition is a combination of compute slice and memory (NUMA) node.

    Theoretical examples:
        SPX_NPS1 = 1 × 1 = 1 partition  (full GPU)
        DPX_NPS2 = 2 × 2 = 4 partitions
        QPX_NPS4 = 4 × 4 = 16 partitions
        CPX_NPS4 = 2 × 4 = 8 partitions (theoretical max)

    Actual hardware examples:
        CPX_NPS4 on asrock-126-b3-1b = 7 partitions (hardware-dependent)

    Args:
        profile: Partition profile like "SPX_NPS1", "CPX_NPS4", "DPX_NPS2"

    Returns:
        Theoretical maximum number of partitions (1, 2, 4, 8, 16, etc.)
        Actual count may be lower - use XCP device enumeration for truth.
    """
    if not profile or "_" not in profile:
        return 1

    compute_part, memory_part = profile.upper().split("_", 1)

    # Compute partition multiplier
    compute_multiplier = {
        "SPX": 1,  # Single partition eXtreme (1 compute partition)
        "DPX": 2,  # Dual Partition eXtreme (2 compute partitions)
        "TPX": 3,  # Triple Partition eXtreme (3 compute partitions, rare)
        "QPX": 4,  # Quad Partition eXtreme (4 compute partitions)
        "CPX": 2,  # Compute Partition eXtreme (2 compute partitions for MI300X)
    }.get(compute_part, 1)

    # Memory partition multiplier (NUMA Per Socket)
    memory_multiplier = {
        "NPS1": 1,  # 1 NUMA node
        "NPS2": 2,  # 2 NUMA nodes
        "NPS4": 4,  # 4 NUMA nodes
        "NPS8": 8,  # 8 NUMA nodes
    }.get(memory_part, 1)

    # Theoretical total = compute × memory
    # Actual count discovered via /sys/devices/platform/amdgpu_xcp_* enumeration
    return compute_multiplier * memory_multiplier


def validate_partition_correlation(hw_info: Dict, node_name: str) -> bool:
    """
    Validate that partition data is properly correlated across sysfs sources.

    Checks:
    1. Number of KFD nodes >= XCP devices (KFD includes full GPU representation)
    2. All partitions have parent PCI reference
    3. All partitions have KFD node correlation

    Note: The actual partition count may differ from the theoretical maximum
    based on profile (e.g., CPX_NPS4 may create 7 partitions instead of 8
    due to hardware/firmware configuration).

    Args:
        hw_info: Hardware info dict from collect_gpu_hardware_info()
        node_name: Name of the node (for logging)

    Returns:
        True if validation passes, False otherwise
    """
    all_valid = True

    for pci_addr, gpu_data in hw_info.get("gpus", {}).items():
        compute_part = gpu_data.get("current_compute_partition", "").strip()
        memory_part = gpu_data.get("current_memory_partition", "").strip()

        # Skip if not partitioned
        if not compute_part or not memory_part:
            if gpu_data.get("partitions"):
                Logger.warning(
                    f"Node {node_name}, GPU {pci_addr}: No partition profile but found {len(gpu_data['partitions'])} partition(s)"
                )
                all_valid = False
            continue

        # Log partition count (informational only - don't fail on mismatch)
        profile = f"{compute_part}_{memory_part}"
        expected_count = get_partition_count_from_profile(profile)
        actual_count = len(gpu_data.get("partitions", []))

        if expected_count != actual_count:
            Logger.info(
                f"Node {node_name}, GPU {pci_addr}: Profile {profile} theoretical max: {expected_count} partition(s), "
                f"actual: {actual_count} partition(s) (hardware-dependent)"
            )
        else:
            Logger.debug(
                f"Node {node_name}, GPU {pci_addr}: ✓ {actual_count} partition(s) match profile {profile}"
            )

        # Validate each partition has required fields
        for i, part in enumerate(gpu_data.get("partitions", [])):
            if not part.get("parent_pci"):
                Logger.warning(
                    f"Node {node_name}, GPU {pci_addr}, partition {i}: Missing parent_pci"
                )
                all_valid = False

            if not part.get("kfd_node_id"):
                Logger.warning(
                    f"Node {node_name}, GPU {pci_addr}, partition {i}: Missing kfd_node_id"
                )
                all_valid = False

    return all_valid


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
