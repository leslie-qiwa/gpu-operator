# Inventory Generator

Python script to automatically generate Ansible inventory files from JSON configuration.

## Features

- ✅ Generate inventory from JSON input
- ✅ Support for master and worker nodes
- ✅ Password-based authentication (with sudo support)
- ✅ SSH key-based authentication
- ✅ **Automatic sudoer configuration** for non-root users
- ✅ GPU metadata (series, count)
- ✅ Registry marking for masters
- ✅ Read from file or stdin
- ✅ Output to file or stdout

## Quick Start

### 1. Create JSON configuration

```json
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
```

### 2. Generate inventory

```bash
# Generate from file
./generate_inventory.py -i config.json -o inventory.ini

# Preview output (stdout)
./generate_inventory.py -i config.json

# From stdin
cat config.json | ./generate_inventory.py -o inventory.ini

# From stdin to stdout
echo '{"name":"test","instances":[...]}' | ./generate_inventory.py
```

### 3. Use with Ansible

```bash
# Test connectivity
ansible -i inventory.ini all -m ping

# Install Kubernetes
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"
```

## JSON Configuration Format

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `instances` | array | List of node configurations |

### Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | "k8s-cluster" | Testbed/cluster name |
| `deployment` | string | "k8" | Deployment type (k8/oc) |

### Instance Fields

#### Common Fields (All Nodes)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ip` | string | ✅ Yes | Node IP address |
| `type` | string | ✅ Yes | Node type: "master" or "worker" |
| `username` | string | No | SSH username (default: "root"). If not "root", user is assumed to be a sudoer |
| `password` | string | No | SSH password and sudo password (users are sudoers) |

#### Master Node Specific

| Field | Type | Description |
|-------|------|-------------|
| `registry` | string | Set to "yes"/"true"/"1" to mark as registry node |

#### Worker Node Specific

| Field | Type | Description |
|-------|------|-------------|
| `gpu_series` | string | GPU series (e.g., "MI200", "MI300X") |
| `gpu_count` | integer | Number of GPUs on the node |

## Examples

### Example 1: Sudoer Users with Password (Common Use Case)

**Input** (`simple.json`):
```json
{
  "name": "dev-cluster",
  "deployment": "k8",
  "instances": [
    {
      "ip": "192.168.1.10",
      "type": "master",
      "username": "ubuntu",
      "password": "MySecurePass"
    },
    {
      "ip": "192.168.1.11",
      "type": "worker",
      "username": "ubuntu",
      "password": "MySecurePass",
      "gpu_series": "MI200",
      "gpu_count": 2
    }
  ]
}
```

**Command**:
```bash
./generate_inventory.py -i simple.json -o inventory.ini
```

**Output**:
```ini
# Ansible Inventory for dev-cluster
# Deployment type: k8
# Generated from JSON configuration

[master]
dev-cluster-master-01 ansible_host=192.168.1.10 ansible_user=ubuntu ansible_password=MySecurePass ansible_become=yes ansible_become_method=sudo ansible_become_password=MySecurePass

[workers]
dev-cluster-worker-01 ansible_host=192.168.1.11 ansible_user=ubuntu ansible_password=MySecurePass ansible_become=yes ansible_become_method=sudo ansible_become_password=MySecurePass gpu_series=MI200 gpu_count=2

[all:vars]
# SSH connection settings
ansible_ssh_common_args='-o StrictHostKeyChecking=no'
ansible_python_interpreter=/usr/bin/python3

# Privilege escalation (users are sudoers)
# Individual host vars override these if needed
# ansible_become=yes
# ansible_become_method=sudo

# Kubernetes settings
k8s_version=1.29.0
pod_network_cidr=10.244.0.0/16
service_cidr=10.96.0.0/12
network_plugin=calico

# Deployment type
deployment_type=k8
testbed_name=dev-cluster
```

**Note**: Non-root users are automatically configured with sudo settings. The script assumes:
- Users are sudoers
- Sudo password is the same as SSH password

### Example 2: Multi-Node with Passwords

**Input** (`multi_node.json`):
```json
{
  "name": "mi300-production",
  "deployment": "k8",
  "instances": [
    {
      "ip": "10.11.78.80",
      "type": "master",
      "username": "ubuntu",
      "password": "SecurePass123",
      "registry": "yes"
    },
    {
      "ip": "10.11.130.28",
      "type": "worker",
      "username": "ubuntu",
      "password": "SecurePass123",
      "gpu_series": "MI300X",
      "gpu_count": 8
    },
    {
      "ip": "10.11.130.29",
      "type": "worker",
      "username": "ubuntu",
      "password": "SecurePass123",
      "gpu_series": "MI300X",
      "gpu_count": 8
    }
  ]
}
```

**Command**:
```bash
./generate_inventory.py -i multi_node.json -o inventory.ini
```

### Example 3: Using with Pipe

```bash
# From API or script output
curl -s https://api.example.com/testbed/config | ./generate_inventory.py -o inventory.ini

# From environment variable
echo $TESTBED_CONFIG | ./generate_inventory.py -o inventory.ini

# Chain with jq for filtering
cat all_configs.json | jq '.testbeds[0]' | ./generate_inventory.py -o inventory.ini
```

### Example 4: SSH Key Authentication (Passwordless)

For SSH key-based auth, omit the `password` field:

```json
{
  "name": "secure-cluster",
  "deployment": "k8",
  "instances": [
    {
      "ip": "10.0.1.10",
      "type": "master",
      "username": "ubuntu"
    },
    {
      "ip": "10.0.1.11",
      "type": "worker",
      "username": "ubuntu",
      "gpu_series": "MI200",
      "gpu_count": 4
    }
  ]
}
```

Then specify SSH key in the generated inventory or via command line:
```bash
# Add to inventory.ini under [all:vars]
ansible_ssh_private_key_file=~/.ssh/id_rsa

# For passwordless sudo, also add:
ansible_become=yes
ansible_become_method=sudo
# No ansible_become_password needed if user has NOPASSWD sudo

# Or via command line
ansible-playbook -i inventory.ini install-k8s.yml --private-key=~/.ssh/id_rsa
```

### Example 5: Root User (No Sudo Needed)

When using root user, no sudo configuration is needed:

```json
{
  "name": "root-cluster",
  "deployment": "k8",
  "instances": [
    {
      "ip": "10.0.1.10",
      "type": "master",
      "username": "root",
      "password": "RootPass123"
    }
  ]
}
```

Generated inventory (no ansible_become settings):
```ini
[master]
root-cluster-master-01 ansible_host=10.0.1.10 ansible_user=root ansible_password=RootPass123
```

## Integration with K8s Installation

Complete workflow example:

```bash
# 1. Generate inventory
./generate_inventory.py -i testbed.json -o inventory.ini

# 2. Test connectivity
ansible -i inventory.ini all -m ping

# 3. Install Kubernetes 1.30
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"

# 4. Verify cluster
export KUBECONFIG=./kubeconfig-1.30.0
kubectl get nodes

# 5. Deploy GPU Operator
helm install amd-gpu-operator rocm/gpu-operator-charts \
  --namespace kube-amd-gpu \
  --create-namespace
```

## Script Options

```
usage: generate_inventory.py [-h] [-i INPUT] [-o OUTPUT] [--pretty]

Generate Ansible inventory from JSON configuration

optional arguments:
  -h, --help            show this help message and exit
  -i INPUT, --input INPUT
                        Input JSON file (default: read from stdin)
  -o OUTPUT, --output OUTPUT
                        Output inventory file (default: print to stdout)
  --pretty              Pretty print the inventory with extra spacing
```

## Testing

Test the script with the provided examples:

```bash
# Test with simple example
./generate_inventory.py -i example_config.json

# Test with multi-node
./generate_inventory.py -i example_multi_node.json

# Test stdin
cat example_config.json | ./generate_inventory.py

# Generate actual inventory
./generate_inventory.py -i example_config.json -o test_inventory.ini

# Verify with ansible
ansible -i test_inventory.ini all -m ping
```

## Error Handling

The script validates:
- ✅ Valid JSON format
- ✅ Required `instances` field present
- ✅ File existence (if -i used)
- ✅ Write permissions (if -o used)

Error examples:

```bash
# Invalid JSON
echo '{invalid}' | ./generate_inventory.py
# Error: Invalid JSON format: ...

# Missing instances field
echo '{"name":"test"}' | ./generate_inventory.py
# Error: 'instances' field is required in JSON

# File not found
./generate_inventory.py -i nonexistent.json
# Error: Input file 'nonexistent.json' not found
```

## Advanced Usage

### Dynamic Configuration

Generate configs programmatically:

```python
#!/usr/bin/env python3
import json
import subprocess

# Generate config dynamically
config = {
    "name": "auto-cluster",
    "deployment": "k8",
    "instances": []
}

# Add master
config["instances"].append({
    "ip": "10.0.1.10",
    "type": "master",
    "username": "ubuntu"
})

# Add workers
for i in range(1, 4):
    config["instances"].append({
        "ip": f"10.0.1.{10+i}",
        "type": "worker",
        "username": "ubuntu",
        "gpu_series": "MI300X",
        "gpu_count": 8
    })

# Generate inventory
proc = subprocess.Popen(
    ["./generate_inventory.py", "-o", "inventory.ini"],
    stdin=subprocess.PIPE,
    text=True
)
proc.communicate(json.dumps(config))
```

### Customizing Generated Inventory

After generation, you can manually edit the inventory to:
- Add additional variables
- Modify network settings
- Change Kubernetes version
- Add groups

Example modifications to `[all:vars]`:
```ini
[all:vars]
ansible_ssh_common_args='-o StrictHostKeyChecking=no'
ansible_python_interpreter=/usr/bin/python3

# Custom Kubernetes settings
k8s_version=1.31.0
pod_network_cidr=10.10.0.0/16
service_cidr=10.20.0.0/12
network_plugin=cilium

# Custom deployment settings
deployment_type=k8
testbed_name=mi200-testbed
enable_monitoring=true
install_dashboard=true
```

## Troubleshooting

### Permission Issues

```bash
# Make script executable
chmod +x generate_inventory.py

# Check Python version (requires 3.6+)
python3 --version
```

### Password in Inventory

**Security Note**: The generated inventory includes passwords in plain text. To secure:

1. **Use SSH keys instead** (omit password from JSON)
2. **Use Ansible Vault**:
   ```bash
   # Encrypt the inventory
   ansible-vault encrypt inventory.ini

   # Use with playbook
   ansible-playbook -i inventory.ini install-k8s.yml --ask-vault-pass
   ```
3. **Use ansible-vault for passwords**:
   ```bash
   # Create encrypted variable
   ansible-vault encrypt_string 'vm' --name 'ansible_password'
   ```

### JSON Format Validation

Validate JSON before generating:

```bash
# Using jq
cat config.json | jq . > /dev/null && echo "Valid JSON"

# Using Python
python3 -m json.tool config.json
```

## See Also

- [Main README](README.md) - Kubernetes installation guide
- [install-k8s.yml](install-k8s.yml) - Installation playbook
- [uninstall-k8s.yml](uninstall-k8s.yml) - Uninstallation playbook
- [upgrade-k8s.yml](upgrade-k8s.yml) - Upgrade playbook

## License

Apache License 2.0
