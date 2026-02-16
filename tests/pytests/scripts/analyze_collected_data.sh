#!/bin/bash

# Helper script to analyze collected GPU data
#
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
# Licensed under the Apache License, Version 2.0
#
# Usage: ./analyze_collected_data.sh [data-directory]
# Default directory: ./node-gpu-data

set -e

DATA_DIR="${1:-./node-gpu-data}"

if [ ! -d "$DATA_DIR" ]; then
    echo "Error: Directory '$DATA_DIR' not found"
    echo "Usage: $0 [data-directory]"
    echo "Example: $0 ./node-gpu-data"
    exit 1
fi

# Check if jq is available
if ! command -v jq &> /dev/null; then
    echo "Error: jq is not installed"
    echo "Install with: sudo apt-get install jq"
    exit 1
fi

JSON_FILES=($DATA_DIR/*.json)
if [ ${#JSON_FILES[@]} -eq 0 ] || [ ! -f "${JSON_FILES[0]}" ]; then
    echo "Error: No JSON files found in $DATA_DIR"
    exit 1
fi

echo "==================================================================="
echo "GPU Data Analysis"
echo "==================================================================="
echo "Data directory: $DATA_DIR"
echo "Files found: ${#JSON_FILES[@]}"
echo

# 1. Summary for all nodes
echo "==================================================================="
echo "1. Summary for All Nodes"
echo "==================================================================="
for f in $DATA_DIR/*.json; do
    echo "Node: $(basename $f -gpu-data.json)"
    jq '.summary' $f
    echo
done

# 2. Partition profiles
echo "==================================================================="
echo "2. Partition Profiles by Node"
echo "==================================================================="
for f in $DATA_DIR/*.json; do
    NODE=$(basename $f -gpu-data.json)
    PROFILES=$(jq -r '.summary.partition_profiles | join(", ")' $f)
    if [ -n "$PROFILES" ] && [ "$PROFILES" != "null" ]; then
        echo "Node $NODE: $PROFILES"
    else
        echo "Node $NODE: No partitions"
    fi
done
echo

# 3. Hardware vs ResourceSlice comparison
echo "==================================================================="
echo "3. Hardware vs ResourceSlice GPU Count Comparison"
echo "==================================================================="
ALL_MATCH=true
for f in $DATA_DIR/*.json; do
    NODE=$(basename $f -gpu-data.json)
    HW=$(jq '.summary.hardware_gpu_count' $f)
    DRA=$(jq '.summary.dra_full_gpu_count' $f)

    # Validate that values are numeric before comparison
    if [[ "$HW" =~ ^[0-9]+$ ]] && [[ "$DRA" =~ ^[0-9]+$ ]]; then
        if [ "$HW" -eq "$DRA" ]; then
            echo "✓ Node $NODE: HW=$HW, DRA=$DRA (match)"
        else
            echo "✗ Node $NODE: HW=$HW, DRA=$DRA (MISMATCH!)"
            ALL_MATCH=false
        fi
    else
        echo "✗ Node $NODE: HW=$HW, DRA=$DRA (INVALID DATA - collection may have failed)"
        ALL_MATCH=false
    fi
done

if $ALL_MATCH; then
    echo ""
    echo "✓ All nodes: GPU counts match between hardware and DRA"
else
    echo ""
    echo "⚠ Warning: Some nodes have mismatched GPU counts"
fi
echo

# 4. ROCm availability
echo "==================================================================="
echo "4. ROCm Tool Availability"
echo "==================================================================="
for f in $DATA_DIR/*.json; do
    NODE=$(basename $f -gpu-data.json)
    ROCM_SMI=$(jq '.summary.rocm_smi_available' $f)
    AMD_SMI=$(jq '.summary.amd_smi_available' $f)

    ROCM_STATUS="✗"
    AMD_STATUS="✗"
    [ "$ROCM_SMI" = "true" ] && ROCM_STATUS="✓"
    [ "$AMD_SMI" = "true" ] && AMD_STATUS="✓"

    echo "Node $NODE: rocm-smi=$ROCM_STATUS, amd-smi=$AMD_STATUS"
done
echo

# 5. Device IDs
echo "==================================================================="
echo "5. Unique GPU Device IDs Across Cluster"
echo "==================================================================="
for f in $DATA_DIR/*.json; do
    jq -r '.hardware.gpus[].device_id' $f
done | sort -u | while read device_id; do
    # Get GPU series for this device ID
    GPU_SERIES=$(for f in $DATA_DIR/*.json; do
        jq -r ".hardware.gpus[] | select(.device_id == \"$device_id\") | .gpu_series" $f
    done | head -1)

    echo "Device ID: $device_id - Series: $GPU_SERIES"
done
echo

# 6. Node details
echo "==================================================================="
echo "6. Node Details"
echo "==================================================================="
for f in $DATA_DIR/*.json; do
    NODE=$(jq -r '.node.name' $f)
    OS=$(jq -r '.node.os_info.os_image' $f)
    KERNEL=$(jq -r '.node.os_info.kernel_version' $f)
    GPUS=$(jq '.summary.hardware_gpu_count' $f)

    echo "Node: $NODE"
    echo "  OS: $OS"
    echo "  Kernel: $KERNEL"
    echo "  GPUs: $GPUS"
    echo
done

echo "==================================================================="
echo "Analysis Complete!"
echo "==================================================================="
echo
echo "To view raw ROCm SMI output for a node (with actual newlines):"
echo "  jq -r '.rocm.rocm_smi.output' $DATA_DIR/<node-name>-gpu-data.json"
echo
echo "To view raw AMD SMI output:"
echo "  jq -r '.rocm.amd_smi.output' $DATA_DIR/<node-name>-gpu-data.json"
echo
echo "For more analysis options, see: scripts/README_COLLECT_NODE_DATA.md"
echo
