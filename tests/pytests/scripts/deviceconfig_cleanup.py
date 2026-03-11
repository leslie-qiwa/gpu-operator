#!/usr/bin/env python3

"""
Copyright (c) 2024. All rights reserved.

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
Standalone script to force-remove GPU Operator resources and perform a hookless uninstall.

Actions:
1. Identify and patch DeviceConfig to remove finalizers.
2. Delete DeviceConfig resource.
3. Clean up orphaned Jobs and Pods in the operator namespace.
4. Uninstall the GPU Operator via Helm using the --no-hooks flag.

Usage:
    python3 force_cleanup_gpu_operator.py --namespace gpu-operator-resources
"""

import argparse
import subprocess
import sys
import json
from typing import List, Tuple, Optional

def run_command(cmd: List[str]) -> Tuple[int, str, str]:
    """
    Run a system command and return exit code, stdout, and stderr.
    """
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return -1, "", str(e)

def patch_deviceconfig(namespace: str):
    """
    Locates DeviceConfig resources and nullifies finalizers to allow deletion.
    """
    print(f"[*] Checking for DeviceConfig resources in '{namespace}'...", file=sys.stderr)
    
    # Get DeviceConfig name
    cmd = ["kubectl", "get", "deviceconfig", "-n", namespace, "-o", "jsonpath={.items[*].metadata.name}"]
    rc, stdout, stderr = run_command(cmd)
    
    if rc != 0 or not stdout:
        print(f"    No DeviceConfig resources found or namespace does not exist.", file=sys.stderr)
        return

    dc_names = stdout.split()
    for name in dc_names:
        print(f"    Patching finalizers for DeviceConfig: {name}", file=sys.stderr)
        
        # Patch command to remove finalizers
        patch_cmd = [
            "kubectl", "patch", "deviceconfig", name, "-n", namespace,
            "--type", "merge", "-p", '{"metadata":{"finalizers":null}}'
        ]
        p_rc, _, p_err = run_command(patch_cmd)
        
        if p_rc == 0:
            print(f"    Successfully patched {name}. Deleting...", file=sys.stderr)
            run_command(["kubectl", "delete", "deviceconfig", name, "-n", namespace, "--cascade=orphan"])
        else:
            print(f"    Error patching {name}: {p_err}", file=sys.stderr)

def cleanup_leftover_resources(namespace: str):
    """
    Deletes any leftover pods or jobs that may have been created by the operator or cleanup hooks.
    """
    print(f"[*] Cleaning up leftover Jobs and Pods in '{namespace}'...", file=sys.stderr)
    
    # Delete Jobs
    print(f"    Deleting all Jobs...", file=sys.stderr)
    run_command(["kubectl", "delete", "jobs", "-n", namespace, "--all", "--wait=false"])
    
    # Force Delete Pods (Grace period 0)
    print(f"    Force deleting all remaining Pods...", file=sys.stderr)
    run_command(["kubectl", "delete", "pods", "-n", namespace, "--all", "--force", "--grace-period=0"])

def helm_uninstall_no_hooks(release_name: str, namespace: str):
    """
    Uninstalls the GPU operator using helm with the --no-hooks flag.
    """
    print(f"[*] Performing Helm uninstall (no-hooks) for '{release_name}'...", file=sys.stderr)
    
    cmd = ["helm", "uninstall", release_name, "-n", namespace, "--no-hooks"]
    rc, stdout, stderr = run_command(cmd)
    
    if rc == 0:
        print(f"Helm uninstall successful: {stdout}", file=sys.stderr)
    else:
        print(f"Helm uninstall failed (it may already be gone): {stderr}", file=sys.stderr)

def main():
    parser = argparse.ArgumentParser(
        description="Force cleanup GPU Operator resources and uninstall without hooks."
    )
    
    parser.add_argument(
        "-n", "--namespace",
        default="kube-amd-gpu",
        help="Namespace where GPU Operator is installed (default: kube-amd-gpu)"
    )
    
    parser.add_argument(
        "-r", "--release",
        default="gpu-operator",
        help="Helm release name (default: gpu-operator)"
    )
    
    args = parser.parse_args()

    print("=" * 60, file=sys.stderr)
    print(f"Starting GPU Operator force cleanup process", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    patch_deviceconfig(args.namespace)
    
    cleanup_leftover_resources(args.namespace)
    
    helm_uninstall_no_hooks(args.release, args.namespace)

    print("=" * 60, file=sys.stderr)
    print("Cleanup script execution finished.", file=sys.stderr)

if __name__ == "__main__":
    main()