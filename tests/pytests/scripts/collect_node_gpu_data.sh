#!/bin/bash

# Example usage script for collect_node_gpu_data.py
#
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
# Licensed under the Apache License, Version 2.0

set -e

echo "==================================================================="
echo "Node GPU Data Collection - Example Usage"
echo "==================================================================="
echo

# Check if kubectl is available
if ! command -v kubectl &> /dev/null; then
    echo "ERROR: kubectl is not installed or not in PATH"
    exit 1
fi

# Auto-change to pytests directory for consistent working directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PYTESTS_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"

echo "Running from: $PYTESTS_DIR"
cd "$PYTESTS_DIR"
echo

# Get list of GPU nodes
echo "Finding GPU nodes in cluster..."
GPU_NODES=$(kubectl get nodes -l feature.node.kubernetes.io/amd-gpu=true -o jsonpath='{.items[*].metadata.name}')

if [ -z "$GPU_NODES" ]; then
    echo "No GPU nodes found with label 'feature.node.kubernetes.io/amd-gpu=true'"
    echo
    echo "Trying to find nodes with AMD GPUs using different labels..."
    GPU_NODES=$(kubectl get nodes -l feature.node.kubernetes.io/amd-vgpu=true -o jsonpath='{.items[*].metadata.name}')
fi

if [ -z "$GPU_NODES" ]; then
    echo "No GPU nodes found. Please check your cluster."
    exit 1
fi

echo "Found GPU nodes: $GPU_NODES"
echo

# Create output directory
OUTPUT_DIR="./node-gpu-data"
mkdir -p "$OUTPUT_DIR"

# Collect data from each GPU node
for node in $GPU_NODES; do
    echo "-------------------------------------------------------------------"
    echo "Collecting data from node: $node"
    echo "-------------------------------------------------------------------"

    output_file="$OUTPUT_DIR/${node}-gpu-data.json"

    # Run collection script from pytests directory
    python3 scripts/collect_node_gpu_data.py "$node" --output "$output_file" --pretty

    echo
    echo "✓ Data saved to: $output_file"
    echo

    # Show summary
    echo "Summary for $node:"
    jq '.summary' "$output_file" 2>/dev/null || echo "  (jq not installed - cannot show summary)"
    echo
done

echo "==================================================================="
echo "Collection Complete!"
echo "==================================================================="
echo
echo "All data saved to: $OUTPUT_DIR/"
echo
echo "-------------------------------------------------------------------"
echo "Quick jq Tips - Viewing Collected Data"
echo "-------------------------------------------------------------------"
echo
echo "View summaries:"
echo "  for f in $OUTPUT_DIR/*.json; do"
echo "    echo \"Node: \$(basename \$f -gpu-data.json)\""
echo "    jq '.summary' \$f"
echo "  done"
echo
echo "Show partition profiles:"
echo "  for f in $OUTPUT_DIR/*.json; do"
echo "    echo \"Node: \$(basename \$f -gpu-data.json)\""
echo "    jq '.summary.partition_profiles' \$f"
echo "  done"
echo
echo "Compare hardware vs ResourceSlice GPU counts:"
echo "  for f in $OUTPUT_DIR/*.json; do"
echo "    NODE=\$(basename \$f -gpu-data.json)"
echo "    HW=\$(jq '.summary.hardware_gpu_count' \$f)"
echo "    DRA=\$(jq '.summary.dra_full_gpu_count' \$f)"
echo "    echo \"Node \$NODE: HW=\$HW, DRA=\$DRA\""
echo "  done"
echo
echo "View ROCm SMI output (use -r for proper newlines!):"
echo "  jq -r '.rocm.rocm_smi.output' $OUTPUT_DIR/<node-name>-gpu-data.json"
echo
echo "View AMD SMI output (use -r for proper newlines!):"
echo "  jq -r '.rocm.amd_smi.output' $OUTPUT_DIR/<node-name>-gpu-data.json"
echo
echo "Extract all unique device IDs:"
echo "  for f in $OUTPUT_DIR/*.json; do"
echo "    jq '.hardware.gpus[].device_id' \$f"
echo "  done | sort -u"
echo
echo "Check if all nodes have ROCm installed:"
echo "  for f in $OUTPUT_DIR/*.json; do"
echo "    NODE=\$(basename \$f -gpu-data.json)"
echo "    ROCM=\$(jq '.summary.rocm_smi_available' \$f)"
echo "    echo \"Node \$NODE: rocm-smi=\$ROCM\""
echo "  done"
echo
echo "-------------------------------------------------------------------"
echo "For more jq examples, see: scripts/README_COLLECT_NODE_DATA.md"
echo "  Section: 'Using jq to View Output'"
echo "-------------------------------------------------------------------"
echo
