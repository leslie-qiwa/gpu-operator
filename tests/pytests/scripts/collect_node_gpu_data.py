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
1. Hardware info (lspci)
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


def run_kubectl_command(cmd: List[str]) -> Tuple[int, str, str]:
    """
    Run a kubectl command and return exit code, stdout, stderr.

    Args:
        cmd: Command as list of strings

    Returns:
        Tuple of (exit_code, stdout, stderr)
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out after 300 seconds"
    except Exception as e:
        return -1, "", str(e)


def collect_hardware_info(k8_cluster, node_name: str) -> Dict:
    """
    Collect GPU hardware information using lspci.

    Uses shared helper from lib.node_gpu_collector - same logic as
    gather_device_info fixture!

    Args:
        k8_cluster: K8 cluster object
        node_name: Name of the node

    Returns:
        Dict with hardware information
    """
    print(f"[1/4] Collecting hardware info (lspci)...", file=sys.stderr)

    # Use shared collector function (same as gather_device_info fixture!)
    hw_info = node_collector.collect_gpu_hardware_info(k8_cluster, node_name)

    # Reformat for script output
    hardware_info = {
        "method": "lspci",
        "gpus": hw_info.get("gpus", []),
        "total_gpus": hw_info.get("total_gpus", 0),
        "device_ids": hw_info.get("device_ids", []),
        "error": None
    }

    print(f"  Found {hardware_info['total_gpus']} AMD GPU(s)", file=sys.stderr)
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
    print(f"[2/4] Collecting ROCm info (rocm-smi, amd-smi)...", file=sys.stderr)

    rocm_info = {
        "rocm_smi": {
            "available": False,
            "output": None,
            "error": None
        },
        "amd_smi": {
            "available": False,
            "output": None,
            "error": None
        }
    }

    # Try rocm-smi using existing utility
    cmd = ["rocm-smi"]
    ret_code, output = k8_util.run_command_on_node(k8_cluster, node_name, cmd)
    if ret_code == 0:
        rocm_info["rocm_smi"]["available"] = True
        rocm_info["rocm_smi"]["output"] = output
        print(f"  rocm-smi: available", file=sys.stderr)
    else:
        rocm_info["rocm_smi"]["error"] = output
        print(f"  rocm-smi: not available", file=sys.stderr)

    # Try amd-smi static
    cmd = ["amd-smi", "static"]
    ret_code, output = k8_util.run_command_on_node(k8_cluster, node_name, cmd)
    if ret_code == 0:
        rocm_info["amd_smi"]["available"] = True
        rocm_info["amd_smi"]["output"] = output
        print(f"  amd-smi: available", file=sys.stderr)
    else:
        rocm_info["amd_smi"]["error"] = output
        print(f"  amd-smi: not available", file=sys.stderr)

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
    print(f"[3/4] Collecting partition info (sysfs)...", file=sys.stderr)

    partition_info = {
        "sysfs_files": [],
        "partition_profiles": [],
        "error": None
    }

    # Find all partition-related sysfs files
    cmd = ["find", "/sys/class/drm", "-name", "partition_*", "-type", "f"]
    ret_code, output = k8_util.run_command_on_node(k8_cluster, node_name, cmd)

    if ret_code != 0:
        partition_info["error"] = f"Failed to search for partition files: {output}"
        print(f"  No partition files found", file=sys.stderr)
        return partition_info

    if not output.strip():
        print(f"  No partition files found", file=sys.stderr)
        return partition_info

    # Read each partition file
    partition_files = output.strip().split('\n')
    print(f"  Found {len(partition_files)} partition-related file(s)", file=sys.stderr)

    for part_file in partition_files:
        if part_file.strip():
            cmd = ["cat", part_file.strip()]
            ret_code, content = k8_util.run_command_on_node(k8_cluster, node_name, cmd)

            file_info = {
                "path": part_file.strip(),
                "content": content.strip() if ret_code == 0 else None,
                "error": content if ret_code != 0 else None
            }

            partition_info["sysfs_files"].append(file_info)

            # Extract profile if this is a partition_profile file
            if "partition_profile" in part_file and ret_code == 0:
                partition_info["partition_profiles"].append(content.strip())

    # Also use shared helper for quick profile collection
    profiles = node_collector.collect_gpu_partition_profiles(k8_cluster, node_name)
    if profiles and not partition_info["partition_profiles"]:
        partition_info["partition_profiles"] = profiles

    return partition_info


def collect_dra_devices(node_name: str) -> Dict:
    """
    Collect advertised GPU devices from ResourceSlices.

    Args:
        node_name: Name of the node

    Returns:
        Dict with DRA device information
    """
    print(f"[4/4] Collecting advertised devices (ResourceSlices)...", file=sys.stderr)

    dra_info = {
        "full_gpus": [],
        "partitions": [],
        "error": None
    }

    # Get ResourceSlices in JSON format
    kubectl_cmd = [
        "kubectl", "get", "resourceslices",
        "-o", "json"
    ]

    ret_code, stdout, stderr = run_kubectl_command(kubectl_cmd)

    if ret_code != 0:
        dra_info["error"] = f"Failed to get ResourceSlices: {stderr}"
        print(f"  Error getting ResourceSlices", file=sys.stderr)
        return dra_info

    try:
        resource_slices = json.loads(stdout)
    except json.JSONDecodeError as e:
        dra_info["error"] = f"Failed to parse ResourceSlices JSON: {e}"
        print(f"  Error parsing ResourceSlices", file=sys.stderr)
        return dra_info

    # Process ResourceSlices
    for slice_obj in resource_slices.get("items", []):
        # Only process AMD GPU driver slices
        spec = slice_obj.get("spec", {})
        if spec.get("driver") != "gpu.amd.com":
            continue

        # Check if this slice is for our node
        slice_node = spec.get("nodeName", "")
        if slice_node != node_name:
            continue

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
                "capacity": capacity
            }

            device_type = gpu_attrs.get("type", "")
            if device_type == "amdgpu":
                dra_info["full_gpus"].append(device_info)
            elif device_type == "amdgpu-partition":
                dra_info["partitions"].append(device_info)

    print(f"  Found {len(dra_info['full_gpus'])} full GPU(s)", file=sys.stderr)
    print(f"  Found {len(dra_info['partitions'])} partition(s)", file=sys.stderr)

    return dra_info


def get_node_info(node_name: str) -> Dict:
    """
    Get basic node information using kubectl.

    Args:
        node_name: Name of the node

    Returns:
        Dict with node information
    """
    node_info = {
        "name": node_name,
        "labels": {},
        "capacity": {},
        "allocatable": {},
        "os_info": {},
        "error": None
    }

    # Get node details
    kubectl_cmd = ["kubectl", "get", "node", node_name, "-o", "json"]
    ret_code, stdout, stderr = run_kubectl_command(kubectl_cmd)

    if ret_code != 0:
        node_info["error"] = f"Failed to get node info: {stderr}"
        return node_info

    try:
        node_data = json.loads(stdout)

        # Extract labels
        node_info["labels"] = node_data.get("metadata", {}).get("labels", {})

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

    except json.JSONDecodeError as e:
        node_info["error"] = f"Failed to parse node JSON: {e}"

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

  # Pretty print
  %(prog)s worker-node-1 --pretty

  # Only collect specific data
  %(prog)s worker-node-1 --skip-rocm --skip-partition
        """
    )

    parser.add_argument(
        "node_name",
        help="Name of the Kubernetes node"
    )

    parser.add_argument(
        "-o", "--output",
        help="Output file (default: stdout)",
        default=None
    )

    parser.add_argument(
        "--pretty",
        help="Pretty print JSON output",
        action="store_true"
    )

    parser.add_argument(
        "--skip-hardware",
        help="Skip hardware info collection (lspci)",
        action="store_true"
    )

    parser.add_argument(
        "--skip-rocm",
        help="Skip ROCm info collection",
        action="store_true"
    )

    parser.add_argument(
        "--skip-partition",
        help="Skip partition info collection",
        action="store_true"
    )

    parser.add_argument(
        "--skip-dra",
        help="Skip ResourceSlice collection",
        action="store_true"
    )

    parser.add_argument(
        "--kubeconfig",
        help="Path to kubeconfig file (default: ~/.kube/config)",
        default=os.path.expanduser("~/.kube/config")
    )

    args = parser.parse_args()

    # Initialize k8_util library with kubeconfig
    k8_util.k8_lib_init(args.kubeconfig)

    # Create a minimal k8_cluster object for run_command_on_node
    # The function requires a k8_cluster object with k8_registry attribute
    k8_cluster = common.k8_cluster([], [])
    k8_cluster.k8_kube_config = args.kubeconfig
    k8_cluster.k8_registry = "docker.io"  # Default registry for debug pods

    # Collect all data
    print(f"Collecting GPU data for node: {args.node_name}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    data = {
        "node": get_node_info(args.node_name),
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }

    if not args.skip_hardware:
        data["hardware"] = collect_hardware_info(k8_cluster, args.node_name)
    else:
        print("[1/4] Skipping hardware info", file=sys.stderr)

    if not args.skip_rocm:
        data["rocm"] = collect_rocm_info(k8_cluster, args.node_name)
    else:
        print("[2/4] Skipping ROCm info", file=sys.stderr)

    if not args.skip_partition:
        data["partition"] = collect_partition_info(k8_cluster, args.node_name)
    else:
        print("[3/4] Skipping partition info", file=sys.stderr)

    if not args.skip_dra:
        data["dra_advertised"] = collect_dra_devices(args.node_name)
    else:
        print("[4/4] Skipping ResourceSlice info", file=sys.stderr)

    # Add summary
    data["summary"] = {
        "hardware_gpu_count": len(data.get("hardware", {}).get("gpus", [])),
        "dra_full_gpu_count": len(data.get("dra_advertised", {}).get("full_gpus", [])),
        "dra_partition_count": len(data.get("dra_advertised", {}).get("partitions", [])),
        "partition_profiles": data.get("partition", {}).get("partition_profiles", []),
        "rocm_smi_available": data.get("rocm", {}).get("rocm_smi", {}).get("available", False),
        "amd_smi_available": data.get("rocm", {}).get("amd_smi", {}).get("available", False),
    }

    print("=" * 60, file=sys.stderr)
    print("Collection complete!", file=sys.stderr)
    print(f"\nSummary:", file=sys.stderr)
    print(f"  Hardware GPUs: {data['summary']['hardware_gpu_count']}", file=sys.stderr)
    print(f"  DRA Full GPUs: {data['summary']['dra_full_gpu_count']}", file=sys.stderr)
    print(f"  DRA Partitions: {data['summary']['dra_partition_count']}", file=sys.stderr)
    print(f"  Partition Profiles: {data['summary']['partition_profiles']}", file=sys.stderr)
    print(f"  rocm-smi: {'available' if data['summary']['rocm_smi_available'] else 'not available'}", file=sys.stderr)
    print(f"  amd-smi: {'available' if data['summary']['amd_smi_available'] else 'not available'}", file=sys.stderr)

    # Output JSON
    if args.pretty:
        json_output = json.dumps(data, indent=2)
    else:
        json_output = json.dumps(data)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(json_output)
        print(f"\nData saved to: {args.output}", file=sys.stderr)
    else:
        print("\n" + "=" * 60, file=sys.stderr)
        print(json_output)


if __name__ == "__main__":
    main()
