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
Test DRA driver device attributes as documented in:
https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md
"""

import pytest
import time
import json
import logging
import re
import lib.k8_util as k8_util
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_dra_driver_attributes")


@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    """Skip if not testing on K8s"""
    if environment.deployment_mode != "k8":
        pytest.skip(
            f"Skipping DRA driver attribute testcases for {environment.deployment_mode} deployment"
        )
    return


def get_resource_slices():
    """Get all ResourceSlices from the cluster
    
    kubectl equivalent: kubectl get resourceslices.resource.k8s.io -o yaml
    
    Returns:
        list: List of ResourceSlice objects
    """
    try:
        from kubernetes import client

        custom_api = client.CustomObjectsApi()
        result = custom_api.list_cluster_custom_object(
            group="resource.k8s.io",
            version="v1",
            plural="resourceslices",
        )
        return result.get("items", [])
    except Exception as e:
        Logger.error(f"Failed to get ResourceSlices: {e}")
        return []


def get_amd_gpu_devices_from_slices(resource_slices):
    """Extract AMD GPU devices from ResourceSlices
    
    Args:
        resource_slices: List of ResourceSlice objects
    
    Returns:
        list: List of AMD GPU device objects with attributes
    """
    amd_devices = []
    
    for slice_obj in resource_slices:
        driver_name = slice_obj.get("spec", {}).get("driver", "")
        if driver_name != "gpu.amd.com":
            continue
        
        devices = slice_obj.get("spec", {}).get("devices", [])
        for device in devices:
            amd_devices.append(device)
    
    return amd_devices


def validate_full_gpu_attributes(device, environment):
    """Validate attributes for a full GPU device
    
    As documented in: https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md#attributes-for-a-full-gpu
    
    Args:
        device: Device object from ResourceSlice
        environment: Test environment
    """
    global Logger
    
    device_name = device.get("name", "unknown")
    attributes = device.get("basic", {}).get("attributes", {})
    gpu_attrs = attributes.get("gpu.amd.com", {})
    capacity = device.get("basic", {}).get("capacity", {})
    
    Logger.info(f"Validating full GPU device: {device_name}")
    Logger.info(f"  Attributes: {json.dumps(gpu_attrs, indent=2)}")
    Logger.info(f"  Capacity: {json.dumps(capacity, indent=2)}")
    
    # Required attributes for full GPU
    required_attrs = [
        "type",
        "pciAddr",
        "cardIndex",
        "renderIndex",
        "deviceID",
        "family",
        "productName",
        "driverVersion",
        "driverSrcVersion",
    ]
    
    for attr in required_attrs:
        K8Helper.triage(
            environment,
            attr in gpu_attrs,
            f"Device {device_name}: Missing required attribute '{attr}'",
        )
        if attr in gpu_attrs:
            Logger.info(f"  ✓ {attr}: {gpu_attrs[attr]}")
    
    # Validate type
    device_type = gpu_attrs.get("type", "")
    K8Helper.triage(
        environment,
        device_type == "amdgpu",
        f"Device {device_name}: Expected type='amdgpu', got '{device_type}'",
    )
    
    # Validate PCI address format (should be like 0000:03:00.0)
    pci_addr = gpu_attrs.get("pciAddr", "")
    pci_pattern = r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9]$"
    K8Helper.triage(
        environment,
        re.match(pci_pattern, pci_addr) is not None,
        f"Device {device_name}: Invalid PCI address format '{pci_addr}'",
    )
    
    # Validate cardIndex and renderIndex are integers
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
    
    # Validate device name format: gpu-<cardIndex>-<renderIndex>
    expected_name = f"gpu-{card_index}-{render_index}"
    K8Helper.triage(
        environment,
        device_name == expected_name,
        f"Device name '{device_name}' doesn't match expected format '{expected_name}'",
    )
    
    # Validate capacity attributes
    required_capacity = ["memory", "computeUnits", "simdUnits"]
    for cap in required_capacity:
        qualified_cap = f"gpu.amd.com/{cap}"
        K8Helper.triage(
            environment,
            qualified_cap in capacity,
            f"Device {device_name}: Missing capacity '{qualified_cap}'",
        )
        if qualified_cap in capacity:
            Logger.info(f"  ✓ Capacity {cap}: {capacity[qualified_cap]}")
    
    # Validate memory capacity is a positive value
    memory_cap = capacity.get("gpu.amd.com/memory", "0")
    # Memory is expressed as a quantity string like "16Gi" or bytes
    K8Helper.triage(
        environment,
        memory_cap != "0" and memory_cap != "",
        f"Device {device_name}: Memory capacity should be > 0, got '{memory_cap}'",
    )
    
    # partitionProfile is optional but should be present on devices that support it
    if "partitionProfile" in gpu_attrs:
        Logger.info(f"  ✓ partitionProfile: {gpu_attrs['partitionProfile']}")


def validate_partition_attributes(device, environment):
    """Validate attributes for a GPU partition device
    
    As documented in: https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md#attributes-for-a-partition
    
    Args:
        device: Device object from ResourceSlice
        environment: Test environment
    """
    global Logger
    
    device_name = device.get("name", "unknown")
    attributes = device.get("basic", {}).get("attributes", {})
    gpu_attrs = attributes.get("gpu.amd.com", {})
    capacity = device.get("basic", {}).get("capacity", {})
    
    Logger.info(f"Validating GPU partition device: {device_name}")
    Logger.info(f"  Attributes: {json.dumps(gpu_attrs, indent=2)}")
    Logger.info(f"  Capacity: {json.dumps(capacity, indent=2)}")
    
    # Required attributes for partition
    required_attrs = [
        "type",
        "pciAddr",
        "cardIndex",
        "renderIndex",
        "deviceID",
        "family",
        "productName",
        "driverVersion",
        "driverSrcVersion",
        "partitionProfile",
    ]
    
    for attr in required_attrs:
        K8Helper.triage(
            environment,
            attr in gpu_attrs,
            f"Partition {device_name}: Missing required attribute '{attr}'",
        )
        if attr in gpu_attrs:
            Logger.info(f"  ✓ {attr}: {gpu_attrs[attr]}")
    
    # Validate type
    device_type = gpu_attrs.get("type", "")
    K8Helper.triage(
        environment,
        device_type == "amdgpu-partition",
        f"Partition {device_name}: Expected type='amdgpu-partition', got '{device_type}'",
    )
    
    # Validate partitionProfile format (should be like spx_<mem>)
    partition_profile = gpu_attrs.get("partitionProfile", "")
    K8Helper.triage(
        environment,
        partition_profile != "",
        f"Partition {device_name}: partitionProfile should not be empty",
    )
    
    # Validate capacity attributes
    required_capacity = ["memory", "computeUnits", "simdUnits"]
    for cap in required_capacity:
        qualified_cap = f"gpu.amd.com/{cap}"
        K8Helper.triage(
            environment,
            qualified_cap in capacity,
            f"Partition {device_name}: Missing capacity '{qualified_cap}'",
        )
        if qualified_cap in capacity:
            Logger.info(f"  ✓ Capacity {cap}: {capacity[qualified_cap]}")


def test_dra_driver_device_attributes(dra_driver_install, environment):
    """Test that DRA driver advertises all required device attributes
    
    Validates attributes documented in:
    https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md
    """
    global Logger
    
    # Wait a bit for ResourceSlices to be created
    time.sleep(15)
    
    # Get all ResourceSlices
    # kubectl equivalent: kubectl get resourceslices.resource.k8s.io -o yaml
    resource_slices = get_resource_slices()
    K8Helper.triage(
        environment,
        len(resource_slices) > 0,
        "No ResourceSlices found in cluster",
    )
    
    Logger.info(f"Found {len(resource_slices)} ResourceSlice(s) in cluster")
    
    # Extract AMD GPU devices
    amd_devices = get_amd_gpu_devices_from_slices(resource_slices)
    K8Helper.triage(
        environment,
        len(amd_devices) > 0,
        "No AMD GPU devices found in ResourceSlices",
    )
    
    Logger.info(f"Found {len(amd_devices)} AMD GPU device(s) in ResourceSlices")
    
    # Track device types
    full_gpu_count = 0
    partition_count = 0
    
    # Validate each device
    for device in amd_devices:
        attributes = device.get("basic", {}).get("attributes", {})
        gpu_attrs = attributes.get("gpu.amd.com", {})
        device_type = gpu_attrs.get("type", "")
        
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


def test_dra_driver_device_naming_convention(dra_driver_install, environment):
    """Test that device names follow the canonical naming convention
    
    Canonical device name: gpu-<cardIndex>-<renderIndex>
    https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md#device-identity-and-naming
    """
    global Logger
    
    time.sleep(10)
    
    resource_slices = get_resource_slices()
    amd_devices = get_amd_gpu_devices_from_slices(resource_slices)
    
    K8Helper.triage(
        environment,
        len(amd_devices) > 0,
        "No AMD GPU devices found for naming validation",
    )
    
    # Validate naming convention for all devices
    name_pattern = r"^gpu-\d+-\d+$"
    
    for device in amd_devices:
        device_name = device.get("name", "")
        attributes = device.get("basic", {}).get("attributes", {})
        gpu_attrs = attributes.get("gpu.amd.com", {})
        
        # Check name matches pattern
        K8Helper.triage(
            environment,
            re.match(name_pattern, device_name) is not None,
            f"Device name '{device_name}' doesn't match pattern 'gpu-<cardIndex>-<renderIndex>'",
        )
        
        # Verify name components match attributes
        if re.match(name_pattern, device_name):
            parts = device_name.split("-")
            name_card_idx = int(parts[1])
            name_render_idx = int(parts[2])
            
            attr_card_idx = gpu_attrs.get("cardIndex")
            attr_render_idx = gpu_attrs.get("renderIndex")
            
            K8Helper.triage(
                environment,
                name_card_idx == attr_card_idx,
                f"Device {device_name}: cardIndex in name ({name_card_idx}) != attribute ({attr_card_idx})",
            )
            
            K8Helper.triage(
                environment,
                name_render_idx == attr_render_idx,
                f"Device {device_name}: renderIndex in name ({name_render_idx}) != attribute ({attr_render_idx})",
            )
            
            Logger.info(f"✓ Device {device_name} naming convention validated")


def test_dra_driver_device_capacity_values(dra_driver_install, environment):
    """Test that device capacity values are properly set
    
    Validates capacity attributes:
    - memory (quantity, bytes): VRAM size
    - computeUnits (quantity): Number of CUs
    - simdUnits (quantity): Number of SIMD units
    """
    global Logger
    
    time.sleep(10)
    
    resource_slices = get_resource_slices()
    amd_devices = get_amd_gpu_devices_from_slices(resource_slices)
    
    K8Helper.triage(
        environment,
        len(amd_devices) > 0,
        "No AMD GPU devices found for capacity validation",
    )
    
    for device in amd_devices:
        device_name = device.get("name", "")
        capacity = device.get("basic", {}).get("capacity", {})
        
        # Check all three capacity values exist and are non-zero
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
        
        Logger.info(f"✓ Device {device_name} capacity values:")
        Logger.info(f"    memory: {memory}")
        Logger.info(f"    computeUnits: {compute_units}")
        Logger.info(f"    simdUnits: {simd_units}")


def test_dra_driver_partition_device_correlation(dra_driver_install, environment):
    """Test that partition devices are properly correlated to parent GPU
    
    Partitions should share the same deviceID as their parent GPU.
    https://github.com/ROCm/k8s-gpu-dra-driver/blob/main/docs/driver-attributes.md#attributes-for-a-partition
    """
    global Logger
    
    time.sleep(10)
    
    resource_slices = get_resource_slices()
    amd_devices = get_amd_gpu_devices_from_slices(resource_slices)
    
    # Separate full GPUs and partitions
    full_gpus = []
    partitions = []
    
    for device in amd_devices:
        attributes = device.get("basic", {}).get("attributes", {})
        gpu_attrs = attributes.get("gpu.amd.com", {})
        device_type = gpu_attrs.get("type", "")
        
        if device_type == "amdgpu":
            full_gpus.append(device)
        elif device_type == "amdgpu-partition":
            partitions.append(device)
    
    if len(partitions) == 0:
        Logger.info("No partitions found - skipping partition correlation test")
        pytest.skip("No GPU partitions found in cluster")
        return
    
    Logger.info(f"Found {len(full_gpus)} full GPU(s) and {len(partitions)} partition(s)")
    
    # Build a map of deviceID -> full GPU
    device_id_map = {}
    for gpu in full_gpus:
        attributes = gpu.get("basic", {}).get("attributes", {})
        gpu_attrs = attributes.get("gpu.amd.com", {})
        device_id = gpu_attrs.get("deviceID", "")
        if device_id:
            device_id_map[device_id] = gpu
    
    # Validate each partition correlates to a parent GPU
    for partition in partitions:
        partition_name = partition.get("name", "")
        attributes = partition.get("basic", {}).get("attributes", {})
        gpu_attrs = attributes.get("gpu.amd.com", {})
        
        partition_device_id = gpu_attrs.get("deviceID", "")
        partition_pci_addr = gpu_attrs.get("pciAddr", "")
        
        # Check if parent GPU exists
        if partition_device_id in device_id_map:
            parent_gpu = device_id_map[partition_device_id]
            parent_attrs = parent_gpu.get("basic", {}).get("attributes", {}).get("gpu.amd.com", {})
            parent_pci = parent_attrs.get("pciAddr", "")
            
            # Verify PCI address matches parent
            K8Helper.triage(
                environment,
                partition_pci_addr == parent_pci,
                f"Partition {partition_name}: PCI address mismatch with parent",
            )
            
            Logger.info(f"✓ Partition {partition_name} correlated to parent GPU with deviceID={partition_device_id}")
        else:
            Logger.warning(f"Partition {partition_name}: No parent GPU found with deviceID={partition_device_id}")
