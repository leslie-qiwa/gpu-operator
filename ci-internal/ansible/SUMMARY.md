# Ansible Kubernetes Deployment - Summary

## 📋 Overview

Complete Ansible automation for Kubernetes cluster deployment on AMD GPU nodes with **automatic sudoer configuration** from JSON input.

## 🎯 Key Features

✅ **Kubernetes versions 1.29 - 1.35**  
✅ **JSON-to-Inventory automation** with sudoer support  
✅ **Automatic sudo configuration** for non-root users  
✅ **Master + Workers** architecture  
✅ **GPU metadata** tracking (series, count)  
✅ **3 CNI options**: Calico, Flannel, Cilium  
✅ **Password & SSH key** authentication  
✅ **Complete uninstall** capability  
✅ **Rolling upgrades**  

## 📁 Files Generated

```
ci-internal/ansible/
├── ansible.cfg                    # Ansible configuration
├── generate_inventory.py          # JSON to inventory generator (with sudo support)
├── install-k8s.yml                # Install K8s playbook
├── uninstall-k8s.yml              # Uninstall K8s playbook
├── upgrade-k8s.yml                # Upgrade K8s playbook
├── tasks/
│   └── install-containerd.yml     # Containerd installation
├── inventory.ini.example          # Manual inventory template
├── example_config.json            # Example: sudoer users
├── example_multi_node.json        # Example: multi-node with sudoers
├── example_root_user.json         # Example: root users
├── README.md                      # Main documentation
├── INVENTORY_GENERATOR.md         # Generator reference
├── CREDENTIALS.md                 # Sudoer & credential guide
└── QUICKSTART.md                  # Quick start guide
```

## 🚀 Quick Usage

### 1. Create JSON (Users are Sudoers)

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

### 2. Generate Inventory (Automatic Sudo Setup)

```bash
./generate_inventory.py -i config.json -o inventory.ini
```

**Generated inventory includes**:
- `ansible_user=vm` - SSH username
- `ansible_password=vm` - SSH password
- `ansible_become=yes` - Enable sudo
- `ansible_become_method=sudo` - Use sudo
- `ansible_become_password=vm` - Sudo password

### 3. Install Kubernetes

```bash
# Test connectivity
ansible -i inventory.ini all -m ping

# Install K8s 1.30
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"

# Access cluster
export KUBECONFIG=./kubeconfig-1.30.0
kubectl get nodes
```

## 🔐 Sudoer Support

### How It Works

The inventory generator automatically detects non-root users and configures sudo:

| Username | Sudo Config | Why |
|----------|-------------|-----|
| `root` | ❌ No sudo settings | Root doesn't need sudo |
| Any other | ✅ Sudo settings added | Non-root users are sudoers |

**Assumptions**:
- Non-root users have sudo privileges
- Sudo password = SSH password
- Sudo requires password (not NOPASSWD)

### Example: Sudoer User

**Input JSON**:
```json
{"ip": "10.0.1.10", "type": "master", "username": "ubuntu", "password": "pass123"}
```

**Generated inventory**:
```ini
host ansible_host=10.0.1.10 ansible_user=ubuntu ansible_password=pass123 ansible_become=yes ansible_become_method=sudo ansible_become_password=pass123
```

### Example: Root User

**Input JSON**:
```json
{"ip": "10.0.1.10", "type": "master", "username": "root", "password": "rootpass"}
```

**Generated inventory** (no sudo settings):
```ini
host ansible_host=10.0.1.10 ansible_user=root ansible_password=rootpass
```

## 📚 Documentation

| File | Purpose |
|------|---------|
| **CREDENTIALS.md** | Complete guide on sudoers, passwords, SSH keys, security |
| **INVENTORY_GENERATOR.md** | Inventory generator reference with examples |
| **README.md** | Main documentation with all features |
| **QUICKSTART.md** | Step-by-step getting started guide |
| **SUMMARY.md** | This file - overview and quick reference |

## 🔧 Prerequisites on Nodes

### For Non-Root Users (Sudoers)

The user must be a sudoer:

```bash
# On Ubuntu/Debian
sudo usermod -aG sudo vm

# On RHEL/CentOS  
sudo usermod -aG wheel vm

# Verify
sudo -l -U vm
```

### For Root Users

Enable root login:

```bash
# Edit SSH config
sudo vi /etc/ssh/sshd_config

# Set
PermitRootLogin yes

# Restart
sudo systemctl restart sshd
```

## 💡 Common Use Cases

### Use Case 1: Development (Sudoer Users)

```bash
# JSON with sudoer users
cat > dev.json << 'JSON'
{
  "name": "dev",
  "instances": [
    {"ip": "10.0.1.10", "type": "master", "username": "ubuntu", "password": "pass"},
    {"ip": "10.0.1.11", "type": "worker", "username": "ubuntu", "password": "pass", "gpu_series": "MI200", "gpu_count": 1}
  ]
}
JSON

# Generate and install
./generate_inventory.py -i dev.json -o inventory.ini
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.29.0"
```

### Use Case 2: Production (Multiple Workers)

```bash
# JSON with multiple GPU workers
cat > prod.json << 'JSON'
{
  "name": "prod",
  "instances": [
    {"ip": "10.0.1.10", "type": "master", "username": "admin", "password": "SecurePass"},
    {"ip": "10.0.1.11", "type": "worker", "username": "admin", "password": "SecurePass", "gpu_series": "MI300X", "gpu_count": 8},
    {"ip": "10.0.1.12", "type": "worker", "username": "admin", "password": "SecurePass", "gpu_series": "MI300X", "gpu_count": 8}
  ]
}
JSON

# Generate and install with Cilium
./generate_inventory.py -i prod.json -o inventory.ini
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0" -e "network_plugin=cilium"
```

### Use Case 3: Root Users Only

```bash
# JSON with root users
cat > root.json << 'JSON'
{
  "name": "root-cluster",
  "instances": [
    {"ip": "10.0.1.10", "type": "master", "username": "root", "password": "RootPass"},
    {"ip": "10.0.1.11", "type": "worker", "username": "root", "password": "RootPass", "gpu_series": "MI200", "gpu_count": 2}
  ]
}
JSON

# Generate (no sudo settings for root)
./generate_inventory.py -i root.json -o inventory.ini
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.31.0"
```

## 🔒 Security Notes

⚠️ **Passwords are in plain text** in the generated inventory.

**Recommendations**:
1. Use SSH keys instead (omit `password` from JSON)
2. Encrypt inventory with `ansible-vault encrypt inventory.ini`
3. Set restrictive permissions: `chmod 600 inventory.ini`
4. Don't commit inventory files to git
5. Use temporary credentials, then rotate

See [CREDENTIALS.md](CREDENTIALS.md) for detailed security guidance.

## 🛠️ Operations

### Install
```bash
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"
```

### Upgrade
```bash
ansible-playbook -i inventory.ini upgrade-k8s.yml -e "k8s_version=1.31.0"
```

### Uninstall
```bash
ansible-playbook -i inventory.ini uninstall-k8s.yml
```

### Test
```bash
ansible -i inventory.ini all -m ping
ansible -i inventory.ini all -m command -a "whoami" --become
```

## ✅ Verification

After installation:

```bash
# Set kubeconfig
export KUBECONFIG=./kubeconfig-1.30.0

# Check nodes
kubectl get nodes

# Check system pods
kubectl get pods -A

# Check GPU labels (on worker nodes)
kubectl get nodes -o json | jq '.items[].metadata.labels' | grep gpu
```

## 📞 Support

- [Main README](README.md) - Full documentation
- [CREDENTIALS.md](CREDENTIALS.md) - Sudoer setup and security
- [INVENTORY_GENERATOR.md](INVENTORY_GENERATOR.md) - Generator details
- [GPU Operator Docs](https://instinct.docs.amd.com/projects/gpu-operator)

## 🎓 Examples Included

| File | Description |
|------|-------------|
| `example_config.json` | Your exact use case - sudoer users |
| `example_multi_node.json` | Multi-worker with sudoers |
| `example_root_user.json` | Root users (no sudo config) |

All examples can be tested:
```bash
./generate_inventory.py -i example_config.json
./generate_inventory.py -i example_multi_node.json
./generate_inventory.py -i example_root_user.json
```

## 🎯 Key Takeaway

**Non-root usernames are automatically configured as sudoers** - just provide username and password in JSON, and the script handles the rest!

```json
{"username": "vm", "password": "vm"} 
→ Generates: ansible_become=yes ansible_become_method=sudo ansible_become_password=vm
```

No manual configuration needed! 🎉
