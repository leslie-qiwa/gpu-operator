#!/usr/bin/env python3

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
Standalone script to collect GPU data from a Kubernetes node.

Collects:
1. Hardware info (lspci/sysfs)
2. ROCm/AMD SMI info
3. Partition info from sysfs
4. ResourceSlices information

Usage:
    python3 collect_node_gpu_data.py <node-name> [options]

Example:
    python3 collect_node_gpu_data.py worker-node-1
    python3 collect_node_gpu_data.py worker-node-1 --output node-data.json
    python3 collect_node_gpu_data.py worker-node-1 --pretty
"""

import argparse
import json
import subprocess
import sys
import os
import re
import time
import logging
from typing import Dict, List, Tuple

# Add parent directory to Python path to import lib modules
script_dir = os.path.dirname(os.path.abspath(__file__))
pytests_dir = os.path.dirname(script_dir)
sys.path.insert(0, pytests_dir)

# Import existing library modules
import lib.k8_util as k8_util
import lib.common as common
import lib.amdgpu as amdgpu_util
import lib.node_gpu_collector as node_collector

# Module logger
logger = logging.getLogger(__name__)


def run_kubectl_command(cmd: List[str]) -> Tuple[int, str, str]:
    """
    Run a kubectl command and return exit code, stdout, stderr.

    Args:
        cmd: Command as list of strings

    Returns:
        Tuple of (exit_code, stdout, stderr)
    """
    logger.debug(f"Running kubectl command: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        logger.debug(f"Command exit code: {result.returncode}")
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after 300 seconds: {' '.join(cmd)}")
        return -1, "", "Command timed out after 300 seconds"
    except Exception as e:
        logger.error(f"Command failed with exception: {e}")
        return -1, "", str(e)


def collect_hardware_info(k8_cluster, node_name: str) -> Dict:
    """
    Collect GPU hardware information using lspci/sysfs.

    Uses shared helper from lib.node_gpu_collector - same logic as
    gather_device_info fixture!

    Args:
        k8_cluster: K8 cluster object
        node_name: Name of the node

    Returns:
        Dict with hardware information
    """
    logger.info("[1/4] Collecting hardware info (lspci/sysfs)...")
    logger.debug(
        f"Using node_collector.collect_gpu_hardware_info for node: {node_name}"
    )

    # Use shared collector function (same as gather_device_info fixture!)
    hw_info = node_collector.collect_gpu_hardware_info(k8_cluster, node_name)
    logger.debug(f"Raw hardware info: {hw_info}")

    # Reformat for script output
    gpus = hw_info.get("gpus", [])
    hardware_info = {
        "method": "lspci/sysfs",
        "gpus": gpus,
        "total_gpus": len(gpus),
        "device_ids": list(set(gpu["device_id"] for gpu in gpus)) if gpus else [],
        "error": None,
    }

    logger.info(f"  Found {hardware_info['total_gpus']} AMD GPU(s)")
    if hardware_info["total_gpus"] > 0:
        logger.debug(f"  Device IDs: {hardware_info['device_ids']}")
    return hardware_info


def collect_rocm_info(k8_cluster, node_name: str) -> Dict:
    """
    Collect ROCm/AMD SMI information.
    Uses existing k8_util.run_command_on_node() function.

    Args:
        k8_cluster: K8 cluster object
        node_name: Name of the node

    Returns:
        Dict with ROCm information
    """
    logger.info("[2/4] Collecting ROCm info (rocm-smi, amd-smi)...")

    rocm_info = {
        "rocm_smi": {"available": False, "output": None, "error": None},
        "amd_smi": {"available": False, "output": None, "error": None},
    }

    # Try rocm-smi using existing utility
    logger.debug(f"Running rocm-smi on node {node_name}")
    cmd = ["rocm-smi"]
    ret_code, output = k8_util.run_command_on_node(k8_cluster, node_name, cmd)
    if ret_code == 0:
        rocm_info["rocm_smi"]["available"] = True
        rocm_info["rocm_smi"]["output"] = output
        logger.info("  rocm-smi: available")
        logger.debug(f"  rocm-smi output length: {len(output)} chars")
    else:
        rocm_info["rocm_smi"]["error"] = output
        logger.info("  rocm-smi: not available")
        logger.debug(f"  rocm-smi error: {output[:200] if output else 'none'}")

    # Try amd-smi static
    logger.debug(f"Running amd-smi static on node {node_name}")
    cmd = ["amd-smi", "static"]
    ret_code, output = k8_util.run_command_on_node(k8_cluster, node_name, cmd)
    if ret_code == 0:
        rocm_info["amd_smi"]["available"] = True
        rocm_info["amd_smi"]["output"] = output
        logger.info("  amd-smi: available")
        logger.debug(f"  amd-smi output length: {len(output)} chars")
    else:
        rocm_info["amd_smi"]["error"] = output
        logger.info("  amd-smi: not available")
        logger.debug(f"  amd-smi error: {output[:200] if output else 'none'}")

    return rocm_info


def collect_partition_info(k8_cluster, node_name: str) -> Dict:
    """
    Collect GPU partition information from sysfs.

    Uses shared helper from lib.node_gpu_collector.

    Args:
        k8_cluster: K8 cluster object
        node_name: Name of the node

    Returns:
        Dict with partition information
    """
    logger.info("[3/4] Collecting partition info (sysfs)...")

    partition_info = {"sysfs_files": [], "partition_profiles": [], "error": None}

    # Find all partition-related sysfs files
    logger.debug(f"Searching for partition files in /sys/class/drm on node {node_name}")
    cmd = ["find", "/sys/class/drm", "-name", "partition_*", "-type", "f"]
    ret_code, output = k8_util.run_command_on_node(k8_cluster, node_name, cmd)

    if ret_code != 0:
        partition_info["error"] = f"Failed to search for partition files: {output}"
        logger.info("  No partition files found (search failed)")
        logger.debug(f"  Error: {output}")
        return partition_info

    if not output.strip():
        logger.info("  No partition files found")
        return partition_info

    # Read each partition file
    partition_files = output.strip().split("\n")
    logger.info(f"  Found {len(partition_files)} partition-related file(s)")
    logger.debug(f"  Partition files: {partition_files}")

    for part_file in partition_files:
        if part_file.strip():
            logger.debug(f"  Reading partition file: {part_file.strip()}")
            cmd = ["cat", part_file.strip()]
            ret_code, content = k8_util.run_command_on_node(k8_cluster, node_name, cmd)

            file_info = {
                "path": part_file.strip(),
                "content": content.strip() if ret_code == 0 else None,
                "error": content if ret_code != 0 else None,
            }

            partition_info["sysfs_files"].append(file_info)

            # Extract profile if this is a partition_profile file
            if "partition_profile" in part_file and ret_code == 0:
                partition_info["partition_profiles"].append(content.strip())
                logger.debug(f"    Profile: {content.strip()}")

    return partition_info


def collect_dra_devices(node_name: str) -> Dict:
    """
    Collect advertised GPU devices from ResourceSlices.

    Args:
        node_name: Name of the node

    Returns:
        Dict with DRA device information
    """
    logger.info("[4/4] Collecting advertised devices (ResourceSlices)...")

    dra_info = {"full_gpus": [], "partitions": [], "error": None}

    # Get ResourceSlices in JSON format
    logger.debug("Fetching ResourceSlices from Kubernetes API")
    kubectl_cmd = ["kubectl", "get", "resourceslices", "-o", "json"]

    ret_code, stdout, stderr = run_kubectl_command(kubectl_cmd)

    if ret_code != 0:
        dra_info["error"] = f"Failed to get ResourceSlices: {stderr}"
        logger.warning("  Error getting ResourceSlices")
        logger.debug(f"  kubectl error: {stderr}")
        return dra_info

    try:
        resource_slices = json.loads(stdout)
        logger.debug(f"  Parsed {len(resource_slices.get('items', []))} ResourceSlices")
    except json.JSONDecodeError as e:
        dra_info["error"] = f"Failed to parse ResourceSlices JSON: {e}"
        logger.error("  Error parsing ResourceSlices")
        logger.debug(f"  JSON decode error: {e}")
        return dra_info

    # Process ResourceSlices
    amd_slices = 0
    node_slices = 0
    for slice_obj in resource_slices.get("items", []):
        # Only process AMD GPU driver slices
        spec = slice_obj.get("spec", {})
        if spec.get("driver") != "gpu.amd.com":
            continue
        amd_slices += 1

        # Check if this slice is for our node
        slice_node = spec.get("nodeName", "")
        if slice_node != node_name:
            continue
        node_slices += 1

        # Extract devices
        for device in spec.get("devices", []):
            device_name = device.get("name", "")
            basic = device.get("basic", {})
            attributes = basic.get("attributes", {})
            gpu_attrs = attributes.get("gpu.amd.com", {})
            capacity = basic.get("capacity", {})

            device_info = {
                "name": device_name,
                "type": gpu_attrs.get("type"),
                "attributes": gpu_attrs,
                "capacity": capacity,
            }

            device_type = gpu_attrs.get("type", "")
            if device_type == "amdgpu":
                dra_info["full_gpus"].append(device_info)
                logger.debug(f"    Found full GPU: {device_name}")
            elif device_type == "amdgpu-partition":
                dra_info["partitions"].append(device_info)
                logger.debug(f"    Found partition: {device_name}")

    logger.debug(
        f"  Total AMD ResourceSlices: {amd_slices}, for this node: {node_slices}"
    )
    logger.info(f"  Found {len(dra_info['full_gpus'])} full GPU(s)")
    logger.info(f"  Found {len(dra_info['partitions'])} partition(s)")

    return dra_info


def print_partition_mapping(hw_info, node_name):
    """Pretty print the partition mapping to stderr (so it doesn't interfere with JSON output)"""
    import sys

    def eprint(*args, **kwargs):
        """Print to stderr"""
        print(*args, file=sys.stderr, **kwargs)

    eprint("\n" + "=" * 80)
    eprint(f"GPU Partition Mapping for Node: {node_name}")
    eprint("=" * 80)

    for gpu in hw_info.get("gpus", []):
        pci_addr = gpu.get("pci_address_full", gpu.get("pci_address", "unknown"))
        eprint(f"\n📍 GPU at PCI {pci_addr}")
        eprint(f"   Device ID: {gpu.get('device_id', 'N/A')}")
        eprint(f"   GPU Series: {gpu.get('gpu_series', 'N/A')}")
        eprint(f"   Product: {gpu.get('product_name', 'N/A')}")
        eprint(f"   Card Index: {gpu.get('cardIndex', 'N/A')}")
        eprint(f"   Render Index: {gpu.get('renderIndex', 'N/A')}")

        compute_part = gpu.get("current_compute_partition", "").strip()
        memory_part = gpu.get("current_memory_partition", "").strip()

        if compute_part and memory_part:
            profile = f"{compute_part}_{memory_part}"
            eprint(f"   Partition Profile: {profile}")
        else:
            eprint(f"   Partition Profile: None (full GPU)")

        partitions = gpu.get("partitions", [])
        if partitions:
            eprint(f"\n   Partitions ({len(partitions)}):")
            for i, part in enumerate(partitions, 1):
                eprint(f"\n   [{i}] Partition Details:")
                eprint(f"       XCP Index: {part.get('xcp_index', 'N/A')}")
                eprint(f"       XCP Name: {part.get('xcp_name', 'N/A')}")
                eprint(f"       Parent PCI: {part.get('parent_pci', 'N/A')}")
                eprint(f"       KFD Node ID: {part.get('kfd_node_id', 'N/A')}")
                eprint(f"       KFD GPU ID: {part.get('kfd_gpu_id', 'N/A')}")
                eprint(f"       SIMD Count: {part.get('kfd_simd_count', 'N/A')}")
                eprint(f"       NUMA Node: {part.get('kfd_numa_node_id', 'N/A')}")
                eprint(f"       Memory Banks: {part.get('kfd_mem_banks_count', 'N/A')}")
        else:
            eprint(f"\n   No partitions (operating as full GPU)")

        eprint()

    eprint("=" * 80)


def get_node_info(node_name: str) -> Dict:
    """
    Get basic node information using kubectl.

    Args:
        node_name: Name of the node

    Returns:
        Dict with node information
    """
    logger.debug(f"Fetching node information for: {node_name}")
    node_info = {
        "name": node_name,
        "labels": {},
        "capacity": {},
        "allocatable": {},
        "os_info": {},
        "error": None,
    }

    # Get node details
    kubectl_cmd = ["kubectl", "get", "node", node_name, "-o", "json"]
    ret_code, stdout, stderr = run_kubectl_command(kubectl_cmd)

    if ret_code != 0:
        node_info["error"] = f"Failed to get node info: {stderr}"
        logger.error(f"Failed to get node info: {stderr}")
        return node_info

    try:
        node_data = json.loads(stdout)

        # Extract labels
        node_info["labels"] = node_data.get("metadata", {}).get("labels", {})
        logger.debug(f"  Node has {len(node_info['labels'])} labels")

        # Extract capacity and allocatable
        status = node_data.get("status", {})
        node_info["capacity"] = status.get("capacity", {})
        node_info["allocatable"] = status.get("allocatable", {})

        # Extract OS info
        node_info_data = status.get("nodeInfo", {})
        node_info["os_info"] = {
            "os_image": node_info_data.get("osImage", ""),
            "kernel_version": node_info_data.get("kernelVersion", ""),
            "kubelet_version": node_info_data.get("kubeletVersion", ""),
        }
        logger.debug(f"  OS: {node_info['os_info']['os_image']}")
        logger.debug(f"  Kernel: {node_info['os_info']['kernel_version']}")

    except json.JSONDecodeError as e:
        node_info["error"] = f"Failed to parse node JSON: {e}"
        logger.error(f"Failed to parse node JSON: {e}")

    return node_info


def main():
    parser = argparse.ArgumentParser(
        description="Collect GPU data from a Kubernetes node",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Collect data for a node
  %(prog)s worker-node-1

  # Save to file
  %(prog)s worker-node-1 --output node-data.json

  # Pretty print JSON
  %(prog)s worker-node-1 --pretty

  # Show detailed partition mapping (XCP + KFD correlation)
  %(prog)s worker-node-1 --show-partition-mapping --output node-data.json

  # Only collect specific data
  %(prog)s worker-node-1 --skip-rocm --skip-partition
        """,
    )

    parser.add_argument("node_name", help="Name of the Kubernetes node")

    parser.add_argument(
        "-o", "--output", help="Output file (default: stdout)", default=None
    )

    parser.add_argument(
        "--pretty", help="Pretty print JSON output", action="store_true"
    )

    parser.add_argument(
        "--skip-hardware",
        help="Skip hardware info collection (lspci/sysfs)",
        action="store_true",
    )

    parser.add_argument(
        "--skip-rocm", help="Skip ROCm info collection", action="store_true"
    )

    parser.add_argument(
        "--skip-partition", help="Skip partition info collection", action="store_true"
    )

    parser.add_argument(
        "--skip-dra", help="Skip ResourceSlice collection", action="store_true"
    )

    parser.add_argument(
        "--kubeconfig",
        help="Path to kubeconfig file (default: ~/.kube/config)",
        default=os.path.expanduser("~/.kube/config"),
    )

    parser.add_argument(
        "--log-level",
        help="Set logging level (DEBUG, INFO, WARNING, ERROR)",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )

    parser.add_argument(
        "--debug",
        help="Enable debug logging (equivalent to --log-level DEBUG)",
        action="store_true",
    )

    parser.add_argument(
        "--show-partition-mapping",
        help="Display detailed partition mapping (XCP, KFD correlation)",
        action="store_true",
    )

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.debug else getattr(logging, args.log_level)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Initialize k8_util library with kubeconfig
    logger.info(f"Initializing k8_util with kubeconfig: {args.kubeconfig}")
    k8_util.k8_lib_init(args.kubeconfig)

    # Create a minimal k8_cluster object for run_command_on_node
    # The function requires a k8_cluster object with k8_registry attribute
    k8_cluster = common.k8_cluster([], [])
    k8_cluster.k8_kube_config = args.kubeconfig
    k8_cluster.k8_registry = "docker.io"  # Default registry for debug pods
    logger.debug(f"Using registry: {k8_cluster.k8_registry}")

    # Collect all data
    logger.info(f"Collecting GPU data for node: {args.node_name}")
    logger.info("=" * 60)

    logger.debug("Collecting node information")
    data = {
        "node": get_node_info(args.node_name),
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }

    if not args.skip_hardware:
        data["hardware"] = collect_hardware_info(k8_cluster, args.node_name)

        # Show partition mapping if requested
        if args.show_partition_mapping:
            print_partition_mapping(data["hardware"], args.node_name)
    else:
        logger.info("[1/4] Skipping hardware info")

    if not args.skip_rocm:
        data["rocm"] = collect_rocm_info(k8_cluster, args.node_name)
    else:
        logger.info("[2/4] Skipping ROCm info")

    if not args.skip_partition:
        data["partition"] = collect_partition_info(k8_cluster, args.node_name)
    else:
        logger.info("[3/4] Skipping partition info")

    if not args.skip_dra:
        data["dra_advertised"] = collect_dra_devices(args.node_name)
    else:
        logger.info("[4/4] Skipping ResourceSlice info")

    # Add summary
    logger.debug("Building summary")

    # Calculate hardware partition counts from new XCP/KFD correlation
    hw_partition_count = 0
    xcp_device_count = 0
    kfd_node_count = 0
    for gpu in data.get("hardware", {}).get("gpus", []):
        partitions = gpu.get("partitions", [])
        hw_partition_count += len(partitions)
        xcp_device_count += sum(1 for p in partitions if p.get("xcp_name"))
        kfd_node_count += sum(1 for p in partitions if p.get("kfd_node_id"))

    data["summary"] = {
        "hardware_gpu_count": len(data.get("hardware", {}).get("gpus", [])),
        "hardware_partition_count": hw_partition_count,  # From XCP/KFD correlation
        "xcp_device_count": xcp_device_count,
        "kfd_node_count": kfd_node_count,
        "dra_full_gpu_count": len(data.get("dra_advertised", {}).get("full_gpus", [])),
        "dra_partition_count": len(
            data.get("dra_advertised", {}).get("partitions", [])
        ),
        "partition_profiles": data.get("partition", {}).get("partition_profiles", []),
        "rocm_smi_available": data.get("rocm", {})
        .get("rocm_smi", {})
        .get("available", False),
        "amd_smi_available": data.get("rocm", {})
        .get("amd_smi", {})
        .get("available", False),
    }

    logger.info("=" * 60)
    logger.info("Collection complete!")
    logger.info("")
    logger.info("Summary:")
    logger.info(f"  Hardware GPUs: {data['summary']['hardware_gpu_count']}")
    logger.info(f"  Hardware Partitions: {data['summary']['hardware_partition_count']} (XCP: {data['summary']['xcp_device_count']}, KFD: {data['summary']['kfd_node_count']})")
    logger.info(f"  DRA Full GPUs: {data['summary']['dra_full_gpu_count']}")
    logger.info(f"  DRA Partitions: {data['summary']['dra_partition_count']}")
    logger.info(f"  Partition Profiles: {data['summary']['partition_profiles']}")
    logger.info(
        f"  rocm-smi: {'available' if data['summary']['rocm_smi_available'] else 'not available'}"
    )
    logger.info(
        f"  amd-smi: {'available' if data['summary']['amd_smi_available'] else 'not available'}"
    )

    # Output JSON
    logger.debug(f"Generating JSON output (pretty={args.pretty})")
    if args.pretty:
        json_output = json.dumps(data, indent=2)
    else:
        json_output = json.dumps(data)

    if args.output:
        logger.debug(f"Writing output to file: {args.output}")
        with open(args.output, "w") as f:
            f.write(json_output)
        logger.info(f"\nData saved to: {args.output}")
    else:
        # When outputting to stdout, only print JSON (no logger output)
        # This allows piping the output to other tools
        print(json_output)


if __name__ == "__main__":
    main()
