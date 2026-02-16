# Node GPU Data Collection - Complete Guide

## Overview

This directory provides scripts for collecting comprehensive GPU information from Kubernetes nodes, including hardware detection, ROCm tools, partition profiles, and ResourceSlice information.

### Available Scripts

**1. collect_node_gpu_data.py** - Python script for single node collection
**2. collect_node_gpu_data.sh** - Shell script to collect from all GPU nodes automatically
**3. analyze_collected_data.sh** - Analyze and summarize collected JSON data

## Quick Start

```bash
cd tests/pytests

# Collect from one node
python3 scripts/collect_node_gpu_data.py <node-name> --pretty

# Or collect from all GPU nodes automatically
./scripts/collect_node_gpu_data.sh
```

### ⚠️ Important: Working Directory

**Always run from `tests/pytests/` directory**

The shell script automatically changes to the correct directory. When running the Python script directly, run from the pytests directory for correct Python module imports.

---

## What Gets Collected

The scripts gather data from 4 sources:

1. **Hardware Info (lspci)**: GPU devices, PCI addresses, device IDs, GPU series
2. **ROCm Info**: rocm-smi and amd-smi output
3. **Partition Info**: GPU partition profiles from sysfs
4. **ResourceSlices**: Advertised GPU devices from Kubernetes ResourceSlices

---

## Usage

### Basic Usage - Python Script

```bash
cd tests/pytests

# Collect data for a node
python3 scripts/collect_node_gpu_data.py worker-node-1

# Save to file
python3 scripts/collect_node_gpu_data.py worker-node-1 --output node-data.json

# Pretty print JSON
python3 scripts/collect_node_gpu_data.py worker-node-1 --pretty

# Save with pretty printing
python3 scripts/collect_node_gpu_data.py worker-node-1 --output node-data.json --pretty
```

### Automated Collection - Shell Script

```bash
cd tests/pytests

# Collect from all GPU nodes automatically
./scripts/collect_node_gpu_data.sh

# This automatically:
# - Finds all GPU nodes in cluster
# - Collects data from each
# - Saves to organized directory (node-gpu-data/)
# - Shows summary for each node
# - Displays jq tips for viewing data
```

### Analyze Collected Data

```bash
cd tests/pytests

# After collecting data, analyze it
./scripts/analyze_collected_data.sh

# This shows:
# - Summaries for all nodes
# - Partition profiles
# - Hardware vs DRA GPU count comparison
# - ROCm tool availability
# - Unique device IDs
# - Node details
```

### Advanced Options

```bash
cd tests/pytests

# Skip specific collections for faster execution
python3 scripts/collect_node_gpu_data.py worker-node-1 --skip-rocm --skip-partition --pretty

# Only collect hardware and DRA info
python3 scripts/collect_node_gpu_data.py worker-node-1 --skip-rocm --skip-partition --pretty

# Only collect DRA info (fastest)
python3 scripts/collect_node_gpu_data.py worker-node-1 --skip-hardware --skip-rocm --skip-partition --pretty

# Use custom kubeconfig
python3 scripts/collect_node_gpu_data.py worker-node-1 --kubeconfig /path/to/config --pretty
```

### Help

```bash
python3 scripts/collect_node_gpu_data.py --help
```

---

## Output Format

### JSON Structure

```json
{
  "node": {
    "name": "worker-node-1",
    "labels": { "feature.node.kubernetes.io/amd-gpu": "true", ... },
    "capacity": { "cpu": "64", "memory": "512Gi", "amd.com/gpu": "8" },
    "allocatable": { ... },
    "os_info": {
      "os_image": "Ubuntu 22.04.3 LTS",
      "kernel_version": "6.11.0",
      "kubelet_version": "v1.32.0"
    }
  },
  "hardware": {
    "method": "lspci",
    "gpus": [
      {
        "pci_address": "83:00.0",
        "device_id": "74a1",
        "device_type": "Processing accelerators",
        "description": "Advanced Micro Devices, Inc. [AMD/ATI] Aqua Vanjaram [Instinct MI300X]",
        "gpu_series": "MI300X"
      }
    ],
    "total_gpus": 8,
    "device_ids": ["74a1"]
  },
  "rocm": {
    "rocm_smi": {
      "available": true,
      "output": "GPU[0]    : GPU ID: 0x740f\nTemperature: 35C\n..."
    },
    "amd_smi": {
      "available": true,
      "output": "GPU: 0\n  Product Name: MI300X\n  Memory: 192GB\n..."
    }
  },
  "partition": {
    "sysfs_files": [
      {
        "path": "/sys/class/drm/card0/device/partition_profile",
        "content": "spx_8gb"
      }
    ],
    "partition_profiles": ["spx_8gb"]
  },
  "dra_advertised": {
    "full_gpus": [
      {
        "name": "gpu-0-128",
        "type": "amdgpu",
        "attributes": {
          "type": "amdgpu",
          "pciAddr": "0000:83:00.0",
          "cardIndex": 0,
          "renderIndex": 128,
          "deviceID": "0x74a1",
          "family": "aqua_vanjaram",
          "productName": "Instinct MI300X",
          "driverVersion": "6.11.0"
        },
        "capacity": {
          "gpu.amd.com/memory": "192Gi",
          "gpu.amd.com/computeUnits": "304",
          "gpu.amd.com/simdUnits": "4864"
        }
      }
    ],
    "partitions": [
      {
        "name": "gpu-0-129",
        "type": "amdgpu-partition",
        "attributes": {
          "partitionProfile": "spx_8gb",
          ...
        }
      }
    ]
  },
  "summary": {
    "hardware_gpu_count": 8,
    "dra_full_gpu_count": 8,
    "dra_partition_count": 16,
    "partition_profiles": ["spx_8gb"],
    "rocm_smi_available": true,
    "amd_smi_available": true
  },
  "collected_at": "2026-02-16 10:30:00 UTC"
}
```

---

## Using jq to View Output

### The Key: Use `-r` for Multi-Line Text

JSON strings with embedded `\n` need the `-r` flag to display properly:

```bash
# ❌ Without -r (shows \n literally)
jq '.rocm.rocm_smi.output' node-data.json
# Output: "GPU[0]: ID 0x740f\nTemperature: 35C\n"

# ✅ With -r (actual newlines)
jq -r '.rocm.rocm_smi.output' node-data.json
# Output:
# GPU[0]: ID 0x740f
# Temperature: 35C
```

### Common jq Queries

```bash
# Summary (no -r needed)
jq '.summary' node.json

# Partition profiles as array (no -r)
jq '.summary.partition_profiles' node.json

# Partition profiles one per line (-r needed)
jq -r '.summary.partition_profiles[]' node.json

# ROCm SMI output with proper newlines (-r needed!)
jq -r '.rocm.rocm_smi.output' node.json

# AMD SMI output with proper newlines (-r needed!)
jq -r '.rocm.amd_smi.output' node.json

# Hardware GPUs (no -r needed)
jq '.hardware.gpus' node.json

# Formatted hardware GPUs (-r for custom format)
jq -r '.hardware.gpus[] | "\(.pci_address): \(.gpu_series) (\(.device_id))"' node.json

# Compare hardware vs DRA counts
jq '{hw: .summary.hardware_gpu_count, dra: .summary.dra_full_gpu_count}' node.json

# Check tool availability
jq '{rocm_smi: .summary.rocm_smi_available, amd_smi: .summary.amd_smi_available}' node.json
```

### Quick Reference: When to Use `-r`

| Field | Use `-r`? | Why |
|-------|-----------|-----|
| `.summary` | No | Structured data |
| `.summary.partition_profiles` | No (array) / Yes (each) | Array vs individual items |
| `.rocm.rocm_smi.output` | **YES** | Multi-line text with `\n` |
| `.rocm.amd_smi.output` | **YES** | Multi-line text with `\n` |
| `.partition.sysfs_files[].content` | **YES** | Text content |
| `.hardware.gpus` | No | Structured data |
| `.dra_advertised` | No | Structured data |

---

## Usage Examples

### Example 1: Get Partition Profiles

```bash
cd tests/pytests

./scripts/collect_node_gpu_data.sh

# Extract profiles from collected data
for f in node-gpu-data/*.json; do
  echo "Node: $(basename $f -gpu-data.json)"
  jq '.summary.partition_profiles' $f
done
```

### Example 2: Compare Hardware vs DRA

```bash
cd tests/pytests

python3 scripts/collect_node_gpu_data.py worker-node-1 --output node.json --pretty

echo "Hardware GPUs: $(jq '.summary.hardware_gpu_count' node.json)"
echo "ResourceSlice GPUs: $(jq '.summary.dra_full_gpu_count' node.json)"
echo "ResourceSlice Partitions: $(jq '.summary.dra_partition_count' node.json)"
```

### Example 3: View ROCm SMI Output (Human-Readable)

```bash
cd tests/pytests

python3 scripts/collect_node_gpu_data.py worker-node-1 --output node.json --pretty

# View with actual newlines
jq -r '.rocm.rocm_smi.output' node.json

# Or search for specific GPU
jq -r '.rocm.rocm_smi.output' node.json | grep "GPU\[3\]"
```

### Example 4: Collect from All Nodes and Compare

```bash
cd tests/pytests

# Collect from all nodes
./scripts/collect_node_gpu_data.sh

# Compare all summaries
for f in node-gpu-data/*.json; do
  echo "=== $(basename $f .json) ==="
  jq '.summary' $f
  echo
done

# Check for GPU count mismatches
for f in node-gpu-data/*.json; do
  HW=$(jq '.summary.hardware_gpu_count' $f)
  DRA=$(jq '.summary.dra_full_gpu_count' $f)
  if [ "$HW" != "$DRA" ]; then
    echo "Mismatch in $(basename $f): HW=$HW, DRA=$DRA"
  fi
done
```

### Example 5: Extract DRA Attributes

```bash
cd tests/pytests

python3 scripts/collect_node_gpu_data.py worker-node-1 --pretty | \
  jq '.dra_advertised.full_gpus[0].attributes'

# Get all device IDs
python3 scripts/collect_node_gpu_data.py worker-node-1 --pretty | \
  jq '[.hardware.gpus[].device_id] | unique'

# Get all partition profiles
python3 scripts/collect_node_gpu_data.py worker-node-1 --pretty | \
  jq '[.dra_advertised.partitions[].attributes.partitionProfile] | unique'
```

### Example 6: Use in Python Test

```python
#!/usr/bin/env python3
import subprocess
import json
import os

def get_node_partition_profiles(node_name):
    """Get partition profiles for a node using the collection script"""

    # Run from pytests directory
    pytests_dir = "/path/to/tests/pytests"

    result = subprocess.run(
        ["python3", "scripts/collect_node_gpu_data.py", node_name],
        capture_output=True,
        text=True,
        cwd=pytests_dir
    )

    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        return []

    data = json.loads(result.stdout)
    return data['summary']['partition_profiles']

# Use in test
profiles = get_node_partition_profiles("worker-node-1")
if profiles:
    print(f"Testing with profile: {profiles[0]}")
```

### Example 7: View All Data Nicely

Create a viewer script `view_node_data.sh`:

```bash
#!/bin/bash
NODE_FILE="$1"

if [ -z "$NODE_FILE" ]; then
    echo "Usage: $0 <node-data.json>"
    exit 1
fi

echo "=========================================="
echo "Node: $(jq -r '.node.name' $NODE_FILE)"
echo "Collected: $(jq -r '.collected_at' $NODE_FILE)"
echo "=========================================="

echo -e "\nSummary:"
jq '.summary' $NODE_FILE

echo -e "\nHardware GPUs:"
jq -r '.hardware.gpus[] | "  \(.pci_address): \(.gpu_series) (DeviceID: \(.device_id))"' $NODE_FILE

echo -e "\nPartition Profiles:"
jq -r '.summary.partition_profiles[]? // "No partitions"' $NODE_FILE

echo -e "\n=========================================="
echo "ROCm SMI Output:"
echo "=========================================="
jq -r '.rocm.rocm_smi.output // "Not available"' $NODE_FILE

echo -e "\n=========================================="
echo "AMD SMI Static Output (first 50 lines):"
echo "=========================================="
jq -r '.rocm.amd_smi.output // "Not available"' $NODE_FILE | head -50
```

Usage:
```bash
chmod +x view_node_data.sh
./view_node_data.sh node-gpu-data/worker-node-1-gpu-data.json
```

---

## Prerequisites

- kubectl installed and configured
- Access to Kubernetes cluster
- Permissions to create debug pods on nodes
- Python 3.6+
- (Optional) jq for viewing JSON output

---

## Troubleshooting

### FileNotFoundError for amdgpu-features.json

**Error**:

```text
FileNotFoundError: [Errno 2] No such file or directory: 'lib/files/amdgpu-features.json'
```

**Cause**: Running from wrong directory

**Solution**: Run from `tests/pytests/` directory:

```bash
cd tests/pytests  # Correct directory
python3 scripts/collect_node_gpu_data.py node1 --pretty
```

The shell script (`./scripts/collect_node_gpu_data.sh`) handles this automatically.

### kubectl debug fails

**Error**: `error: unable to create a new debug pod on node "worker-node-1"`

**Solution**: Check node is Ready and you have permissions:

```bash
kubectl get nodes
kubectl auth can-i create pods
```

### No GPU nodes found

**Error**: `No GPU nodes found. Please check your cluster.`

**Solution**: Check node labels:

```bash
kubectl get nodes --show-labels | grep amd-gpu
```

If nodes have GPUs but aren't labeled yet, specify the node name directly:

```bash
python3 scripts/collect_node_gpu_data.py <actual-node-name> --pretty
```

### rocm-smi/amd-smi not available

**Not an error!** This is normal if ROCm drivers aren't installed yet.

The script marks them as unavailable and continues:

```json
{
  "rocm_smi_available": false,
  "amd_smi_available": false
}
```

### No partition files found

**Not an error!** Not all GPUs support partitioning.

The script continues with empty partition data:

```json
{
  "partition_profiles": []
}
```

### Script hangs

**Cause**: kubectl debug waiting for input

**Solution**: Test kubectl debug manually:

```bash
kubectl debug node/worker-node-1 -it --image=ubuntu:22.04 -- ls
```

---

## Integration with pytest

### Use in Tests

```python
import subprocess
import json

def test_compare_node_data_with_dra(node_name):
    """Example of using collection script in pytest"""

    # Run the collection script
    result = subprocess.run(
        ["python3", "scripts/collect_node_gpu_data.py", node_name],
        capture_output=True,
        text=True,
        cwd="/path/to/tests/pytests"
    )

    assert result.returncode == 0, f"Script failed: {result.stderr}"

    # Parse output
    data = json.loads(result.stdout)

    # Validate
    assert data['summary']['hardware_gpu_count'] > 0
    assert data['summary']['dra_full_gpu_count'] > 0

    # Use partition profiles
    profiles = data['summary']['partition_profiles']
    if profiles:
        test_partition_allocation(profiles[0])
```

### Pre-collect Data as Fixture

```python
# In conftest.py
@pytest.fixture(scope="session")
def node_gpu_data_cache(environment):
    """Pre-collect node GPU data for all tests"""
    import subprocess
    import json

    cache = {}
    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()

    for node in gpu_nodes:
        node_name = k8_util.k8_get_node_hostname(node)

        result = subprocess.run(
            ["python3", "scripts/collect_node_gpu_data.py", node_name],
            capture_output=True,
            text=True,
            cwd="/path/to/tests/pytests"
        )

        if result.returncode == 0:
            cache[node_name] = json.loads(result.stdout)

    return cache

# Use in tests
def test_with_cached_data(node_gpu_data_cache):
    data = node_gpu_data_cache["worker-node-1"]
    profiles = data["summary"]["partition_profiles"]
```

---

## Code Reuse with pytest Fixtures

The scripts use the same shared code as pytest fixtures!

**Shared module**: `lib/node_gpu_collector.py`

Both use:

- ✅ `k8_util.run_command_on_node()` - Run commands on nodes
- ✅ `node_collector.collect_gpu_hardware_info()` - GPU detection
- ✅ `node_collector.collect_gpu_partition_profiles()` - Partition profiles
- ✅ `amdgpu_util.get_amdgpu_device_series()` - Device series lookup

**Result**: Script data is identical to pytest fixture data!

---

## Performance Notes

- **Full collection**: ~30-60 seconds per node
- **Skip ROCm**: ~20-30 seconds per node (faster)
- **DRA only**: ~5-10 seconds (fastest)

For large clusters:

- Use `./scripts/collect_node_gpu_data.sh` to run in sequence
- Or parallelize manually for multiple nodes
- Use `--skip-rocm` if ROCm info not needed
- Cache results for test session

---

## Related Documentation

- Python test utilities: `../lib/k8_util.py`
- Shared helper module: `../lib/node_gpu_collector.py`
- Test examples: `../k8/dra-driver/test_dra_attribute_validation.py`
