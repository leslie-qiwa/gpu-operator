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
Test DRA driver device attributes.

This module combines two types of attribute tests:
1. Attribute structure/format validation (per DRA driver docs)
2. Attribute vs hardware validation (comparing with actual node data)

Based on: https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md
"""

import pytest
import time
import json
import logging
import re
import lib.k8_util as k8_util
import lib.amdgpu as amdgpu_util
import lib.node_gpu_collector as node_collector
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_dra_driver_attributes")

# Common validation constants
# Attributes shared by both full GPUs and partitions
COMMON_ATTRS = [
    "type",
    "cardIndex",
    "renderIndex",
    "family",
    "productName",
    "driverVersion",
    "driverSrcVersion",
]

# Required attributes for FULL GPUs
REQUIRED_FULL_GPU_ATTRS = COMMON_ATTRS + [
    "deviceID",
    "pciAddr",
]

# Required attributes for PARTITIONS
# Note: Partitions don't have deviceID or pciAddr in new format
# They use parentDeviceID and parentPciAddr instead
REQUIRED_PARTITION_ATTRS = COMMON_ATTRS + [
    "partitionProfile",
    "parentPciAddr",
    "parentDeviceID",
]

# Optional attributes that may be present for partitions
OPTIONAL_PARTITION_ATTRS = ["numaNode"]

# Critical attributes shared by both full GPUs and partitions
# Note: deviceID is critical for full GPUs but NOT present in partitions
CRITICAL_ATTRS = [
    "family",
    "productName",
    "driverVersion",
    "driverSrcVersion",
]

REQUIRED_CAPACITY_ATTRS = ["memory", "computeUnits", "simdUnits"]


@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    """Skip if not testing on K8s"""
    if environment.deployment_mode != "k8":
        pytest.skip(
            f"Skipping DRA driver attribute testcases for {environment.deployment_mode} deployment"
        )
    return


@pytest.fixture(scope="module")
def gpu_hardware_info(amdgpu_driver_install, gpu_cluster, environment):
    """
    Collect hardware information for all GPU nodes once.

    This fixture collects hardware data only once per test module,
    reducing the number of pod creations from 3N to N (where N = number of nodes).

    Depends on amdgpu_driver_install to ensure the driver is loaded before
    collecting hardware information.

    Returns:
        Dict mapping node_name -> {
            "hardware": {...},  # GPU hardware info from lspci + sysfs (includes partition data)
        }
    """
    Logger.info("=" * 70)
    Logger.info("Collecting hardware info for all GPU nodes (shared fixture)")
    Logger.info("=" * 70)

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get GPU nodes")

    hardware_data = {}
    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)
        Logger.info(f"Collecting hardware info for node: {node_name}")

        # Collect all hardware info in one batch (includes partition data in sysfs)
        hw_info = node_collector.collect_gpu_hardware_info(gpu_cluster, node_name)

        hardware_data[node_name] = {
            "hardware": hw_info,
        }

        Logger.info(
            f"  ✓ {node_name}: {len(hw_info['gpus'])} GPUs with partition info"
        )

    Logger.info("=" * 70)
    Logger.info(f"✓ Hardware collection complete for {len(hardware_data)} node(s)")
    Logger.info("=" * 70)

    return hardware_data


def get_resource_slices():
    """Get all ResourceSlices from the cluster

    kubectl equivalent: kubectl get resourceslices.resource.k8s.io -o yaml

    Returns:
        list: List of ResourceSlice objects
    """
    # Use existing k8_util helper
    ret_code, items, err = k8_util.k8_get_custom_resource_objects(
        group="resource.k8s.io", version="v1", plural="resourceslices"
    )

    if ret_code != 0:
        Logger.error(f"Failed to get ResourceSlices: {err}")
        return []

    return items if items else []


def get_amd_gpu_devices_from_slices(resource_slices=None, node_name=None):
    """Extract AMD GPU devices from ResourceSlices

    Args:
        resource_slices: Optional - List of ResourceSlice objects. If None, fetches them automatically
        node_name: Optional - filter by specific node name

    Returns:
        list: List of device dicts with fields: name, type, node_name, attributes, capacity
    """
    # Fetch ResourceSlices if not provided
    if resource_slices is None:
        resource_slices = get_resource_slices()

    amd_devices = []

    Logger.info(f"Processing {len(resource_slices)} ResourceSlice(s)")

    for idx, slice_obj in enumerate(resource_slices):
        slice_name = slice_obj.get("metadata", {}).get("name", "unknown")
        driver_name = slice_obj.get("spec", {}).get("driver", "")
        slice_node = slice_obj.get("spec", {}).get("nodeName", "unknown")

        Logger.debug(
            f"  Slice {idx}: name={slice_name}, driver={driver_name}, node={slice_node}"
        )

        if driver_name != "gpu.amd.com":
            Logger.debug(f"    Skipping - driver is '{driver_name}' (not gpu.amd.com)")
            continue

        # Filter by node if specified
        if node_name and slice_node != node_name:
            continue

        devices = slice_obj.get("spec", {}).get("devices")

        # Skip if devices is null (e.g., controller nodes without GPUs)
        if devices is None:
            Logger.warning(
                f"  Slice '{slice_name}' on node '{slice_node}': No devices (null) - this may indicate a DRA driver bug for non-partitioned GPUs"
            )
            # Dump the ResourceSlice for debugging
            import yaml
            Logger.debug(f"ResourceSlice YAML for debugging:\n{yaml.dump(slice_obj, default_flow_style=False)}")
            continue

        Logger.info(
            f"  Slice '{slice_name}' on node '{slice_node}': {len(devices)} device(s)"
        )

        for dev_idx, device in enumerate(devices):
            device_name = device.get("name", "unknown")
            Logger.debug(f"    Device {dev_idx}: {device_name}")

            # Normalize the device structure to extract typed values
            # Actual structure has attributes like: cardIndex: {int: 9}
            normalized_device = normalize_device_attributes(device)

            # Extract normalized values
            gpu_attrs = (
                normalized_device.get("basic", {})
                .get("attributes", {})
                .get("gpu.amd.com", {})
            )
            device_type = gpu_attrs.get("type", "unknown")
            Logger.debug(f"      Type: {device_type}")

            # Create simpler device info structure
            device_info = {
                "name": normalized_device.get("name", ""),
                "type": device_type,
                "node_name": slice_node,
                "attributes": gpu_attrs,
                "capacity": normalized_device.get("basic", {}).get("capacity", {}),
            }

            amd_devices.append(device_info)

    Logger.info(f"Extracted {len(amd_devices)} total AMD GPU device(s) from all slices")

    return amd_devices


def normalize_device_attributes(device):
    """Normalize ResourceSlice device attributes from typed values to simple dict

    ResourceSlice attributes have structure like:
      cardIndex: {int: 9}
      deviceID: {string: "12151357581094058033"}
      type: {string: "amdgpu"}

    This function extracts the actual values.

    Args:
        device: Device object from ResourceSlice

    Returns:
        Normalized device dict with simple attribute values
    """
    normalized = {
        "name": device.get("name", ""),
        "basic": {"attributes": {"gpu.amd.com": {}}, "capacity": {}},
    }

    # Extract attributes
    raw_attributes = device.get("attributes", {})
    gpu_attrs = {}

    for attr_name, attr_value in raw_attributes.items():
        # Extract the actual value from typed structure
        if isinstance(attr_value, dict):
            # Try different type keys
            value = (
                attr_value.get("string")
                or attr_value.get("int")
                or attr_value.get("version")
                or attr_value.get("bool")
            )
            if value is not None:
                gpu_attrs[attr_name] = value
        else:
            gpu_attrs[attr_name] = attr_value

    normalized["basic"]["attributes"]["gpu.amd.com"] = gpu_attrs

    # Extract capacity
    raw_capacity = device.get("capacity", {})
    capacity = {}

    for cap_name, cap_value in raw_capacity.items():
        # Capacity values have structure like: {value: "256"}
        if isinstance(cap_value, dict):
            value = cap_value.get("value")
            if value is not None:
                # Prefix with gpu.amd.com for consistency
                capacity[f"gpu.amd.com/{cap_name}"] = value
        else:
            capacity[f"gpu.amd.com/{cap_name}"] = cap_value

    normalized["basic"]["capacity"] = capacity

    return normalized


def validate_required_attributes_present(
    device_name, gpu_attrs, required_attrs, environment
):
    """Validate that all required attributes are present"""
    for attr in required_attrs:
        K8Helper.triage(
            environment,
            attr in gpu_attrs,
            f"Device {device_name}: Missing required attribute '{attr}'",
        )
        if attr in gpu_attrs:
            Logger.info(f"  ✓ {attr}: {gpu_attrs[attr]}")


def validate_device_type(device_name, gpu_attrs, expected_type, environment):
    """Validate device type matches expected value"""
    device_type = gpu_attrs.get("type", "")
    K8Helper.triage(
        environment,
        device_type == expected_type,
        f"Device {device_name}: Expected type='{expected_type}', got '{device_type}'",
    )


def validate_pci_address_format(device_name, gpu_attrs, environment):
    """Validate PCI address format

    Note: pciAddr may be null for partitions in new format.
    In that case, parentPciAddr should be present instead.
    """
    pci_addr = gpu_attrs.get("pciAddr", "")
    parent_pci_addr = gpu_attrs.get("parentPciAddr", "")

    # Either pciAddr or parentPciAddr should be present
    has_pci = pci_addr or parent_pci_addr
    K8Helper.triage(
        environment,
        has_pci,
        f"Device {device_name}: Neither pciAddr nor parentPciAddr is set",
    )

    # Validate format if pciAddr is present
    if pci_addr:
        pci_pattern = r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9]$"
        K8Helper.triage(
            environment,
            re.match(pci_pattern, pci_addr) is not None,
            f"Device {device_name}: Invalid pciAddr format '{pci_addr}'",
        )

    # Validate format if parentPciAddr is present
    if parent_pci_addr:
        pci_pattern = r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9]$"
        K8Helper.triage(
            environment,
            re.match(pci_pattern, parent_pci_addr) is not None,
            f"Device {device_name}: Invalid parentPciAddr format '{parent_pci_addr}'",
        )


def validate_critical_attributes_not_empty(
    device_name, gpu_attrs, critical_attrs, environment
):
    """Validate that critical attributes are not null or empty"""
    for attr in critical_attrs:
        attr_value = gpu_attrs.get(attr)
        K8Helper.triage(
            environment,
            attr_value is not None and attr_value != "",
            f"Device {device_name}: Critical attribute '{attr}' is null or empty",
        )


def validate_card_and_render_indices(device_name, gpu_attrs, environment):
    """Validate cardIndex and renderIndex are valid integers"""
    card_index = gpu_attrs.get("cardIndex")
    K8Helper.triage(
        environment,
        isinstance(card_index, int) and card_index >= 0,
        f"Device {device_name}: cardIndex must be a non-negative integer, got {card_index}",
    )

    render_index = gpu_attrs.get("renderIndex")
    K8Helper.triage(
        environment,
        isinstance(render_index, int) and render_index >= 128,
        f"Device {device_name}: renderIndex must be >= 128, got {render_index}",
    )

    return card_index, render_index


def validate_device_name_format(device_name, card_index, render_index, environment):
    """Validate device name matches format gpu-<cardIndex>-<renderIndex>"""
    expected_name = f"gpu-{card_index}-{render_index}"
    K8Helper.triage(
        environment,
        device_name == expected_name,
        f"Device name '{device_name}' doesn't match expected format '{expected_name}'",
    )


def validate_capacity_attributes_present(
    device_name, capacity, required_capacity, environment
):
    """Validate that required capacity attributes are present"""
    for cap in required_capacity:
        qualified_cap = f"gpu.amd.com/{cap}"
        K8Helper.triage(
            environment,
            qualified_cap in capacity,
            f"Device {device_name}: Missing capacity '{qualified_cap}'",
        )
        if qualified_cap in capacity:
            Logger.info(f"  ✓ Capacity {cap}: {capacity[qualified_cap]}")


def validate_all_capacity_values_nonzero(device_name, capacity, environment):
    """Validate all capacity values (memory, computeUnits, simdUnits) are non-zero"""
    memory = capacity.get("gpu.amd.com/memory", "0")
    compute_units = capacity.get("gpu.amd.com/computeUnits", "0")
    simd_units = capacity.get("gpu.amd.com/simdUnits", "0")

    K8Helper.triage(
        environment,
        memory not in ["0", "", None],
        f"Device {device_name}: memory capacity is not set or zero",
    )

    K8Helper.triage(
        environment,
        compute_units not in ["0", "", None],
        f"Device {device_name}: computeUnits capacity is not set or zero",
    )

    K8Helper.triage(
        environment,
        simd_units not in ["0", "", None],
        f"Device {device_name}: simdUnits capacity is not set or zero",
    )


def validate_partition_profile_full_gpu(device_name, gpu_attrs, environment):
    """Validate partitionProfile for full GPU (should be spx_nps1)"""
    partition_profile = gpu_attrs.get("partitionProfile", "")
    K8Helper.triage(
        environment,
        partition_profile == "spx_nps1",
        f"Device {device_name}: Expected partitionProfile='spx_nps1', got '{partition_profile}'",
    )
    Logger.info(f"  ✓ partitionProfile: {partition_profile}")


def validate_partition_profile_format(device_name, gpu_attrs, environment):
    """Validate partitionProfile format for partition devices

    Format: <type>_<config>
    Type: spx, cpx, dpx, qpx
    Config: nps1, nps2, nps4
    """
    partition_profile = gpu_attrs.get("partitionProfile", "")

    # Check not empty
    K8Helper.triage(
        environment,
        partition_profile != "",
        f"Partition {device_name}: partitionProfile should not be empty",
    )

    # Split and validate format
    profile_parts = partition_profile.split("_")
    K8Helper.triage(
        environment,
        len(profile_parts) == 2,
        f"Partition {device_name}: partitionProfile '{partition_profile}' should have format '<type>_<config>' (e.g., 'spx_nps1')",
    )

    if len(profile_parts) == 2:
        partition_type = profile_parts[0]
        partition_config = profile_parts[1]

        # Validate first part (partition type)
        valid_partition_types = ["spx", "cpx", "dpx", "qpx"]
        K8Helper.triage(
            environment,
            partition_type in valid_partition_types,
            f"Partition {device_name}: partitionProfile type '{partition_type}' is invalid. Expected one of: {valid_partition_types}",
        )

        # Validate second part (configuration)
        valid_configs = ["nps1", "nps2", "nps4"]
        K8Helper.triage(
            environment,
            partition_config in valid_configs,
            f"Partition {device_name}: partitionProfile config '{partition_config}' is invalid. Expected one of: {valid_configs}",
        )

        Logger.info(
            f"  ✓ partitionProfile validated: type={partition_type}, config={partition_config}"
        )


def validate_common_device_attributes(
    device_name, gpu_attrs, capacity, required_attrs, environment
):
    """Validate common attributes shared by both full GPUs and partitions

    Validates:
    - Required attributes presence
    - PCI address format
    - Critical attributes not empty
    - cardIndex and renderIndex are valid integers
    - Device name format matches cardIndex and renderIndex
    - Capacity attributes present
    """
    # Call common validation functions using module-level constants
    validate_required_attributes_present(
        device_name, gpu_attrs, required_attrs, environment
    )
    validate_pci_address_format(device_name, gpu_attrs, environment)
    validate_critical_attributes_not_empty(
        device_name, gpu_attrs, CRITICAL_ATTRS, environment
    )

    # Validate cardIndex/renderIndex and device name (common to both types)
    card_index, render_index = validate_card_and_render_indices(
        device_name, gpu_attrs, environment
    )
    validate_device_name_format(device_name, card_index, render_index, environment)

    # Validate capacity attributes (common to both types)
    validate_capacity_attributes_present(
        device_name, capacity, REQUIRED_CAPACITY_ATTRS, environment
    )
    validate_all_capacity_values_nonzero(device_name, capacity, environment)


def validate_full_gpu_attributes(device, environment):
    """Validate attributes for a full GPU device

    As documented in: https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md#attributes-for-a-full-gpu

    Note: Full GPUs have deviceID and pciAddr attributes.

    Args:
        device: Device dict with fields: name, type, attributes, capacity
        environment: Test environment
    """
    global Logger

    device_name = device.get("name", "unknown")
    gpu_attrs = device.get("attributes", {})
    capacity = device.get("capacity", {})

    Logger.info(f"Validating full GPU device: {device_name}")
    Logger.info(f"  Attributes: {json.dumps(gpu_attrs, indent=2)}")
    Logger.info(f"  Capacity: {json.dumps(capacity, indent=2)}")

    # Validate full GPU required attributes
    validate_common_device_attributes(
        device_name, gpu_attrs, capacity, REQUIRED_FULL_GPU_ATTRS, environment
    )

    # Validate full GPU specific attributes
    validate_device_type(device_name, gpu_attrs, "amdgpu", environment)

    # Validate deviceID is not empty (critical for full GPUs)
    device_id = gpu_attrs.get("deviceID", "")
    K8Helper.triage(
        environment,
        device_id != "",
        f"Full GPU {device_name}: deviceID is required but missing or empty",
    )

    # Validate pciAddr is not empty (critical for full GPUs)
    pci_addr = gpu_attrs.get("pciAddr", "")
    K8Helper.triage(
        environment,
        pci_addr != "",
        f"Full GPU {device_name}: pciAddr is required but missing or empty",
    )

    validate_partition_profile_full_gpu(device_name, gpu_attrs, environment)

    # Validate pcieRoot attribute (optional)
    pci_addr = gpu_attrs.get("pciAddr", "")
    validate_pcie_root_attribute(device_name, pci_addr, gpu_attrs, environment)


def validate_partition_attributes(device, environment):
    """Validate attributes for a GPU partition device

    As documented in: https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md#attributes-for-a-partition

    Note: When a GPU is partitioned (e.g., CPX_NPS4), the DRA driver reports ALL devices
    as type "amdgpu-partition", including the full GPU's renderD device. This is because
    even the full GPU view operates under the partition profile.

    Args:
        device: Device dict with fields: name, type, attributes, capacity
        environment: Test environment
    """
    global Logger

    device_name = device.get("name", "unknown")
    gpu_attrs = device.get("attributes", {})
    capacity = device.get("capacity", {})

    Logger.info(f"Validating GPU partition device: {device_name}")
    Logger.info(f"  Attributes: {json.dumps(gpu_attrs, indent=2)}")
    Logger.info(f"  Capacity: {json.dumps(capacity, indent=2)}")

    # Validate common attributes (using partition-specific required attrs)
    validate_common_device_attributes(
        device_name, gpu_attrs, capacity, REQUIRED_PARTITION_ATTRS, environment
    )

    # Validate partition specific attributes
    validate_device_type(device_name, gpu_attrs, "amdgpu-partition", environment)
    validate_partition_profile_format(device_name, gpu_attrs, environment)

    # Validate parent PCI address is set (required)
    parent_pci_addr = gpu_attrs.get("parentPciAddr", "")
    K8Helper.triage(
        environment,
        parent_pci_addr != "",
        f"Partition {device_name}: parentPciAddr is required but missing or empty",
    )

    # Validate parent device ID is set (required)
    parent_device_id = gpu_attrs.get("parentDeviceID", "")
    K8Helper.triage(
        environment,
        parent_device_id != "",
        f"Partition {device_name}: parentDeviceID is required but missing or empty",
    )

    # Validate numaNode if present (optional attribute)
    numa_node = gpu_attrs.get("numaNode")
    if numa_node is not None:
        K8Helper.triage(
            environment,
            isinstance(numa_node, int) and numa_node >= 0,
            f"Partition {device_name}: numaNode must be a non-negative integer if present, got {numa_node}",
        )
        Logger.info(f"  ✓ numaNode: {numa_node}")

    # Validate pcieRoot attribute (optional)
    # Note: pciAddr may be null for partitions in new format - use parentPciAddr instead
    pci_addr = gpu_attrs.get("pciAddr", "") or parent_pci_addr
    validate_pcie_root_attribute(device_name, pci_addr, gpu_attrs, environment)


def validate_device_identifiers_uniqueness(amd_devices, environment):
    """Validate uniqueness of device identifiers per-node

    Validates that cardIndex, renderIndex, deviceID (for full GPUs), and pciAddr (for full GPUs)
    are unique within each node. Partitions can share deviceID and pciAddr with their parent GPU.

    Note: cardIndex and renderIndex are node-local identifiers, so they only need to be unique
    within the same node, not across the entire cluster.
    """
    Logger.info("Validating uniqueness of device identifiers per-node...")

    # Group devices by node
    devices_by_node = {}
    for device in amd_devices:
        attrs = device.get("attributes", {})
        device_name = device.get("name", "")
        node_name = (
            attrs.get("nodeName")
            or attrs.get("node")
            or device.get("nodeName")
            or device.get("node")
        )
        if not node_name:
            node_name = "__unknown_node__"
            Logger.warning(
                f"Device '{device_name}' is missing node metadata; "
                f"validating it under fallback node bucket '{node_name}'"
            )
        devices_by_node.setdefault(node_name, []).append(device)

    Logger.info(f"Validating devices across {len(devices_by_node)} node(s)")
    Logger.debug(f"Nodes found: {list(devices_by_node.keys())}")
    for node_name, devices in devices_by_node.items():
        Logger.debug(f"  Node '{node_name}': {len(devices)} device(s)")

    # Validate uniqueness within each node
    for node_name, node_devices in devices_by_node.items():
        Logger.info(f"Validating {len(node_devices)} device(s) on node: {node_name}")

        # Maps: attribute_value -> device name (to detect duplicates within node)
        card_indices_map = {}
        render_indices_map = {}
        device_ids_map = {}
        pci_addrs_map = {}

        for device in node_devices:
            attrs = device.get("attributes", {})
            device_name = device.get("name", "")

            # Extract device attributes
            device_id = attrs.get("deviceID")
            pci_addr = attrs.get("pciAddr")
            device_type = attrs.get("deviceType")
            card_idx = attrs.get("cardIndex")
            render_idx = attrs.get("renderIndex")

            # DeviceID can be shared among partitions from same parent GPU
            # Just track them for reference, no uniqueness check
            if device_id:
                if device_id not in device_ids_map:
                    device_ids_map[device_id] = []
                device_ids_map[device_id].append(device_name)

            # pciAddr tracking (may be null for partitions using new format)
            # In new format, partitions have pciAddr=null and use parentPciAddr instead
            if pci_addr:
                if pci_addr not in pci_addrs_map:
                    pci_addrs_map[pci_addr] = []
                pci_addrs_map[pci_addr].append(device_name)

                # Check for duplicate deviceID - only allowed for partitions (share parent GPU's ID)
                if device_id:
                    if device_id in device_ids_map and device_type == "amdgpu":
                        K8Helper.triage(
                            environment,
                            False,
                            f"Node {node_name}: Duplicate deviceID {device_id} on full GPU: already used by {device_ids_map[device_id]}, found again on {device_name}",
                        )
                    if device_id not in device_ids_map:
                        device_ids_map[device_id] = device_name

                # Check for duplicate pciAddr - only allowed for partitions (share parent GPU's PCI)
                if pci_addr:
                    if pci_addr in pci_addrs_map and device_type == "amdgpu":
                        K8Helper.triage(
                            environment,
                            False,
                            f"Node {node_name}: Duplicate pciAddr {pci_addr} on full GPU: already used by {pci_addrs_map[pci_addr]}, found again on {device_name}",
                        )
                    if pci_addr not in pci_addrs_map:
                        pci_addrs_map[pci_addr] = device_name

                # Validate device naming convention: gpu-<cardIndex>-<renderIndex>
                name_pattern = r"^gpu-\d+-\d+$"
                K8Helper.triage(
                    environment,
                    re.match(name_pattern, device_name) is not None,
                    f"Node {node_name}: Device name '{device_name}' doesn't match pattern 'gpu-<cardIndex>-<renderIndex>'",
                )

                # Verify name components match attributes
                if re.match(name_pattern, device_name):
                    parts = device_name.split("-")
                    name_card_idx = int(parts[1])
                    name_render_idx = int(parts[2])

                    K8Helper.triage(
                        environment,
                        name_card_idx == card_idx,
                        f"Node {node_name}: Device {device_name}: cardIndex in name ({name_card_idx}) != attribute ({card_idx})",
                    )

                    K8Helper.triage(
                        environment,
                        name_render_idx == render_idx,
                        f"Node {node_name}: Device {device_name}: renderIndex in name ({name_render_idx}) != attribute ({render_idx})",
                    )

    Logger.info(
        "✓ All device identifiers are unique per-node (or correctly shared for partitions)"
    )
    Logger.info("✓ All device names follow canonical naming convention")


def validate_common_attributes_consistency(amd_devices, environment):
    """Validate common attributes are consistent across all devices

    Checks that driverSrcVersion, driverVersion, family, and productName
    have the same value across all devices.
    """
    Logger.info("Validating common attributes are not null/empty and consistent...")

    common_attrs = {
        "driverSrcVersion": set(),
        "driverVersion": set(),
        "family": set(),
        "productName": set(),
    }

    for device in amd_devices:
        attrs = device.get("attributes", {})
        device_name = device.get("name", "")

        for attr_name in common_attrs.keys():
            attr_value = attrs.get(attr_name)

            # 1. Check if empty, throw error immediately
            K8Helper.triage(
                environment,
                attr_value is not None and attr_value != "",
                f"Device {device_name}: Attribute '{attr_name}' is null or empty",
            )

            # 2. If set is empty, add it; otherwise compare with existing value
            if len(common_attrs[attr_name]) == 0:
                common_attrs[attr_name].add(attr_value)
            else:
                existing_value = list(common_attrs[attr_name])[0]
                K8Helper.triage(
                    environment,
                    attr_value == existing_value,
                    f"Device {device_name}: Attribute '{attr_name}' value '{attr_value}' differs from expected '{existing_value}'",
                )


def validate_partition_parent_correlation(amd_devices, environment):
    """Validate partitions are correctly linked to their parent GPUs

    When GPU is unpartitioned (SPX_NPS1):
    - Parent is type "amdgpu" with deviceID and pciAddr
    - Partitions (if any) reference it

    When GPU is partitioned (e.g., CPX_NPS4):
    - ALL devices are type "amdgpu-partition"
    - The "parent" is the device with lowest renderIndex (full GPU renderD)
    - Other partitions reference it via parentDeviceID and parentPciAddr
    """
    Logger.info("Validating partition correlation to parent GPUs...")

    # Separate by type
    full_gpus = [d for d in amd_devices if d.get("type") == "amdgpu"]
    partitions = [d for d in amd_devices if d.get("type") == "amdgpu-partition"]

    if len(partitions) == 0:
        Logger.info("No partitions found - skipping partition correlation validation")
        return

    Logger.info(
        f"Found {len(partitions)} partition device(s), {len(full_gpus)} full GPU(s)"
    )

    # Build parent device maps
    # For unpartitioned GPUs: parent is type "amdgpu"
    # For partitioned GPUs: parent is the partition with lowest renderIndex (full GPU view)
    parent_by_pci = {}
    parent_by_device_id = {}

    # Add full GPU devices as potential parents
    for gpu in full_gpus:
        device_id = gpu.get("attributes", {}).get("deviceID", "")
        pci_addr = gpu.get("attributes", {}).get("pciAddr", "")
        if device_id:
            parent_by_device_id[device_id] = gpu
        if pci_addr:
            parent_by_pci[pci_addr] = gpu

    # For partitioned GPUs, identify the "parent" partition (lowest renderIndex per PCI)
    # Group partitions by parentPciAddr
    partitions_by_parent_pci = {}
    for part in partitions:
        parent_pci = part.get("attributes", {}).get("parentPciAddr", "")
        if parent_pci:
            if parent_pci not in partitions_by_parent_pci:
                partitions_by_parent_pci[parent_pci] = []
            partitions_by_parent_pci[parent_pci].append(part)

    # For each PCI address with partitions, find the one with lowest renderIndex
    for parent_pci, parts in partitions_by_parent_pci.items():
        # Sort by renderIndex to find the "parent" partition (lowest index = full GPU view)
        parts_sorted = sorted(parts, key=lambda p: p.get("attributes", {}).get("renderIndex", 999))
        if parts_sorted:
            parent_partition = parts_sorted[0]
            parent_device_id = parent_partition.get("attributes", {}).get("parentDeviceID", "")

            # Add this as a parent reference
            if parent_pci not in parent_by_pci:
                parent_by_pci[parent_pci] = parent_partition
            if parent_device_id and parent_device_id not in parent_by_device_id:
                parent_by_device_id[parent_device_id] = parent_partition

    Logger.info(f"  Identified {len(parent_by_pci)} parent device(s) by PCI address")

    # Validate each partition correlates to a parent
    # Skip validation for the "parent" partitions themselves (lowest renderIndex per PCI)
    validated_count = 0
    skipped_parent_count = 0

    for partition in partitions:
        partition_name = partition.get("name", "")
        part_attrs = partition.get("attributes", {})
        partition_render_idx = part_attrs.get("renderIndex")

        parent_device_id = part_attrs.get("parentDeviceID", "")
        parent_pci_addr = part_attrs.get("parentPciAddr", "")

        # Check if this partition IS the parent (lowest renderIndex for its PCI address)
        is_parent = False
        if parent_pci_addr in partitions_by_parent_pci:
            sorted_parts = sorted(
                partitions_by_parent_pci[parent_pci_addr],
                key=lambda p: p.get("attributes", {}).get("renderIndex", 999)
            )
            if sorted_parts and sorted_parts[0].get("name") == partition_name:
                is_parent = True
                skipped_parent_count += 1
                Logger.debug(
                    f"  Skipping {partition_name}: This is the parent partition (lowest renderIndex) for PCI {parent_pci_addr}"
                )
                continue

        # Validate non-parent partitions
        partition_profile = part_attrs.get("partitionProfile", "")

        # Try to find parent by PCI address first (most reliable)
        parent_device = parent_by_pci.get(parent_pci_addr)

        if not parent_device and parent_device_id:
            # Fallback to deviceID lookup
            parent_device = parent_by_device_id.get(parent_device_id)

        if parent_device:
            parent_name = parent_device.get("name", "")
            parent_type = parent_device.get("type", "")

            # For partitioned GPUs, parent can be either "amdgpu" or "amdgpu-partition" (the base partition)
            K8Helper.triage(
                environment,
                parent_type in ["amdgpu", "amdgpu-partition"],
                f"Partition {partition_name}: Parent device has unexpected type '{parent_type}'",
            )

            Logger.debug(
                f"  ✓ Partition {partition_name} (profile: {partition_profile}) → parent {parent_name} (type: {parent_type})"
            )
            validated_count += 1
        else:
            K8Helper.triage(
                environment,
                False,
                f"Partition {partition_name}: No parent found with parentPciAddr={parent_pci_addr} or parentDeviceID={parent_device_id}",
            )

    Logger.info(
        f"✓ Validated {validated_count} partition(s), skipped {skipped_parent_count} parent partition(s)"
    )


def validate_pcie_root_attribute(device_name, pci_addr, gpu_attrs, environment):
    """Validate pcieRoot attribute format and consistency with PCI address

    The pcieRoot should be derived from the PCI address (domain:bus portion).
    For example, PCI address "0000:83:00.0" should have pcieRoot "0000:83".

    Args:
        device_name: Name of the device
        pci_addr: PCI address from pciAddr attribute
        gpu_attrs: Device attributes dict
        environment: Test environment

    Returns:
        None
    """
    pcie_root = gpu_attrs.get("pcieRoot", "")

    # If pcieRoot is not present, it's optional so just log
    if not pcie_root:
        Logger.debug(f"  {device_name}: pcieRoot attribute not present (optional)")
        return

    # Validate format: should match domain:bus pattern (e.g., "0000:83")
    pcie_root_pattern = r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}$"
    if not re.match(pcie_root_pattern, pcie_root):
        K8Helper.triage(
            environment,
            False,
            f"Device {device_name}: pcieRoot '{pcie_root}' has invalid format. Expected format: DDDD:BB (e.g., '0000:83')",
        )

    # Validate consistency with PCI address
    # Expected: pcieRoot should be first two parts of pciAddr
    # pciAddr: 0000:83:00.0 -> pcieRoot: 0000:83
    if not pci_addr:
        Logger.debug(
            f"  {device_name}: Cannot validate pcieRoot consistency - pciAddr is missing"
        )
        return

    pci_parts = pci_addr.split(":")
    if len(pci_parts) >= 2:
        expected_pcie_root = f"{pci_parts[0]}:{pci_parts[1]}"

        K8Helper.triage(
            environment,
            pcie_root == expected_pcie_root,
            f"Device {device_name}: pcieRoot '{pcie_root}' doesn't match PCI address '{pci_addr}'. Expected '{expected_pcie_root}' (domain:bus from pciAddr)",
        )

        Logger.info(f"  ✓ pcieRoot: {pcie_root} (matches PCI address)")
    else:
        Logger.warning(
            f"  {device_name}: Cannot validate pcieRoot - pciAddr format unexpected: {pci_addr}"
        )


def test_dra_driver_device_attributes(dra_driver_install, environment):
    """Test that DRA driver advertises all required device attributes.

    This test validates that the DRA driver correctly advertises all required
    device attributes for AMD GPUs via ResourceSlices. It checks both the
    presence and validity of each attribute.

    Validates:
        - ResourceSlices are created and populated (waits up to 60 seconds)
        - Each GPU device has all required attributes:
            * type: Device type (e.g., "amdgpu")
            * pciAddr: PCI address (format: 0000:06:00.0 or 06:00.0)
            * drmCardIndex: DRM card index (integer >= 0)
            * numCUs: Number of compute units (integer > 0)
            * numSIMDs: Number of SIMD units (integer > 0)
            * devName: Device name/codename (non-empty string)
            * uuid: Unique device identifier (non-empty string)
            * vram: VRAM size in bytes (integer > 0)
        - Attribute values are valid (correct types, reasonable ranges)
        - Partition attributes if device is partitioned

    Fixtures:
        dra_driver_install: Ensures DRA driver is installed and running

    kubectl equivalents:
        kubectl get resourceslices.resource.k8s.io
        kubectl get resourceslices -o yaml

    Expected outcome:
        All GPU devices have complete and valid attribute sets

    Reference:
        https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md
    """
    global Logger

    # Wait for ResourceSlices to be created and populated
    # DRA driver may take some time to discover and advertise devices
    Logger.info("Waiting for ResourceSlices to be populated...")
    max_wait = 60  # Wait up to 60 seconds
    amd_devices = []

    for attempt in range(6):  # 6 attempts x 10 seconds = 60 seconds
        time.sleep(10)

        resource_slices = get_resource_slices()
        amd_devices = get_amd_gpu_devices_from_slices(resource_slices)

        if len(amd_devices) > 0:
            Logger.info(
                f"Found {len(amd_devices)} AMD GPU device(s) in ResourceSlices after {(attempt + 1) * 10} seconds"
            )
            break

        Logger.debug(f"Attempt {attempt + 1}: No AMD devices found yet, waiting...")

    # Final check
    resource_slices = get_resource_slices()
    K8Helper.triage(
        environment,
        len(resource_slices) > 0,
        "No ResourceSlices found in cluster - DRA driver may not be running correctly",
    )

    Logger.info(f"Found {len(resource_slices)} ResourceSlice(s) in cluster")

    # Extract AMD GPU devices
    amd_devices = get_amd_gpu_devices_from_slices(resource_slices)

    # Provide more detailed error message if no devices found
    if len(amd_devices) == 0:
        # Check if we have GPU nodes with AMD hardware and driver loaded
        ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
        gpu_node_count = len(gpu_nodes) if ret_code == 0 else 0

        # Check if we have ResourceSlices with null devices (DRA driver bug)
        null_device_slices = [
            rs for rs in resource_slices
            if rs.get("spec", {}).get("driver") == "gpu.amd.com" and rs.get("spec", {}).get("devices") is None
        ]

        error_msg = "No AMD GPU devices found in ResourceSlices after waiting. "

        if gpu_node_count == 0:
            error_msg += "\nNo GPU nodes found with NFD label 'feature.node.kubernetes.io/amd-gpu=true'. "
            error_msg += "Check if: 1) AMD GPU hardware exists, 2) amdgpu driver is loaded, 3) NFD is running and labeling nodes"
        elif null_device_slices:
            # We have GPU nodes but ResourceSlices have null devices - DRA driver bug
            error_msg += f"\nFound {gpu_node_count} GPU node(s) with AMD GPUs, but {len(null_device_slices)} ResourceSlice(s) have null devices. "
            error_msg += "This is likely a DRA driver bug for non-partitioned GPUs. "
            error_msg += f"\nGPU nodes: {[k8_util.k8_get_node_hostname(n) for n in gpu_nodes]}"
            error_msg += f"\nAffected ResourceSlices: {[rs.get('metadata', {}).get('name') for rs in null_device_slices]}"
        else:
            error_msg += f"\nFound {gpu_node_count} GPU node(s) but no ResourceSlices from DRA driver. "
            error_msg += "Check if: 1) DRA driver pods are running, 2) DRA driver has permissions to publish ResourceSlices"

        K8Helper.triage(environment, False, error_msg)

    Logger.info(f"Found {len(amd_devices)} AMD GPU device(s) in ResourceSlices")

    # Track device types
    full_gpu_count = 0
    partition_count = 0

    # Validate each device
    for device in amd_devices:
        device_type = device.get("type", "")

        if device_type == "amdgpu":
            full_gpu_count += 1
            validate_full_gpu_attributes(device, environment)
        elif device_type == "amdgpu-partition":
            partition_count += 1
            validate_partition_attributes(device, environment)
        else:
            Logger.warning(f"Unknown device type: {device_type}")

    Logger.info(f"Validated {full_gpu_count} full GPU(s)")
    Logger.info(f"Validated {partition_count} partition(s)")

    # At least one device should be validated
    K8Helper.triage(
        environment,
        (full_gpu_count + partition_count) > 0,
        "No valid AMD GPU devices found",
    )

    # Cross-device validations
    validate_device_identifiers_uniqueness(amd_devices, environment)
    validate_common_attributes_consistency(amd_devices, environment)
    validate_partition_parent_correlation(amd_devices, environment)


def test_dra_gpu_count_matches_hardware(
    amdgpu_driver_install, dra_driver_install, environment, gpu_hardware_info
):
    """Test that DRA advertises the same number of GPUs as detected by hardware.

    This test validates that the GPU count advertised by the DRA driver matches
    the actual number of GPUs detected on each node via hardware inspection
    (lspci and sysfs). This is a basic sanity check before detailed attribute
    validation.

    Validates:
        - DRA advertised GPU count matches hardware detected count
        - Count check performed per-node (all GPU nodes validated)
        - Only "amdgpu" type devices counted (partitions excluded)
        - Reports specific node if mismatch found

    Fixtures:
        dra_driver_install: Ensures DRA driver is installed
        gpu_hardware_info: Cached hardware info for all GPU nodes

    Hardware Detection Method:
        - Spawns privileged debug pod on each GPU node
        - Runs lspci to detect AMD GPUs
        - Reads /sys/class/drm to enumerate devices
        - Collects data once per test module (shared fixture)

    kubectl equivalents:
        kubectl get resourceslices -o yaml
        # Hardware detection uses privileged pod with lspci and sysfs

    Expected outcome:
        DRA count == Hardware count for all GPU nodes

    Notes:
        If this test fails but test_dra_devices_match_hardware passes,
        it may indicate a filtering or type classification issue.
    """
    global Logger

    # Check each GPU node using cached hardware info
    mismatches = []

    for node_name, hw_data in gpu_hardware_info.items():
        Logger.info(f"Validating node: {node_name}")

        # Use cached hardware info (collected once by fixture)
        hw_info = hw_data["hardware"]

        # Get DRA advertised devices for this node
        dra_devices = get_amd_gpu_devices_from_slices(node_name=node_name)

        # Count hardware GPUs and partitions
        hw_gpu_count = len(hw_info["gpus"])
        hw_partition_count = sum(len(gpu.get("partitions", [])) for gpu in hw_info["gpus"])

        # Count DRA advertised devices by type
        dra_full_gpu_count = len([d for d in dra_devices if d.get("type") == "amdgpu"])
        dra_partition_count = len([d for d in dra_devices if d.get("type") == "amdgpu-partition"])

        Logger.info(f"  Hardware: {hw_gpu_count} GPU(s), {hw_partition_count} partition(s)")
        Logger.info(f"  DRA advertised: {dra_full_gpu_count} full GPU(s), {dra_partition_count} partition(s)")

        # Validation logic:
        # If no partitions detected in hardware, DRA should report full GPUs
        # If partitions detected, DRA reports all as partitions (including full GPU renderD)
        if hw_partition_count == 0:
            # Unpartitioned GPUs - expect full GPU type
            if hw_gpu_count != dra_full_gpu_count:
                mismatches.append(
                    f"{node_name}: Unpartitioned GPUs - HW={hw_gpu_count}, DRA full GPUs={dra_full_gpu_count}"
                )
        else:
            # Partitioned GPUs - expect partition type for all devices
            if hw_partition_count != dra_partition_count:
                mismatches.append(
                    f"{node_name}: Partitioned GPUs - HW partitions={hw_partition_count}, DRA partitions={dra_partition_count}"
                )

    # Report results
    if mismatches:
        error_msg = f"GPU/partition count mismatches found:\n" + "\n".join(mismatches)
        Logger.error(error_msg)
        K8Helper.triage(environment, False, error_msg)
    else:
        Logger.info("✓ All nodes: DRA device count matches hardware detection")


def compare_hw_attribute_with_dra(
    node_name, pci_addr, hw_value, dra_value, attr_name, is_critical=True
):
    """Compare a hardware attribute value with DRA advertised value

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_value: Hardware value
        dra_value: DRA advertised value
        attr_name: Name of the attribute being compared
        is_critical: If True, returns error message on mismatch; if False, logs warning only

    Returns:
        str or None: Error message if mismatch and is_critical=True, None otherwise
    """
    # Skip comparison if either value is empty
    if not hw_value or not dra_value:
        return None

    if hw_value != dra_value:
        msg = f"Node {node_name}, PCI {pci_addr}: {attr_name} mismatch - HW={hw_value}, DRA={dra_value}"
        if is_critical:
            Logger.error(msg)
            return msg
        else:
            Logger.warning(msg)
            return None
    else:
        Logger.debug(f"    ✓ {attr_name} matches: {hw_value}")
        return None


def validate_device_id_match(node_name, pci_addr, hw_gpu, dra_gpu):
    """Validate device ID matches between hardware and DRA

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_gpu: Hardware GPU information dict
        dra_gpu: DRA GPU device dict

    Returns:
        str or None: Error message if mismatch, None if match
    """
    hw_device_id = hw_gpu.get("device_id", "").lower()
    dra_device_id = dra_gpu["attributes"].get("deviceID", "")

    # Normalize DRA device ID (remove 0x prefix if present)
    if dra_device_id.startswith("0x"):
        dra_device_id = dra_device_id[2:]
    dra_device_id = dra_device_id.lower()

    return compare_hw_attribute_with_dra(
        node_name, pci_addr, hw_device_id, dra_device_id, "Device ID", is_critical=True
    )


def validate_product_name_match(node_name, pci_addr, hw_gpu, dra_gpu):
    """Validate product name matches between hardware and DRA

    Note: Product name mismatch is treated as a warning, not an error

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_gpu: Hardware GPU information dict
        dra_gpu: DRA GPU device dict

    Returns:
        None: Always returns None (warnings only, no errors)
    """
    hw_product = hw_gpu.get("product_name", "")
    dra_product = dra_gpu["attributes"].get("productName", "")

    return compare_hw_attribute_with_dra(
        node_name, pci_addr, hw_product, dra_product, "Product name", is_critical=False
    )


def validate_card_index_match(node_name, pci_addr, hw_gpu, dra_gpu):
    """Validate cardIndex matches between hardware and DRA

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_gpu: Hardware GPU information dict
        dra_gpu: DRA GPU device dict

    Returns:
        str or None: Error message if mismatch, None if match
    """
    hw_card = hw_gpu.get("cardIndex", "")
    dra_card = str(dra_gpu["attributes"].get("cardIndex", ""))

    return compare_hw_attribute_with_dra(
        node_name, pci_addr, hw_card, dra_card, "cardIndex", is_critical=True
    )


def validate_render_index_match(node_name, pci_addr, hw_gpu, dra_gpu):
    """Validate renderIndex matches between hardware and DRA

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_gpu: Hardware GPU information dict
        dra_gpu: DRA GPU device dict

    Returns:
        str or None: Error message if mismatch, None if match
    """
    hw_render = hw_gpu.get("renderIndex", "")
    dra_render = str(dra_gpu["attributes"].get("renderIndex", ""))

    return compare_hw_attribute_with_dra(
        node_name, pci_addr, hw_render, dra_render, "renderIndex", is_critical=True
    )


def validate_driver_version_match(node_name, pci_addr, hw_gpu, dra_gpu):
    """Validate driverVersion matches between hardware and DRA

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_gpu: Hardware GPU information dict
        dra_gpu: DRA GPU device dict

    Returns:
        str or None: Error message if mismatch, None if match
    """
    hw_driver_ver = hw_gpu.get("driverVersion", "")
    dra_driver_ver = dra_gpu["attributes"].get("driverVersion", "")

    return compare_hw_attribute_with_dra(
        node_name,
        pci_addr,
        hw_driver_ver,
        dra_driver_ver,
        "driverVersion",
        is_critical=True,
    )


def validate_driver_src_version_match(node_name, pci_addr, hw_gpu, dra_gpu):
    """Validate driverSrcVersion matches between hardware and DRA

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_gpu: Hardware GPU information dict
        dra_gpu: DRA GPU device dict

    Returns:
        str or None: Error message if mismatch, None if match
    """
    hw_driver_src_ver = hw_gpu.get("driverSrcVersion", "")
    dra_driver_src_ver = dra_gpu["attributes"].get("driverSrcVersion", "")

    return compare_hw_attribute_with_dra(
        node_name,
        pci_addr,
        hw_driver_src_ver,
        dra_driver_src_ver,
        "driverSrcVersion",
        is_critical=True,
    )


def validate_partition_profile_from_hardware(
    node_name, pci_addr, hw_gpu, dra_devices_for_pci, environment
):
    """Validate partition profile and type based on hardware partition state

    Validates partition profiles against hardware state using these rules:
    1. If one of current_compute_partition or current_memory_partition is empty, the other must be empty too
    2. If current_memory_partition is empty, type in ResourceSlice should be 'amdgpu'
    3. If current_compute_partition is 'SPX' and current_memory_partition is 'NPS1', type should be 'amdgpu'; otherwise 'amdgpu-partition'
    4. If both are not empty, current_compute_partition + '_' + current_memory_partition should equal partitionProfile in ResourceSlice

    Args:
        node_name: Name of the node
        pci_addr: PCI address of the GPU
        hw_gpu: Hardware GPU information dict
        dra_devices_for_pci: List of DRA devices at this PCI address
        environment: Test environment
    """
    hw_compute_part = (hw_gpu.get("current_compute_partition") or "").strip().upper()
    hw_memory_part = (hw_gpu.get("current_memory_partition") or "").strip().upper()

    # Rule 1: compute/memory partition should be both empty or both non-empty
    K8Helper.triage(
        environment,
        bool(hw_compute_part) == bool(hw_memory_part),
        f"Node {node_name}, PCI {pci_addr}: partition state invalid - current_compute_partition='{hw_compute_part}', current_memory_partition='{hw_memory_part}' (both must be empty or both non-empty)",
    )

    # Rules 2 & 3: Determine expected type based on partition state
    if not hw_memory_part:
        # Rule 2: No partitioning
        expected_type = "amdgpu"
    elif hw_compute_part == "SPX" and hw_memory_part == "NPS1":
        # Rule 3: SPX_NPS1 is treated as full GPU
        expected_type = "amdgpu"
    else:
        # Rule 3: Any other partitioning scheme
        expected_type = "amdgpu-partition"

    # Validate advertised types match expected
    advertised_types = {d.get("type", "") for d in dra_devices_for_pci}
    K8Helper.triage(
        environment,
        expected_type in advertised_types,
        f"Node {node_name}, PCI {pci_addr}: Expected advertised type '{expected_type}' from hardware partition state ({hw_compute_part}, {hw_memory_part}), got {sorted(advertised_types)}",
    )

    # Rule 4: If partitioned, validate partition profile matches
    if hw_compute_part and hw_memory_part:
        expected_profile = f"{hw_compute_part.lower()}_{hw_memory_part.lower()}"

        # Collect advertised partition profiles from all devices at this PCI address
        advertised_profiles = set()
        for d in dra_devices_for_pci:
            profile = (
                (d.get("attributes", {}).get("partitionProfile") or "").strip().lower()
            )
            if profile:
                advertised_profiles.add(profile)

        K8Helper.triage(
            environment,
            expected_profile in advertised_profiles,
            f"Node {node_name}, PCI {pci_addr}: Expected partitionProfile '{expected_profile}' from hardware partition state, got {sorted(advertised_profiles)}",
        )
        Logger.debug(f"    ✓ partitionProfile matches: {expected_profile}")


def test_dra_devices_match_hardware(dra_driver_install, environment, gpu_hardware_info):
    """Test that DRA advertised GPU attributes match hardware per PCI address.

    This is the most comprehensive validation test. It performs deep attribute
    comparison for each GPU individually, using PCI address as the correlation
    key. This provides precise debugging - identifies exactly which GPU and
    which attribute has mismatched data.

    Validates (per GPU, correlated by PCI address):
        - pciAddr: DRA matches hardware PCI address (normalized to long format)
        - drmCardIndex: DRA matches hardware DRM card index
        - numCUs: DRA matches hardware compute units count
        - devName: DRA matches hardware device name/codename
        - uuid: DRA matches hardware GPU UUID
        - vram: DRA matches hardware VRAM size (bytes)
        - Partition attributes (if GPU is partitioned):
            * partitionProfile: Matches compute_memory partition format
            * partitionIndex: Valid partition index
            * parentPciAddr: References parent GPU PCI address

    Fixtures:
        dra_driver_install: Ensures DRA driver is installed
        gpu_hardware_info: Cached hardware info for all GPU nodes

    Hardware Detection Method:
        - Reads /sys/class/drm/card*/device/* for GPU attributes
        - Uses lspci for PCI address validation
        - Collects partition info from sysfs
        - Data collected once per module (shared fixture)

    Correlation Method:
        1. Build hardware GPU map by PCI address (normalized)
        2. For each DRA device, find matching hardware GPU by PCI
        3. Compare all attributes individually
        4. Report any mismatches with details

    kubectl equivalents:
        kubectl get resourceslices -o yaml
        # Hardware: privileged pod reading /sys/class/drm and lspci

    Expected outcome:
        All DRA GPU attributes match corresponding hardware values

    Error Reporting:
        - Lists all mismatches with node, PCI address, attribute name
        - Shows expected (hardware) vs actual (DRA) values
        - Reports missing GPUs (in hardware but not in DRA or vice versa)

    Notes:
        This test is most useful for debugging attribute issues. If it fails,
        check the detailed mismatch report to identify the specific problem.
    """
    global Logger

    all_mismatches = []

    for node_name, hw_data in gpu_hardware_info.items():
        Logger.info(f"Validating GPU attributes for node: {node_name}")

        # Use cached hardware info (collected once by fixture)
        hw_info = hw_data["hardware"]

        # Build hardware GPU map by PCI address (use full format from collector)
        hw_gpus_by_pci = {}
        for gpu in hw_info["gpus"]:
            # Prefer pci_address_full (always set by enhanced collector)
            # Fallback to pci_address with normalization for backward compatibility
            pci_addr = gpu.get("pci_address_full")
            if not pci_addr:
                pci_addr = gpu.get("pci_address", "")
                # Normalize short format to long format (0000:06:00.0)
                if pci_addr and pci_addr.count(":") == 1:
                    pci_addr = f"0000:{pci_addr}"

            if pci_addr:
                hw_gpus_by_pci[pci_addr] = gpu

        Logger.info(f"  Hardware GPUs by PCI: {list(hw_gpus_by_pci.keys())}")

        # Get DRA devices for this node
        dra_devices = get_amd_gpu_devices_from_slices(node_name=node_name)

        # Build DRA device map by PCI address (can have full GPU + partitions on same PCI)
        # Note: partitions may have pciAddr=null and use parentPciAddr instead
        dra_devices_by_pci = {}
        for device in dra_devices:
            # Use pciAddr if available, otherwise parentPciAddr
            pci_addr = device["attributes"].get("pciAddr") or device["attributes"].get("parentPciAddr", "")
            if pci_addr:
                if pci_addr not in dra_devices_by_pci:
                    dra_devices_by_pci[pci_addr] = []
                dra_devices_by_pci[pci_addr].append(device)

        # Pick representative device per PCI for common attribute checks
        # Prefer full GPU (type: amdgpu), otherwise the partition with lowest renderIndex
        # (which represents the full GPU view in partitioned mode)
        dra_primary_by_pci = {}
        for pci_addr, devices in dra_devices_by_pci.items():
            full_gpu = next((d for d in devices if d.get("type") == "amdgpu"), None)
            if full_gpu:
                dra_primary_by_pci[pci_addr] = full_gpu
            else:
                # For partitioned GPUs, pick the one with lowest renderIndex
                # This matches the hardware GPU's cardIndex/renderIndex
                sorted_devices = sorted(
                    devices,
                    key=lambda d: d.get("attributes", {}).get("renderIndex", 999)
                )
                dra_primary_by_pci[pci_addr] = sorted_devices[0] if sorted_devices else devices[0]

        Logger.info(f"  DRA devices by PCI: {list(dra_devices_by_pci.keys())}")

        # Check for missing GPUs
        hw_pci_addrs = set(hw_gpus_by_pci.keys())
        dra_pci_addrs = set(dra_devices_by_pci.keys())

        missing_in_dra = hw_pci_addrs - dra_pci_addrs
        extra_in_dra = dra_pci_addrs - hw_pci_addrs

        if missing_in_dra:
            error_msg = f"Node {node_name}: GPUs in hardware but not advertised in DRA: {missing_in_dra}"
            Logger.error(error_msg)
            all_mismatches.append(error_msg)

        if extra_in_dra:
            error_msg = f"Node {node_name}: GPUs advertised in DRA but not found in hardware: {extra_in_dra}"
            Logger.error(error_msg)
            all_mismatches.append(error_msg)

        # Compare each matched GPU's attributes
        for pci_addr in hw_pci_addrs & dra_pci_addrs:
            hw_gpu = hw_gpus_by_pci[pci_addr]
            dra_devices_for_pci = dra_devices_by_pci[pci_addr]
            dra_gpu = dra_primary_by_pci[pci_addr]

            Logger.debug(f"  Comparing GPU at {pci_addr}")

            # Validate partition profile and type from hardware partition state
            validate_partition_profile_from_hardware(
                node_name, pci_addr, hw_gpu, dra_devices_for_pci, environment
            )

            # If hardware shows partitions, validate DRA reports correct partition count
            hw_partitions = hw_gpu.get("partitions", [])
            if hw_partitions:
                hw_partition_count = len(hw_partitions)
                dra_partition_count = len([d for d in dra_devices_for_pci if d.get("type") == "amdgpu-partition"])

                K8Helper.triage(
                    environment,
                    hw_partition_count == dra_partition_count,
                    f"Node {node_name}, PCI {pci_addr}: Hardware has {hw_partition_count} partition(s), "
                    f"but DRA advertises {dra_partition_count} partition(s)",
                )
                Logger.info(f"  ✓ Partition count matches: {dra_partition_count} partition(s)")

            # Compare GPU attributes between hardware and DRA
            error_msg = validate_device_id_match(node_name, pci_addr, hw_gpu, dra_gpu)
            if error_msg:
                all_mismatches.append(error_msg)

            validate_product_name_match(node_name, pci_addr, hw_gpu, dra_gpu)

            error_msg = validate_card_index_match(node_name, pci_addr, hw_gpu, dra_gpu)
            if error_msg:
                all_mismatches.append(error_msg)

            error_msg = validate_render_index_match(
                node_name, pci_addr, hw_gpu, dra_gpu
            )
            if error_msg:
                all_mismatches.append(error_msg)

            error_msg = validate_driver_version_match(
                node_name, pci_addr, hw_gpu, dra_gpu
            )
            if error_msg:
                all_mismatches.append(error_msg)

            error_msg = validate_driver_src_version_match(
                node_name, pci_addr, hw_gpu, dra_gpu
            )
            if error_msg:
                all_mismatches.append(error_msg)

        if not missing_in_dra and not all_mismatches:
            Logger.info(f"  ✓ All {len(hw_pci_addrs)} GPUs validated successfully")

    # Final triage
    K8Helper.triage(
        environment,
        len(all_mismatches) == 0,
        (
            f"GPU attribute mismatches found: {all_mismatches}"
            if all_mismatches
            else ""
        ),
    )

    Logger.info(
        f"✓ All nodes: DRA GPU attributes match hardware (validated {sum(len(hw_data['hardware']['gpus']) for hw_data in gpu_hardware_info.values())} total GPUs)"

    )
