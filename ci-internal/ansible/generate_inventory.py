#!/usr/bin/env python3
"""
Generate Ansible inventory file from JSON configuration.

Usage:
    ./generate_inventory.py -i input.json -o inventory.ini
    cat input.json | ./generate_inventory.py -o inventory.ini
"""

import json
import sys
import argparse
from typing import Dict, List, Any


def generate_inventory(config: Dict[str, Any]) -> str:
    """
    Generate Ansible inventory content from configuration.

    Args:
        config: Configuration dictionary with testbed details

    Returns:
        Inventory file content as string
    """
    name = config.get("name", "k8s-cluster")
    deployment = config.get("deployment", "k8")
    instances = config.get("instances", [])

    # Separate instances by type
    masters = [inst for inst in instances if inst.get("type") == "master"]
    workers = [inst for inst in instances if inst.get("type") == "worker"]

    inventory_lines = []

    # Header comment
    inventory_lines.append(f"# Ansible Inventory for {name}")
    inventory_lines.append(f"# Deployment type: {deployment}")
    inventory_lines.append(f"# Generated from JSON configuration")
    inventory_lines.append("")

    # Master nodes section
    inventory_lines.append("[master]")
    if masters:
        for idx, master in enumerate(masters):
            hostname = f"{name}-master-{idx+1:02d}"
            ip = master.get("ip")
            username = master.get("username", "root")
            password = master.get("password")

            host_line = f"{hostname} ansible_host={ip} ansible_user={username}"

            if password:
                host_line += f" ansible_password={password}"
                # If user is not root and has password, assume sudoer with same password
                if username != "root":
                    host_line += f" ansible_become=yes ansible_become_method=sudo ansible_become_password={password}"

            # Add registry info if present
            if master.get("registry", "").lower() in ["yes", "true", "1"]:
                host_line += " is_registry=true"

            inventory_lines.append(host_line)
    else:
        inventory_lines.append("# No master nodes defined")

    inventory_lines.append("")

    # Worker nodes section
    inventory_lines.append("[workers]")
    if workers:
        for idx, worker in enumerate(workers):
            hostname = f"{name}-worker-{idx+1:02d}"
            ip = worker.get("ip")
            username = worker.get("username", "root")
            password = worker.get("password")

            host_line = f"{hostname} ansible_host={ip} ansible_user={username}"

            if password:
                host_line += f" ansible_password={password}"
                # If user is not root and has password, assume sudoer with same password
                if username != "root":
                    host_line += f" ansible_become=yes ansible_become_method=sudo ansible_become_password={password}"

            # Add GPU info if present
            if "gpu_series" in worker:
                host_line += f" gpu_series={worker['gpu_series']}"
            if "gpu_count" in worker:
                host_line += f" gpu_count={worker['gpu_count']}"

            inventory_lines.append(host_line)
    else:
        inventory_lines.append("# No worker nodes defined")

    inventory_lines.append("")

    # Group variables
    inventory_lines.append("[all:vars]")
    inventory_lines.append("# SSH connection settings")
    inventory_lines.append("ansible_ssh_common_args='-o StrictHostKeyChecking=no'")
    inventory_lines.append("# Python interpreter - auto-detect with fallback")
    inventory_lines.append("# If auto_silent fails, manually set to /usr/bin/python3 or /usr/bin/python")
    inventory_lines.append("ansible_python_interpreter=auto")
    inventory_lines.append("")
    inventory_lines.append("# Privilege escalation (users are sudoers)")
    inventory_lines.append("# Individual host vars override these if needed")
    inventory_lines.append("# ansible_become=yes")
    inventory_lines.append("# ansible_become_method=sudo")
    inventory_lines.append("")
    inventory_lines.append("# Kubernetes settings")
    inventory_lines.append("k8s_version=1.29.0")
    inventory_lines.append("pod_network_cidr=10.244.0.0/16")
    inventory_lines.append("service_cidr=10.96.0.0/12")
    inventory_lines.append("network_plugin=calico")
    inventory_lines.append("")
    inventory_lines.append("# Deployment type")
    inventory_lines.append(f"deployment_type={deployment}")
    inventory_lines.append(f"testbed_name={name}")

    return "\n".join(inventory_lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Generate Ansible inventory from JSON configuration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example JSON format:
{
  "name": "mi200-testbed",
  "deployment": "k8",
  "instances": [
    {
      "ip": "10.11.78.80",
      "type": "master",
      "username": "vm",
      "password": "vm",
      "registry": "yes"
    },
    {
      "ip": "10.11.130.28",
      "type": "worker",
      "username": "vm",
      "password": "vm",
      "gpu_series": "MI200",
      "gpu_count": 1
    }
  ]
}

Example usage:
  ./generate_inventory.py -i config.json -o inventory.ini
  cat config.json | ./generate_inventory.py -o inventory.ini
  ./generate_inventory.py -i config.json  # Print to stdout
        """
    )

    parser.add_argument(
        "-i", "--input",
        help="Input JSON file (default: read from stdin)",
        type=str
    )

    parser.add_argument(
        "-o", "--output",
        help="Output inventory file (default: print to stdout)",
        type=str
    )

    parser.add_argument(
        "--pretty",
        help="Pretty print the inventory with extra spacing",
        action="store_true"
    )

    args = parser.parse_args()

    # Read input JSON
    try:
        if args.input:
            with open(args.input, 'r') as f:
                config = json.load(f)
        else:
            # Read from stdin
            config = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON format: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: Input file '{args.input}' not found", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error reading input: {e}", file=sys.stderr)
        sys.exit(1)

    # Validate required fields
    if "instances" not in config:
        print("Error: 'instances' field is required in JSON", file=sys.stderr)
        sys.exit(1)

    # Generate inventory
    inventory_content = generate_inventory(config)

    # Write output
    try:
        if args.output:
            with open(args.output, 'w') as f:
                f.write(inventory_content)
            print(f"Inventory file generated: {args.output}")
        else:
            # Print to stdout
            print(inventory_content, end='')
    except Exception as e:
        print(f"Error writing output: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
