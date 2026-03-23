# 🚀 START HERE - Kubernetes Ansible Automation

## 🎯 What This Does

Automatically deploy Kubernetes (versions 1.29-1.35) on AMD GPU nodes using:
1. **JSON configuration** with node credentials
2. **Automatic inventory generation** with sudoer support
3. **Ansible playbooks** for install/upgrade/uninstall

## ⚡ Quick Start (3 Steps)

### 1️⃣ Create your JSON config

```bash
cat > my-cluster.json << 'EOF'
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
