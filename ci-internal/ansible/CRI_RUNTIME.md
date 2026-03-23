# Container Runtime Interface (CRI) Selection

## Overview

The playbooks now support **automatic CRI socket detection** with preference order:
1. **Containerd** (first preference)
2. **CRI-O** (second preference)
3. Auto-detect (if neither found)

## Why This Matters

Nodes may have multiple container runtimes installed (containerd and cri-o). Kubeadm needs to know which one to use via the `--cri-socket` flag. Without explicit selection, kubeadm may:
- Fail with "multiple CRI sockets detected" error
- Use the wrong runtime
- Behave inconsistently across nodes

## How It Works

### Detection Logic

On each node (master and workers):

```yaml
1. Check if /run/containerd/containerd.sock exists
   ├─ YES → Use containerd ✅
   └─ NO  → Check if /var/run/crio/crio.sock exists
       ├─ YES → Use cri-o ✅
       └─ NO  → Auto-detect (let kubeadm decide)
```

### CRI Socket Paths

| Runtime | Socket Path | Priority |
|---------|------------|----------|
| Containerd | `/run/containerd/containerd.sock` | 1st (preferred) |
| CRI-O | `/var/run/crio/crio.sock` | 2nd |

## Installation Behavior

### Master Node

```yaml
- name: Check if containerd socket exists
  stat:
    path: /run/containerd/containerd.sock
  register: containerd_socket

- name: Check if crio socket exists
  stat:
    path: /var/run/crio/crio.sock
  register: crio_socket

- name: Determine CRI socket (prefer containerd)
  set_fact:
    cri_socket: "{{ '/run/containerd/containerd.sock' if containerd_socket.stat.exists else ('/var/run/crio/crio.sock' if crio_socket.stat.exists else '') }}"

- name: Initialize Kubernetes cluster with containerd
  shell: |
    kubeadm init \
      --kubernetes-version={{ k8s_version }} \
      --pod-network-cidr={{ pod_network_cidr }} \
      --service-cidr={{ service_cidr }} \
      --cri-socket={{ cri_socket }} \
      --upload-certs
  when: cri_socket != ''
```

### Worker Nodes

```yaml
- name: Determine CRI socket (prefer containerd)
  set_fact:
    cri_socket: "{{ '/run/containerd/containerd.sock' if containerd_socket.stat.exists else ('/var/run/crio/crio.sock' if crio_socket.stat.exists else '') }}"

- name: Add CRI socket to join command
  shell: |
    sed -i 's|kubeadm join|kubeadm join --cri-socket={{ cri_socket }}|' /tmp/k8s-join-command.sh
  when: cri_socket != ''

- name: Join worker node to cluster
  shell: bash /tmp/k8s-join-command.sh
```

## Uninstallation Behavior

During `kubeadm reset`, the same CRI detection logic applies:

```yaml
- name: Determine CRI socket (prefer containerd)
  set_fact:
    cri_socket: "{{ '/run/containerd/containerd.sock' if containerd_socket.stat.exists else ('/var/run/crio/crio.sock' if crio_socket.stat.exists else '') }}"

- name: Reset kubeadm with CRI socket
  shell: kubeadm reset -f --cri-socket={{ cri_socket }}
  when: cri_socket != ''
```

Both runtimes are stopped and cleaned up:

```yaml
- name: Stop and disable containerd
  systemd:
    name: containerd
    state: stopped
    enabled: no

- name: Stop and disable crio
  systemd:
    name: crio
    state: stopped
    enabled: no

- name: Remove containerd directories
  file:
    path: "{{ item }}"
    state: absent
  loop:
    - /etc/containerd
    - /var/lib/containerd
    - /run/containerd

- name: Remove crio directories
  file:
    path: "{{ item }}"
    state: absent
  loop:
    - /etc/crio
    - /var/lib/crio
    - /var/run/crio
```

## Examples

### Scenario 1: Node with Containerd Only

```bash
# Detection on master:
Checking: /run/containerd/containerd.sock → EXISTS ✅
Using CRI socket: /run/containerd/containerd.sock

# Kubeadm init command:
kubeadm init --cri-socket=/run/containerd/containerd.sock ...

# On worker:
kubeadm join --cri-socket=/run/containerd/containerd.sock ...
```

### Scenario 2: Node with CRI-O Only

```bash
# Detection on master:
Checking: /run/containerd/containerd.sock → NOT FOUND
Checking: /var/run/crio/crio.sock → EXISTS ✅
Using CRI socket: /var/run/crio/crio.sock

# Kubeadm init command:
kubeadm init --cri-socket=/var/run/crio/crio.sock ...

# On worker:
kubeadm join --cri-socket=/var/run/crio/crio.sock ...
```

### Scenario 3: Node with Both Containerd and CRI-O

```bash
# Detection on master:
Checking: /run/containerd/containerd.sock → EXISTS ✅
Using CRI socket: /run/containerd/containerd.sock (preferred!)

# CRI-O is ignored even if present
```

### Scenario 4: Node with Neither (Fresh Install)

```bash
# Detection on master:
Checking: /run/containerd/containerd.sock → NOT FOUND
Checking: /var/run/crio/crio.sock → NOT FOUND
Using CRI socket: auto-detect

# Kubeadm init command (no --cri-socket flag):
kubeadm init --kubernetes-version=1.30.0 ...

# This is fine because install-k8s.yml installs containerd first
```

## Verification

### Check which CRI is running

```bash
# On each node:

# Check containerd
systemctl status containerd
ls -la /run/containerd/containerd.sock

# Check cri-o
systemctl status crio
ls -la /var/run/crio/crio.sock
```

### Check which CRI Kubernetes is using

```bash
# On master:
kubectl get nodes -o wide

# Or check kubelet config:
grep containerRuntimeEndpoint /var/lib/kubelet/config.yaml

# Example output:
containerRuntimeEndpoint: unix:///run/containerd/containerd.sock
```

### Verify during installation

The playbook will display which CRI socket is selected:

```
TASK [Display selected CRI socket] *************************************
ok: [master] => {
    "msg": "Using CRI socket: /run/containerd/containerd.sock"
}
```

## Troubleshooting

### Issue: "multiple CRI sockets detected"

**Cause**: Both containerd and cri-o are running, and kubeadm auto-detect is confused.

**Solution**: The playbooks now handle this automatically by preferring containerd.

### Issue: Wrong CRI selected

**Cause**: The preferred CRI (containerd) is not running, so cri-o is used.

**Solution**: Start containerd:

```bash
systemctl start containerd
systemctl enable containerd
```

Then reinstall:

```bash
ansible-playbook -i inventory.ini uninstall-k8s.yml
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"
```

### Issue: Neither CRI found during install

**Cause**: Fresh install before containerd is installed.

**Solution**: This is normal. The playbook installs containerd first, then initializes K8s.

### Issue: Want to use CRI-O instead of containerd

**Option 1**: Remove containerd before running playbook:

```bash
systemctl stop containerd
systemctl disable containerd
apt remove -y containerd.io containerd  # or yum remove
```

**Option 2**: Modify the playbook to prefer cri-o:

```yaml
# Change detection logic:
cri_socket: "{{ '/var/run/crio/crio.sock' if crio_socket.stat.exists else ('/run/containerd/containerd.sock' if containerd_socket.stat.exists else '') }}"
```

### Check CRI socket in running cluster

```bash
# On any node:
crictl info | grep -i socket

# Or:
kubectl get node <node-name> -o jsonpath='{.status.nodeInfo.containerRuntimeVersion}'
```

## Manual Override

If you need to force a specific CRI socket, modify the playbook:

```yaml
# In install-k8s.yml, master section:
- name: Force specific CRI socket
  set_fact:
    cri_socket: "/var/run/crio/crio.sock"  # Force CRI-O

# Then run:
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"
```

## Best Practices

1. **Use one CRI per cluster** - Don't mix containerd and cri-o across nodes
2. **Prefer containerd** - Better upstream support, more widely used
3. **Clean install** - Remove old runtimes before installing new ones
4. **Verify after install** - Check that all nodes use the same CRI

## Summary

✅ **Containerd is preferred** over CRI-O  
✅ **Automatic detection** on all nodes (master + workers)  
✅ **Explicit --cri-socket** flag used in kubeadm commands  
✅ **Both runtimes cleaned up** during uninstall  
✅ **Consistent across install/uninstall/upgrade**  
✅ **No manual intervention** needed for most cases  

**Your workflow**: Just run the playbooks - CRI selection is automatic! 🎉
