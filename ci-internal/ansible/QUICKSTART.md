# Quick Start Guide

## Complete Workflow: JSON → Inventory → K8s Cluster

### Step 1: Create JSON Configuration

Create `my-cluster.json`:

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

**Important**: The `username` must be a **sudoer** on the target nodes. The `password` is used for both SSH login and sudo authentication. See [CREDENTIALS.md](../ansible/CREDENTIALS.md) for setup details.

### Step 2: Generate Inventory

```bash
./generate_inventory.py -i my-cluster.json -o inventory.ini
```

### Step 3: Test Connectivity

```bash
ansible -i inventory.ini all -m ping
```

### Step 4: Install Kubernetes

```bash
# Install K8s 1.30 with Calico and cert-manager (default)
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"

# Or install K8s 1.31 with Cilium and cert-manager
ansible-playbook -i inventory.ini install-k8s.yml \
  -e "k8s_version=1.31.0" \
  -e "network_plugin=cilium"

# Install without cert-manager (if you want to install it manually later)
ansible-playbook -i inventory.ini install-k8s.yml \
  -e "k8s_version=1.30.0" \
  -e "install_certmanager=false"
```

**Note**: Cert-manager is installed by default (required for GPU Operator).

### Step 5: Access Cluster

```bash
# Set kubeconfig
export KUBECONFIG=./kubeconfig-1.30.0

# Verify nodes
kubectl get nodes

# Check system pods
kubectl get pods -A
```

### Step 6: Deploy GPU Operator

```bash
# Cert-manager is already installed by default
# Just install GPU Operator
helm repo add rocm https://rocm.github.io/gpu-operator
helm install amd-gpu-operator rocm/gpu-operator-charts \
  --namespace kube-amd-gpu \
  --create-namespace \
  --version=v1.4.0
```

**Note**: If you skipped cert-manager installation with `install_certmanager=false`, install it first:

```bash
helm repo add jetstack https://charts.jetstack.io --force-update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version v1.15.1 \
  --set crds.enabled=true
```

### Step 7: Verify GPU Operator

```bash
# Check operator pods
kubectl get pods -n kube-amd-gpu

# Create DeviceConfig (if not auto-created)
kubectl get deviceconfigs -n kube-amd-gpu

# Check GPU nodes
kubectl get nodes -o json | jq '.items[].metadata.labels' | grep gpu
```

## Common Commands

### Cluster Management

```bash
# Get cluster info
kubectl cluster-info

# Get all nodes
kubectl get nodes -o wide

# Describe node
kubectl describe node mi200-testbed-worker-01

# Get all pods across namespaces
kubectl get pods -A

# Check GPU resources
kubectl describe node <worker-node> | grep -A 10 "Capacity"
```

### Upgrade Cluster

```bash
# Upgrade to 1.31
ansible-playbook -i inventory.ini upgrade-k8s.yml -e "k8s_version=1.31.0"
```

### Uninstall Cluster

```bash
# Complete removal
ansible-playbook -i inventory.ini uninstall-k8s.yml
```

## Different Deployment Scenarios

### Scenario 1: Development (1 master + 1 worker)

```json
{
  "name": "dev-cluster",
  "instances": [
    {"ip": "10.0.1.10", "type": "master", "username": "ubuntu"},
    {"ip": "10.0.1.11", "type": "worker", "username": "ubuntu", "gpu_series": "MI200", "gpu_count": 1}
  ]
}
```

```bash
./generate_inventory.py -i dev.json -o inventory.ini
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.29.0"
```

### Scenario 2: Production (1 master + 3 workers)

```json
{
  "name": "prod-cluster",
  "instances": [
    {"ip": "10.0.1.10", "type": "master", "username": "ubuntu", "registry": "yes"},
    {"ip": "10.0.1.11", "type": "worker", "username": "ubuntu", "gpu_series": "MI300X", "gpu_count": 8},
    {"ip": "10.0.1.12", "type": "worker", "username": "ubuntu", "gpu_series": "MI300X", "gpu_count": 8},
    {"ip": "10.0.1.13", "type": "worker", "username": "ubuntu", "gpu_series": "MI300X", "gpu_count": 8}
  ]
}
```

```bash
./generate_inventory.py -i prod.json -o inventory.ini
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0" -e "network_plugin=cilium"
```

### Scenario 3: Mixed GPU Types

```json
{
  "name": "mixed-gpu",
  "instances": [
    {"ip": "10.0.1.10", "type": "master", "username": "root"},
    {"ip": "10.0.1.11", "type": "worker", "username": "root", "gpu_series": "MI200", "gpu_count": 4},
    {"ip": "10.0.1.12", "type": "worker", "username": "root", "gpu_series": "MI300X", "gpu_count": 8}
  ]
}
```

## Troubleshooting

### Connection Issues

```bash
# Test SSH manually
ssh vm@10.11.78.80

# Test with ansible
ansible -i inventory.ini all -m ping -vvv

# Use password authentication
ansible -i inventory.ini all -m ping --ask-pass
```

### Installation Failures

```bash
# Check logs on master
ssh vm@10.11.78.80
journalctl -u kubelet -f

# Clean up and retry
ansible-playbook -i inventory.ini uninstall-k8s.yml
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"
```

### Node Not Ready

```bash
# Check node status
kubectl get nodes
kubectl describe node <node-name>

# Check network plugin
kubectl get pods -n kube-system -l k8s-app=calico-node

# Restart kubelet (on the node)
systemctl restart kubelet
```

## Complete Example

```bash
#!/bin/bash
set -e

# 1. Create cluster config
cat > cluster.json << 'CLUSTER'
{
  "name": "mi200-testbed",
  "deployment": "k8",
  "instances": [
    {"ip": "10.11.78.80", "type": "master", "username": "vm", "password": "vm", "registry": "yes"},
    {"ip": "10.11.130.28", "type": "worker", "username": "vm", "password": "vm", "gpu_series": "MI200", "gpu_count": 1}
  ]
}
CLUSTER

# 2. Generate inventory
./generate_inventory.py -i cluster.json -o inventory.ini

# 3. Test connectivity
ansible -i inventory.ini all -m ping

# 4. Install K8s
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"

# 5. Setup kubeconfig
export KUBECONFIG=./kubeconfig-1.30.0

# 6. Verify cluster
kubectl get nodes
kubectl get pods -A

# 7. Verify cert-manager (installed by default)
kubectl get pods -n cert-manager

# 8. Install GPU operator
helm repo add rocm https://rocm.github.io/gpu-operator
helm install amd-gpu-operator rocm/gpu-operator-charts \
  --namespace kube-amd-gpu \
  --create-namespace \
  --version=v1.4.0

# 9. Wait for GPU operator
kubectl wait --for=condition=ready pod -l app=amd-gpu-operator -n kube-amd-gpu --timeout=300s

echo "Cluster ready!"
```

Save as `deploy.sh` and run:

```bash
chmod +x deploy.sh
./deploy.sh
```

## Next Steps

- [Main README](README.md) - Detailed documentation
- [INVENTORY_GENERATOR.md](INVENTORY_GENERATOR.md) - Inventory generator reference
- [GPU Operator Docs](https://instinct.docs.amd.com/projects/gpu-operator)
