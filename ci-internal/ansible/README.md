# Kubernetes Cluster Management with Ansible

This directory contains Ansible playbooks for installing, upgrading, and uninstalling Kubernetes clusters (versions 1.29 to 1.35) on a set of nodes.

**Key Features**:
- ✅ Automatic CRI socket selection (containerd preferred, cri-o fallback)
- ✅ Automatic cert-manager installation
- ✅ Automatic sudoer configuration from JSON
- ✅ Support for Kubernetes 1.29 - 1.35
- ✅ Multiple CNI options (Calico, Flannel, Cilium)

## Inventory Generator

**New**: Automatically generate inventory files from JSON! See [INVENTORY_GENERATOR.md](INVENTORY_GENERATOR.md)

```bash
# Create inventory from JSON
./generate_inventory.py -i config.json -o inventory.ini

# Example JSON format (users are sudoers)
{
  "name": "mi200-testbed",
  "instances": [
    {"ip": "10.11.78.80", "type": "master", "username": "vm", "password": "vm"},
    {"ip": "10.11.130.28", "type": "worker", "username": "vm", "password": "vm", "gpu_series": "MI200", "gpu_count": 1}
  ]
}
```

**Note**: Non-root usernames are automatically configured as sudoers. The password is used for both SSH and sudo authentication. See [CREDENTIALS.md](CREDENTIALS.md) for details.

## Prerequisites

1. **Ansible installed** on your control machine:
   ```bash
   # Ubuntu/Debian
   sudo apt update && sudo apt install ansible -y

   # RHEL/CentOS
   sudo yum install ansible -y

   # Using pip
   pip install ansible
   ```

2. **SSH access** to all target nodes (master and workers)

3. **Root or sudo access** on all target nodes
   - For non-root users: User must be a sudoer
   - See [CREDENTIALS.md](CREDENTIALS.md) for setup details

4. **Supported OS**: Ubuntu 20.04+, Debian 11+ (including Debian 12 bookworm, Debian 13 trixie), RHEL/CentOS 8+

## Quick Start

### 1. Setup Inventory

Copy the example inventory file and configure your nodes:

```bash
cd ci-internal/ansible
cp inventory.ini.example inventory.ini
```

Edit `inventory.ini` with your node information:

```ini
[master]
k8s-master ansible_host=192.168.1.10 ansible_user=root

[workers]
k8s-worker-01 ansible_host=192.168.1.11 ansible_user=root
k8s-worker-02 ansible_host=192.168.1.12 ansible_user=root

[all:vars]
ansible_ssh_private_key_file=~/.ssh/id_rsa
k8s_version=1.29.0
pod_network_cidr=10.244.0.0/16
service_cidr=10.96.0.0/12
network_plugin=calico
```

### 2. Test Connectivity

```bash
ansible -i inventory.ini all -m ping
```

### 3. Install Kubernetes

Install Kubernetes with default version (1.29.0) and cert-manager:

```bash
ansible-playbook -i inventory.ini install-k8s.yml
```

Install a specific Kubernetes version:

```bash
# K8s 1.30 with cert-manager (default)
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"

# K8s 1.31 without cert-manager
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.31.0" -e "install_certmanager=false"

# K8s 1.32
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.32.0"

# K8s 1.33
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.33.0"

# K8s 1.34
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.34.0"

# K8s 1.35
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.35.0"
```

**Note**: By default, cert-manager v1.15.1 is installed (required for GPU Operator). To skip cert-manager, use `-e "install_certmanager=false"`.

After successful installation, the kubeconfig file will be saved to `./kubeconfig-<version>`.

### 4. Verify Installation

```bash
export KUBECONFIG=./kubeconfig-1.29.0
kubectl get nodes
kubectl get pods -A
```

## Supported Kubernetes Versions

- 1.29.x
- 1.30.x
- 1.31.x
- 1.32.x
- 1.33.x
- 1.34.x
- 1.35.x

## Configuration Options

### Network Plugins

Set `network_plugin` in inventory.ini or via command line:

```bash
# Calico (default)
ansible-playbook -i inventory.ini install-k8s.yml -e "network_plugin=calico"

# Flannel
ansible-playbook -i inventory.ini install-k8s.yml -e "network_plugin=flannel"

# Cilium
ansible-playbook -i inventory.ini install-k8s.yml -e "network_plugin=cilium"
```

### Network CIDR

Customize pod and service network ranges:

```bash
ansible-playbook -i inventory.ini install-k8s.yml \
  -e "pod_network_cidr=10.244.0.0/16" \
  -e "service_cidr=10.96.0.0/12"
```

### Multiple Options

Combine multiple parameters:

```bash
ansible-playbook -i inventory.ini install-k8s.yml \
  -e "k8s_version=1.31.0" \
  -e "network_plugin=cilium" \
  -e "pod_network_cidr=10.10.0.0/16"
```

## Upgrade Kubernetes

Upgrade an existing cluster to a newer version:

```bash
# Upgrade to K8s 1.30
ansible-playbook -i inventory.ini upgrade-k8s.yml -e "k8s_version=1.30.0"

# Upgrade to K8s 1.31
ansible-playbook -i inventory.ini upgrade-k8s.yml -e "k8s_version=1.31.0"
```

**Important Notes**:
- Only upgrade one minor version at a time (e.g., 1.29 → 1.30 → 1.31)
- The upgrade process drains nodes one at a time to minimize disruption
- Always backup your cluster before upgrading

## Uninstall GPU Operator

Remove GPU Operator without affecting Kubernetes:

```bash
ansible-playbook -i inventory.ini uninstall-gpu-operator.yml
```

This will:
- Uninstall GPU Operator helm release
- Wait for GPU operator pods to terminate
- Delete kube-amd-gpu namespace

## Uninstall Kubernetes

Completely remove Kubernetes from all nodes:

```bash
# Optional: Remove GPU Operator first (if installed)
ansible-playbook -i inventory.ini uninstall-gpu-operator.yml

# Then remove Kubernetes
ansible-playbook -i inventory.ini uninstall-k8s.yml
```

This will:
- Remove cert-manager (if installed)
- Drain and delete all nodes from the cluster
- Reset kubeadm on all nodes
- Remove all Kubernetes packages and configuration
- Clean up CNI networks and iptables rules
- Remove containerd runtime
- Clean up all related directories

**Warning**: This is a destructive operation and cannot be undone!

**Note**: GPU Operator should be removed first if installed. The uninstall-k8s.yml playbook focuses on Kubernetes and cert-manager cleanup.

## Playbook Files

| File | Description |
|------|-------------|
| `install-k8s.yml` | Install Kubernetes cluster from scratch |
| `uninstall-k8s.yml` | Completely remove Kubernetes from all nodes |
| `uninstall-gpu-operator.yml` | Remove GPU Operator (separate from K8s) |
| `upgrade-k8s.yml` | Upgrade existing cluster to a new version |
| `tasks/install-containerd.yml` | Install and configure containerd runtime |
| `inventory.ini.example` | Example inventory file |
| `ansible.cfg` | Ansible configuration file |

## Advanced Usage

### Run on specific hosts

```bash
# Install only on master
ansible-playbook -i inventory.ini install-k8s.yml --limit master

# Install only on specific workers
ansible-playbook -i inventory.ini install-k8s.yml --limit k8s-worker-01,k8s-worker-02
```

### Dry run (check mode)

```bash
ansible-playbook -i inventory.ini install-k8s.yml --check
```

### Verbose output

```bash
ansible-playbook -i inventory.ini install-k8s.yml -v   # verbose
ansible-playbook -i inventory.ini install-k8s.yml -vv  # more verbose
ansible-playbook -i inventory.ini install-k8s.yml -vvv # very verbose
```

### Use password authentication

```bash
# Prompt for SSH password
ansible-playbook -i inventory.ini install-k8s.yml --ask-pass

# Prompt for sudo password
ansible-playbook -i inventory.ini install-k8s.yml --ask-become-pass

# Both
ansible-playbook -i inventory.ini install-k8s.yml --ask-pass --ask-become-pass
```

## Troubleshooting

### SSH Connection Issues

```bash
# Test SSH connection
ansible -i inventory.ini all -m ping

# Use different user
ansible-playbook -i inventory.ini install-k8s.yml -u ubuntu

# Use different SSH key
ansible-playbook -i inventory.ini install-k8s.yml --private-key=~/.ssh/mykey.pem
```

### Cleanup failed installation

If installation fails, run the uninstall playbook first:

```bash
ansible-playbook -i inventory.ini uninstall-k8s.yml
```

Then retry the installation.

### View cluster logs

```bash
# On master node
journalctl -u kubelet -f

# View kubeadm logs
journalctl -u kubeadm -f

# Check containerd
journalctl -u containerd -f
```

### Common Issues

**Issue**: Nodes not becoming Ready
- Check network plugin installation: `kubectl get pods -n kube-system`
- Verify containerd is running: `systemctl status containerd`
- Check kubelet logs: `journalctl -u kubelet`

**Issue**: Worker nodes not joining
- Verify join command: `cat /tmp/k8s-join-command.sh`
- Check network connectivity between nodes
- Ensure firewall allows Kubernetes ports

**Issue**: Package version not found
- Verify the k8s_version is valid
- Check if version is available in Kubernetes repositories
- Update repository cache: `apt update` or `yum update`

## Post-Installation

### Deploy GPU Operator

After Kubernetes is installed, you can deploy the AMD GPU Operator:

```bash
export KUBECONFIG=./kubeconfig-1.29.0

# Add Helm repo
helm repo add rocm https://rocm.github.io/gpu-operator
helm repo update

# Install cert-manager (prerequisite)
helm repo add jetstack https://charts.jetstack.io --force-update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version v1.15.1 \
  --set crds.enabled=true

# Install GPU Operator
helm install amd-gpu-operator rocm/gpu-operator-charts \
  --namespace kube-amd-gpu \
  --create-namespace \
  --version=v1.4.0
```

### Access cluster from local machine

```bash
# Copy kubeconfig to default location
cp ./kubeconfig-1.29.0 ~/.kube/config

# Or export KUBECONFIG
export KUBECONFIG=$(pwd)/kubeconfig-1.29.0

# Verify access
kubectl get nodes
```

## Examples

### Example 1: Fresh K8s 1.30 cluster with Cilium

```bash
ansible-playbook -i inventory.ini install-k8s.yml \
  -e "k8s_version=1.30.0" \
  -e "network_plugin=cilium"
```

### Example 2: Upgrade from 1.29 to 1.30

```bash
ansible-playbook -i inventory.ini upgrade-k8s.yml -e "k8s_version=1.30.0"
```

### Example 3: Complete cleanup and reinstall

```bash
# Uninstall
ansible-playbook -i inventory.ini uninstall-k8s.yml

# Wait a moment
sleep 30

# Reinstall
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.31.0"
```

## Support

For issues or questions:
- File an issue on the [GPU Operator GitHub](https://github.com/ROCm/gpu-operator/issues)
- Check the [documentation](https://instinct.docs.amd.com/projects/gpu-operator)

## License

Apache License 2.0
