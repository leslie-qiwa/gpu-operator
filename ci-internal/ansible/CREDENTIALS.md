# Credential Management and Sudoers

## Overview

The inventory generator and Ansible playbooks support both root and non-root sudoer users. The JSON configuration provides credentials that are used for:

1. **SSH authentication** - Login to the nodes
2. **Privilege escalation (sudo)** - Execute privileged commands

## User Types

### Root User

If `username` is `root`, the user already has full privileges:

```json
{
  "ip": "10.11.78.80",
  "type": "master",
  "username": "root",
  "password": "RootPass123"
}
```

Generated inventory:
```ini
hostname ansible_host=10.11.78.80 ansible_user=root ansible_password=RootPass123
```

### Sudoer User (Non-root)

If `username` is **not** `root`, the user is assumed to be a **sudoer** with the **same password** for sudo:

```json
{
  "ip": "10.11.78.80",
  "type": "master",
  "username": "vm",
  "password": "vm"
}
```

Generated inventory:
```ini
hostname ansible_host=10.11.78.80 ansible_user=vm ansible_password=vm ansible_become=yes ansible_become_method=sudo ansible_become_password=vm
```

**Note**: The script assumes that:
- The user has sudo privileges
- The sudo password is the **same** as the SSH password
- Sudo requires a password (not NOPASSWD)

## How It Works

### 1. SSH Authentication

Ansible connects to the node using:
- `ansible_user` - The username from JSON
- `ansible_password` - The password from JSON

### 2. Privilege Escalation

When Ansible needs to run privileged commands (with `become: yes`):
- `ansible_become=yes` - Enable privilege escalation
- `ansible_become_method=sudo` - Use sudo
- `ansible_become_password=<password>` - Password for sudo

### 3. Example Flow

When running a playbook task:

```yaml
- name: Install package
  apt:
    name: curl
    state: present
  become: yes
```

Ansible executes:
1. SSH to node as user `vm` with password `vm`
2. Run command with `sudo` using password `vm`
3. Execute: `sudo apt install curl`

## JSON Configuration Examples

### Example 1: Sudoer with Password

```json
{
  "name": "dev-cluster",
  "deployment": "k8",
  "instances": [
    {
      "ip": "192.168.1.10",
      "type": "master",
      "username": "ubuntu",
      "password": "SecurePass123"
    },
    {
      "ip": "192.168.1.11",
      "type": "worker",
      "username": "ubuntu",
      "password": "SecurePass123",
      "gpu_series": "MI200",
      "gpu_count": 2
    }
  ]
}
```

**User setup on nodes**:
```bash
# User 'ubuntu' should be a sudoer
sudo usermod -aG sudo ubuntu

# Verify sudo access
sudo -l -U ubuntu
```

### Example 2: Root User

```json
{
  "name": "prod-cluster",
  "deployment": "k8",
  "instances": [
    {
      "ip": "10.0.1.10",
      "type": "master",
      "username": "root",
      "password": "RootPassword"
    }
  ]
}
```

### Example 3: Mixed (Root + Sudoer)

```json
{
  "name": "mixed-cluster",
  "deployment": "k8",
  "instances": [
    {
      "ip": "10.0.1.10",
      "type": "master",
      "username": "root",
      "password": "RootPass"
    },
    {
      "ip": "10.0.1.11",
      "type": "worker",
      "username": "worker-admin",
      "password": "WorkerPass",
      "gpu_series": "MI300X",
      "gpu_count": 8
    }
  ]
}
```

## Prerequisites on Target Nodes

### For Non-Root Sudoer Users

Ensure the user is configured as a sudoer on each node:

#### Option 1: Add to sudo group
```bash
# On Ubuntu/Debian
sudo usermod -aG sudo vm

# On RHEL/CentOS
sudo usermod -aG wheel vm
```

#### Option 2: Add to sudoers file
```bash
# Edit sudoers file
sudo visudo

# Add line (replace 'vm' with your username)
vm ALL=(ALL:ALL) ALL

# Or for passwordless sudo (not recommended with password in JSON)
# vm ALL=(ALL:ALL) NOPASSWD: ALL
```

#### Verify sudo access
```bash
# Login as user
ssh vm@10.11.78.80

# Test sudo
sudo whoami
# Should output: root
```

### For Root User

Ensure root login is enabled:

```bash
# Edit SSH config
sudo vi /etc/ssh/sshd_config

# Enable root login
PermitRootLogin yes

# Restart SSH
sudo systemctl restart sshd
```

## Security Considerations

### ⚠️ Passwords in Plain Text

The generated inventory contains passwords in **plain text**. This is a security risk.

#### Mitigation Options:

**Option 1: Use SSH Keys Instead** (Recommended)

Omit `password` from JSON and use SSH key authentication:

```json
{
  "ip": "10.11.78.80",
  "type": "master",
  "username": "vm"
}
```

Then configure SSH keys:
```bash
# Generate SSH key
ssh-keygen -t rsa -b 4096 -f ~/.ssh/k8s_key

# Copy to nodes
ssh-copy-id -i ~/.ssh/k8s_key.pub vm@10.11.78.80

# Add to inventory manually or via command line
ansible-playbook -i inventory.ini install-k8s.yml --private-key=~/.ssh/k8s_key
```

**Option 2: Ansible Vault**

Encrypt the inventory file:

```bash
# Encrypt inventory
ansible-vault encrypt inventory.ini

# Use with playbook (will prompt for vault password)
ansible-playbook -i inventory.ini install-k8s.yml --ask-vault-pass

# Or use password file
echo "VaultPassword123" > .vault_pass
chmod 600 .vault_pass
ansible-playbook -i inventory.ini install-k8s.yml --vault-password-file=.vault_pass
```

**Option 3: Environment Variables**

Store passwords in environment variables:

```bash
export ANSIBLE_PASSWORD="vm"
export ANSIBLE_BECOME_PASSWORD="vm"

# Modify inventory to use env vars
# ansible_password={{ lookup('env', 'ANSIBLE_PASSWORD') }}
```

**Option 4: Passwordless Sudo** (Less Secure)

Configure sudo to not require password:

```bash
# On each node
sudo visudo

# Add
vm ALL=(ALL:ALL) NOPASSWD: ALL
```

Then remove `ansible_become_password` from inventory.

### 🔒 Best Practices

1. **Use SSH keys** instead of passwords when possible
2. **Encrypt inventory files** with ansible-vault
3. **Restrict file permissions** on inventory files:
   ```bash
   chmod 600 inventory.ini
   ```
4. **Use temporary credentials** for installation, then rotate
5. **Don't commit** inventory files with passwords to git:
   ```bash
   echo "inventory.ini" >> .gitignore
   ```
6. **Use separate credentials** for each environment (dev/staging/prod)
7. **Audit sudo access** regularly

## Testing Credentials

### Test SSH Connection

```bash
# Test SSH with password
sshpass -p 'vm' ssh vm@10.11.78.80 'echo "SSH OK"'

# Test with ansible
ansible -i inventory.ini all -m ping
```

### Test Sudo Access

```bash
# Test sudo with password
ssh vm@10.11.78.80 'echo "vm" | sudo -S whoami'

# Test with ansible
ansible -i inventory.ini all -m command -a "whoami" --become
```

### Verify Complete Access

```bash
# Run privileged command
ansible -i inventory.ini all -m apt -a "name=curl state=present update_cache=yes" --become
```

## Troubleshooting

### Issue: "sudo: a password is required"

**Solution**: Ensure `ansible_become_password` is set correctly.

Check inventory:
```bash
grep become_password inventory.ini
```

### Issue: "Permission denied"

**Solution**: Verify user has sudo privileges.

On the node:
```bash
sudo -l -U vm
```

### Issue: "Authentication failure"

**Solution**: Verify SSH password is correct.

Test manually:
```bash
ssh vm@10.11.78.80
```

### Issue: User is not in sudoers

**Solution**: Add user to sudo group.

```bash
# On node
sudo usermod -aG sudo vm

# Verify
groups vm
```

## Example: Complete Setup

### 1. Prepare nodes

```bash
# On each node (as root or existing sudoer)
# Create user
useradd -m -s /bin/bash vm
echo "vm:vm" | chpasswd

# Add to sudo group
usermod -aG sudo vm

# Verify
su - vm
sudo whoami  # Should output: root
```

### 2. Create JSON config

```json
{
  "name": "test-cluster",
  "deployment": "k8",
  "instances": [
    {"ip": "10.0.1.10", "type": "master", "username": "vm", "password": "vm"},
    {"ip": "10.0.1.11", "type": "worker", "username": "vm", "password": "vm", "gpu_series": "MI200", "gpu_count": 1}
  ]
}
```

### 3. Generate and use inventory

```bash
# Generate inventory
./generate_inventory.py -i test.json -o inventory.ini

# Secure the file
chmod 600 inventory.ini

# Test access
ansible -i inventory.ini all -m ping

# Test sudo
ansible -i inventory.ini all -m command -a "whoami" --become

# Install K8s
ansible-playbook -i inventory.ini install-k8s.yml -e "k8s_version=1.30.0"
```

## See Also

- [INVENTORY_GENERATOR.md](INVENTORY_GENERATOR.md) - Inventory generator reference
- [README.md](README.md) - Main documentation
- [Ansible Become Documentation](https://docs.ansible.com/ansible/latest/playbook_guide/playbooks_privilege_escalation.html)
