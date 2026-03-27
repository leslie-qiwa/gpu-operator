#!/usr/bin/python3
"""
GPU Operator Installation Script

This script automates the installation of AMD GPU Operator using Helm by:
1. Parsing an image manifest YAML file to extract container image locations
2. Generating a Helm values.yaml file with the extracted image references
3. Optionally creating Kubernetes registry secrets for private registries
4. Installing the GPU Operator via Helm
5. Patching the DeviceConfig CR with config manager settings

Prerequisites:
- kubectl configured with cluster access
- helm CLI installed
- Python 3.6+
- Valid image manifest YAML file
- Valid device-config-manager configuration YAML file

Example usage:
    python3 install_gpuop.py \\
        --image-manifest /path/to/manifest.yaml \\
        --helm-chart /path/to/gpu-operator-chart \\
        --dcm-config-file /path/to/dcm-config.yaml \\
        --namespace openshift-amd-gpu \\
        --gpu-operator-version 1.2.3

Author: AMD GPU Operator Team
"""
import sys
import os
import argparse
import yaml
import json
# Add parent directory to sys.path to allow importing lib modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import lib.spec_util as spec_util
import lib.k8_util as k8_util
from lib import common

def create_registry_secrets(k8_cluster, secrets_json, default_namespace):
    """
    Create Kubernetes registry secrets from a JSON configuration file.

    Args:
        k8_cluster: Kubernetes cluster object
        secrets_json: Path to JSON file containing secrets configuration
        default_namespace: Default namespace if not specified in secret config

    Returns:
        bool: True if all secrets created successfully, False otherwise
    """
    try:
        with open(secrets_json, 'r') as f:
            secrets_obj = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: Secrets file not found: {secrets_json}")
        return False
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse secrets JSON: {e}")
        return False

    secrets_list = secrets_obj.get('secrets', [])
    if not secrets_list:
        print("WARNING: No secrets found in secrets.json")
        return True  # Not an error, just no secrets to create

    for i, secret in enumerate(secrets_list):
        secret_name = secret.get('name', f'docker-registry-secret-{i}')
        namespace = secret.get('namespace', default_namespace)
        server = secret.get('server', 'https://index.docker.io/v1/')
        username = secret.get('username', '')
        password = secret.get('password', '')
        secret_type = secret.get('type', 'docker-registry')

        if not username or not password:
            print(f"WARNING: Skipping secret '{secret_name}' - missing username or password")
            continue

        print(f"Creating registry secret '{secret_name}' in namespace '{namespace}'...")
        try:
            ret, out, err = k8_util.k8_create_secret(
                k8_cluster,
                secret_name,
                secret_type,
                namespace=namespace,
                server=server,
                username=username,
                password=password
            )
            if ret != 0:
                print(f"ERROR: Failed to create secret {secret_name}: {err}")
                return False
            print(f"✓ Registry secret '{secret_name}' created successfully")
        except Exception as e:
            print(f"ERROR: Unexpected error creating secret '{secret_name}': {e}")
            return False

    return True

def run_helm_install(helm_chart, release_name, namespace, values_yaml, gpu_operator_version):
    import subprocess

    # Validate inputs
    if not os.path.exists(values_yaml):
        print(f"ERROR: Values file not found: {values_yaml}")
        return False

    helm_cmd = [
        "helm", "install", release_name, helm_chart,
        "-n", namespace,
        "-f", values_yaml,
        "--version", gpu_operator_version,
        "--create-namespace"  # Create namespace if it doesn't exist
    ]
    print(f"Running: {' '.join(helm_cmd)}")
    try:
        result = subprocess.run(helm_cmd, capture_output=True, text=True, timeout=300)
        print(result.stdout)
        if result.returncode != 0:
            print(f"ERROR: Helm install failed with return code {result.returncode}")
            print(result.stderr)
            return False
        print("Helm install completed successfully.")
        return True
    except subprocess.TimeoutExpired:
        print("ERROR: Helm install timed out after 300 seconds")
        return False
    except Exception as e:
        print(f"ERROR: Unexpected error during helm install: {e}")
        return False

def handle_image_manifest(values_yaml, args):
    from urllib.parse import urlparse

    try:
        with open(args.image_manifest, 'r') as f:
            manifest = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"ERROR: Image manifest file not found: {args.image_manifest}")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"ERROR: Failed to parse image manifest YAML: {e}")
        sys.exit(1)

    k8_images = manifest.get('images', {}).get('k8', {})
    if not k8_images:
        print(f"ERROR: No 'images.k8' section found in manifest {args.image_manifest}")
        sys.exit(1)

    default_registry = manifest.get('images', {}).get('meta', {}).get('registry', {}).get('default', None)
    images = {}
    secrets_set = set()  # Collect unique secret names

    for artifact, info in k8_images.items():
        key = info.get('key')
        if not key:
            print(f"WARNING: Skipping artifact '{artifact}' - missing 'key' field")
            continue
        location = info.get('location', '')
        version = info.get('version', None)
        secret = info.get('secret', None)
        # Handle <registry> substitution
        if 'container://' in location:
            url = location.replace('container://', '')
            if '<registry>' in url and default_registry:
                url = url.replace('<registry>', default_registry)
            parsed = urlparse('//' + url)  # Add // for urlparse to work
            repo = f"{parsed.netloc}{parsed.path}"
            images[f"{key}.repository"] = repo
            if version:
                images[f"{key}.version"] = version
            if secret:
                images[f"{key}.secret"] = secret
                secrets_set.add(secret)  # Collect secret name
        elif 'file://' in location:
            file_path = location.replace('file://', '')
            images[f"{artifact}.helm-chart"] = file_path
        # Add other location types if needed

    # Convert secrets set to sorted list for consistent ordering
    secret_list = sorted(list(secrets_set))

    # Generate values.yaml
    # Get gpu_operator_version from args or use 'latest'
    gpu_operator_version = getattr(args, 'gpu_operator_version', 'latest')

    # Pass collected secrets to generate_helmchart_deployment_config
    print(f"Collected {len(secret_list)} unique image pull secret(s): {secret_list}")
    ok = spec_util.generate_helmchart_deployment_config(gpu_operator_version, images, secret_list, values_yaml)
    if not ok:
        print(f"Failed to generate values.yaml from manifest {args.image_manifest}")
        sys.exit(1)
    print(f"Generated values.yaml: {values_yaml}")
    return images, secret_list

def setConfigManagerImage(namespace, images, dcm_config_file):
    """
    Update the DeviceConfig CR's configManager.image field with the image from manifest,
    and configManager.config.name from the config file.
    Also applies the config file.
    """
    import subprocess
    import yaml

    # Step 1: Parse config file and get metadata.name
    if not dcm_config_file or not os.path.exists(dcm_config_file):
        print(f"ERROR: DeviceConfigManager config file not found: {dcm_config_file}")
        return False

    try:
        with open(dcm_config_file, 'r') as f:
            dcm_config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"ERROR: Failed to parse device-config-manager config file: {e}")
        return False

    dcmConfigName = dcm_config.get('metadata', {}).get('name', None)
    if not dcmConfigName:
        print("ERROR: metadata.name not found in device-config-manager config file.")
        return False

    # Step 2: Apply the config file
    apply_cmd = ["kubectl", "apply", "-f", dcm_config_file]
    print(f"Applying device-config-manager config file: {dcm_config_file}")
    try:
        apply_result = subprocess.run(apply_cmd, capture_output=True, text=True, timeout=60)
        print(apply_result.stdout)
        if apply_result.returncode != 0:
            print(f"ERROR: Failed to apply config file")
            print(apply_result.stderr)
            return False
    except subprocess.TimeoutExpired:
        print("ERROR: kubectl apply timed out")
        return False
    except Exception as e:
        print(f"ERROR: Unexpected error applying config: {e}")
        return False

    # Step 3: Patch DeviceConfig CR
    # Note: Keys in images dict use dot notation (e.g., 'config-manager.repository')
    # Try different possible key formats
    repository = (images.get('configManager.image.repository') or
                  images.get('config-manager.repository') or
                  images.get('configManager.repository'))
    version = (images.get('configManager.image.version') or
               images.get('config-manager.version') or
               images.get('configManager.version'))
    secret = (images.get('configManager.image.secret') or
              images.get('config-manager.secret') or
              images.get('configManager.secret'))

    if not repository or not version:
        print(f"ERROR: config-manager repository or version missing in images.")
        print(f"Available keys: {list(images.keys())}")
        return False

    image_url = f"{repository}:{version}"

    # Build patch - only include imageRegistrySecret if secret is provided
    patch = {
        "spec": {
            "configManager": {
                "image": image_url,
                "config": {
                    "name": dcmConfigName
                }
            }
        }
    }

    if secret:
        patch["spec"]["configManager"]["imageRegistrySecret"] = {"name": secret}

    patch_yaml = yaml.dump(patch)
    patch_file = "configmgr_patch.yaml"
    try:
        with open(patch_file, "w") as pf:
            pf.write(patch_yaml)
    except IOError as e:
        print(f"ERROR: Failed to write patch file: {e}")
        return False

    cmd = [
        "kubectl", "patch", "deviceconfig", "default",
        "-n", namespace,
        "--type=merge",
        f"--patch-file={patch_file}"
    ]
    print(f"Patching DeviceConfig with configManager.image: {image_url}, imageRegistrySecret: {secret}, config.name: {dcmConfigName}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        print(result.stdout)
        if result.returncode != 0:
            print(f"ERROR: Failed to patch DeviceConfig")
            print(result.stderr)
            return False
        print("Successfully patched DeviceConfig CR")
        return True
    except subprocess.TimeoutExpired:
        print("ERROR: kubectl patch timed out")
        return False
    except Exception as e:
        print(f"ERROR: Unexpected error patching DeviceConfig: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Install GPU Operator using image manifest and optional registry secrets.")
    parser.add_argument("--image-manifest", required=True, help="Path to image manifest YAML file.")
    parser.add_argument("--secrets-json", help="Path to registry secrets JSON file.")
    parser.add_argument("--namespace", default="kube-amd-gpu", help="Kubernetes namespace to install GPU Operator.")
    parser.add_argument("--values-yaml", default="values.yaml", help="Output values.yaml file.")
    parser.add_argument("--helm-chart", required=True, help="Path to the GPU Operator helm chart.")
    parser.add_argument("--release-name", default="gpu-operator", help="Helm release name.")
    parser.add_argument("--gpu-operator-version", default="latest", help="GPU Operator version to install.")
    parser.add_argument("--kube-config", default=os.path.expanduser("~/.kube/config"), help="Path to kube config file (default: ~/.kube/config)")
    parser.add_argument("--dcm-config-file", required=True, help="Path to device-config-manager configuration YAML file.")
    parser.add_argument("--skip-dcm-patch", action="store_true", help="Skip patching DeviceConfig CR with config manager settings.")
    args = parser.parse_args()

    # Validate required files exist
    if not os.path.exists(args.image_manifest):
        print(f"ERROR: Image manifest file not found: {args.image_manifest}")
        sys.exit(1)

    if not os.path.exists(args.kube_config):
        print(f"ERROR: Kubeconfig file not found: {args.kube_config}")
        sys.exit(1)

    # Create local k8_cluster instance and set kube config
    k8_cluster = common.k8_cluster()
    k8_cluster.k8_kube_config = args.kube_config
    try:
        k8_util.k8_lib_init(k8_cluster.k8_kube_config)
    except Exception as e:
        print(f"ERROR: Failed to initialize k8 library: {e}")
        sys.exit(1)

    if args.secrets_json:
        if not os.path.exists(args.secrets_json):
            print(f"ERROR: Secrets JSON file not found: {args.secrets_json}")
            sys.exit(1)
        try:
            with open(args.secrets_json) as fp:
                k8_cluster.k8_secrets = json.load(fp)
            k8_util.k8_init_cluster(k8_cluster)
            #create_registry_secrets(k8_cluster, args.secrets_json, args.namespace)
        except json.JSONDecodeError as e:
            print(f"ERROR: Failed to parse secrets JSON: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"ERROR: Failed to initialize cluster with secrets: {e}")
            sys.exit(1)
    else:
        print("WARNING: --secrets-json not provided. Registry secrets will not be created.")

    values_yaml = args.values_yaml
    images, secret_list = handle_image_manifest(values_yaml, args)

    # Create registry secrets if secrets.json provided and secrets found in manifest
    if args.secrets_json and secret_list:
        print(f"\n=== Creating Registry Secrets ===")
        print(f"Secrets to create: {secret_list}")
        secrets_created = create_registry_secrets(k8_cluster, args.secrets_json, args.namespace)
        if not secrets_created:
            print("WARNING: Failed to create some registry secrets. Installation may fail if images require authentication.")
    elif secret_list and not args.secrets_json:
        print(f"\nWARNING: Image manifest references secrets {secret_list} but no --secrets-json provided.")
        print("If these images require authentication, installation will fail.")

    # Run helm install
    helm_chart = args.helm_chart
    release_name = args.release_name
    gpu_operator_version = args.gpu_operator_version

    print(f"\n=== Installing GPU Operator ===")
    print(f"Helm Chart: {helm_chart}")
    print(f"Release Name: {release_name}")
    print(f"Namespace: {args.namespace}")
    print(f"Version: {gpu_operator_version}")
    print(f"Values File: {values_yaml}")
    if secret_list:
        print(f"Image Pull Secrets: {', '.join(secret_list)}")

    helm_result = run_helm_install(helm_chart, release_name, args.namespace, values_yaml, gpu_operator_version)
    if not helm_result:
        print("ERROR: Helm install failed.")
        sys.exit(1)

    # After helm install, patch DeviceConfig CR with configManager image
    if not args.skip_dcm_patch:
        print(f"\n=== Patching DeviceConfig CR ===")
        patch_result = setConfigManagerImage(args.namespace, images, args.dcm_config_file)
        if not patch_result:
            print("ERROR: Patching DeviceConfig CR with configManager image failed.")
            sys.exit(1)
    else:
        print("Skipping DeviceConfig CR patch (--skip-dcm-patch specified)")

    print("\n=== GPU Operator installation completed successfully ===")
    sys.exit(0)

if __name__ == "__main__":
    main()