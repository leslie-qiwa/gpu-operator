# Cert-Manager Handling

## Overview

The Ansible playbooks now handle cert-manager installation and removal automatically.

## Why Cert-Manager?

Cert-manager is a **prerequisite for AMD GPU Operator**. It manages TLS certificates for the operator's webhooks and validation.

## Installation Behavior

### Default (Recommended)

By default, `install-k8s.yml` **automatically installs cert-manager v1.15.1**:

```bash
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"
```

This installs:
- Kubernetes 1.30
- CNI plugin (Calico by default)
- **Cert-manager v1.15.1**

### Skip Cert-Manager

If you want to install cert-manager manually later:

```bash
ansible-playbook -i inventory.ini install-k8s.yml \
  -e "k8s_version=1.30.0" \
  -e "install_certmanager=false"
```

## Uninstallation Behavior

`uninstall-k8s.yml` **automatically removes cert-manager** (and GPU Operator if present):

```bash
ansible-playbook -i inventory.ini uninstall-k8s.yml
```

This removes (in order):
1. GPU Operator (if installed)
2. Cert-manager (if installed)
3. Kubernetes cluster
4. All K8s packages and configuration

## Reinstallation Workflow

For clean reinstalls (uninstall → install):

```bash
# 1. Uninstall everything (including cert-manager)
ansible-playbook -i inventory.ini uninstall-k8s.yml

# 2. Reinstall K8s with cert-manager
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"

# 3. Install GPU Operator
helm repo add rocm https://rocm.github.io/gpu-operator
helm install amd-gpu-operator rocm/gpu-operator-charts \
  --namespace kube-amd-gpu \
  --create-namespace \
  --version=v1.4.0
```

**No manual cert-manager cleanup needed!** ✅

## What Gets Installed

When `install_certmanager=true` (default):

```bash
# Helm is installed if not present
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Jetstack repo added
helm repo add jetstack https://charts.jetstack.io --force-update

# Cert-manager installed
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version v1.15.1 \
  --set crds.enabled=true
```

## What Gets Removed

When running `uninstall-k8s.yml`:

```bash
# GPU Operator removed
helm uninstall amd-gpu-operator -n kube-amd-gpu
kubectl delete namespace kube-amd-gpu

# Cert-manager removed
helm uninstall cert-manager -n cert-manager
kubectl delete namespace cert-manager
```

## Verification

### After Installation

Check cert-manager is running:

```bash
export KUBECONFIG=./kubeconfig-1.30.0
kubectl get pods -n cert-manager
```

Expected output:
```
NAME                                      READY   STATUS    RESTARTS   AGE
cert-manager-7d9f8c6d4f-xxxxx            1/1     Running   0          2m
cert-manager-cainjector-5f9f8c6d4f-xxxxx 1/1     Running   0          2m
cert-manager-webhook-5f9f8c6d4f-xxxxx    1/1     Running   0          2m
```

### After Uninstallation

Verify cert-manager is removed:

```bash
kubectl get namespace cert-manager
# Should return: Error from server (NotFound): namespaces "cert-manager" not found
```

## Configuration Options

| Variable | Default | Description |
|----------|---------|-------------|
| `install_certmanager` | `true` | Install cert-manager during K8s installation |

### Examples

```bash
# Default: Install with cert-manager
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"

# Skip cert-manager
ansible-playbook -i inventory.ini install-k8s.yml \
  -e "k8s_version=1.30.0" \
  -e "install_certmanager=false"

# Install with Cilium and cert-manager
ansible-playbook -i inventory.ini install-k8s.yml \
  -e "k8s_version=1.31.0" \
  -e "network_plugin=cilium"
```

## Troubleshooting

### Cert-Manager Not Ready

If cert-manager pods are not ready:

```bash
# Check pod status
kubectl get pods -n cert-manager

# Check logs
kubectl logs -n cert-manager deploy/cert-manager

# Restart cert-manager
kubectl rollout restart deployment -n cert-manager
```

### Reinstall Failed

If reinstall fails due to leftover cert-manager:

```bash
# Manually clean up
kubectl delete namespace cert-manager --force --grace-period=0
kubectl delete crd --all

# Then retry install
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"
```

### Check Cert-Manager Version

```bash
kubectl get deployment cert-manager -n cert-manager -o yaml | grep image:
```

Should show: `quay.io/jetstack/cert-manager-controller:v1.15.1`

## Manual Installation (if skipped)

If you used `install_certmanager=false`, install manually:

```bash
helm repo add jetstack https://charts.jetstack.io --force-update
helm repo update

helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version v1.15.1 \
  --set crds.enabled=true

# Wait for ready
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/instance=cert-manager \
  -n cert-manager \
  --timeout=300s
```

## Upgrade Cert-Manager

To upgrade cert-manager version:

```bash
# Check current version
helm list -n cert-manager

# Upgrade
helm upgrade cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --version v1.16.0 \
  --set crds.enabled=true
```

## Summary

✅ **Cert-manager is installed by default** with `install-k8s.yml`  
✅ **Cert-manager is removed** with `uninstall-k8s.yml`  
✅ **Uninstall → Install cycles are clean** (no manual cleanup needed)  
✅ **GPU Operator prerequisite** is satisfied automatically  
✅ **Can be skipped** with `install_certmanager=false` if needed  

**Your workflow**: Simply run uninstall → install, and cert-manager is handled automatically! 🎉
