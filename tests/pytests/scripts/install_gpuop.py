#!/usr/bin/python3
"""
Script to generate values.yaml from a specified image-manifest file and run helm install for GPU-Operator.
Reuses functions from the pytest framework (lib.spec_util, k8_util, etc).
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
    with open(secrets_json, 'r') as f:
        secrets_obj = json.load(f)
    secrets_list = secrets_obj.get('secrets', [])
    if not secrets_list:
        print("No secrets found in secrets.json")
        sys.exit(1)
    for secret in secrets_list:
        secret_name = secret.get('name', 'docker-registry-secret')
        namespace = secret.get('namespace', default_namespace)
        server = secret.get('server', 'https://index.docker.io/v1/')
        username = secret.get('username', '')
        password = secret.get('password', '')
        secret_type = secret.get('type', 'docker-registry')
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
            print(f"Failed to create secret {secret_name}: {err}")
            sys.exit(ret)
        print(f"Registry secret '{secret_name}' created in namespace '{namespace}'. Output:\n{out}")

def run_helm_install(helm_chart, release_name, namespace, values_yaml, gpu_operator_version):
    helm_cmd = [
        "helm", "install", release_name, helm_chart,
        "-n", namespace,
        "-f", values_yaml,
        "--version", gpu_operator_version
    ]
    print(f"Running: {' '.join(helm_cmd)}")
    import subprocess
    result = subprocess.run(helm_cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        sys.exit(result.returncode)
    print("Helm install completed successfully.")

def handle_image_manifest(values_yaml, args):
    from urllib.parse import urlparse

    with open(args.image_manifest, 'r') as f:
        manifest = yaml.safe_load(f)

    k8_images = manifest.get('images', {}).get('k8', {})
    default_registry = manifest.get('images', {}).get('meta', {}).get('registry', {}).get('default', None)
    images = {}
    for artifact, info in k8_images.items():
        key = info.get('key')
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
        elif 'file://' in location:
            file_path = location.replace('file://', '')
            images[f"{artifact}.helm-chart"] = file_path
        # Add other location types if needed

    # Generate values.yaml
    # Note: script doesn't have access to k8 secrets, passing empty list
    ok = spec_util.generate_helmchart_deployment_config(getattr(args, 'gpu_operator_version', 'latest'), images, [], values_yaml)
    if not ok:
        print(f"Failed to generate values.yaml from manifest {args.image_manifest}")
        sys.exit(1)
    print(f"Generated values.yaml: {values_yaml}")
    return images

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
    with open(dcm_config_file, 'r') as f:
        dcm_config = yaml.safe_load(f)
    dcmConfigName = dcm_config.get('metadata', {}).get('name', None)
    if not dcmConfigName:
        print("ERROR: metadata.name not found in device-config-manager config file.")
        return False

    # Step 2: Apply the config file
    apply_cmd = ["kubectl", "apply", "-f", dcm_config_file]
    print(f"Applying device-config-manager config file: {dcm_config_file}")
    apply_result = subprocess.run(apply_cmd, capture_output=True, text=True)
    print(apply_result.stdout)
    if apply_result.returncode != 0:
        print(apply_result.stderr)
        return False

    # Step 3: Patch DeviceConfig CR
    repository = images.get('configManager.image.repository', None)
    version = images.get('configManager.image.version', None)
    secret = images.get('configManager.image.secret', None)
    if not repository or not version:
        print("ERROR: config-manager repository or version missing in images.")
        return False
    image_url = f"{repository}:{version}"
    patch = {
        "spec": {
            "configManager": {
                "image": image_url,
                "imageRegistrySecret": {
                    "name": secret
                },
                "config": {
                    "name": dcmConfigName
                }
            }
        }
    }
    patch_yaml = yaml.dump(patch)
    patch_file = "configmgr_patch.yaml"
    with open(patch_file, "w") as pf:
        pf.write(patch_yaml)
    cmd = [
        "kubectl", "patch", "deviceconfig", "default",
        "-n", namespace,
        "--type=merge",
        f"--patch-file={patch_file}"
    ]
    print(f"Patching DeviceConfig with configManager.image: {image_url}, imageRegistrySecret: {secret}, config.name: {dcmConfigName}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        return False
    return True

def main():
    parser = argparse.ArgumentParser(description="Install GPU Operator using image manifest and optional registry secrets.")
    parser.add_argument("--image-manifest", required=True, help="Path to image manifest YAML file.")
    parser.add_argument("--secrets-json", help="Path to registry secrets JSON file.")
    parser.add_argument("--namespace", default="kube-amd-gpu", help="Kubernetes namespace to install GPU Operator.")
    parser.add_argument("--values-yaml", default="values.yaml", help="Output values.yaml file.")
    parser.add_argument("--helm-chart", required=True, help="Path to the GPU Operator helm chart.")
    parser.add_argument("--release-name", default="gpu-operator", help="Helm release name.")
    parser.add_argument("--kube-config", default=os.path.expanduser("~/.kube/config"), help="Path to kube config file (default: ~/.kube/config)")
    parser.add_argument("--dcm-config-file", required=True, help="Path to device-config-manager configuration YAML file.")
    args = parser.parse_args()

    # Create local k8_cluster instance and set kube config
    k8_cluster = common.k8_cluster()
    k8_cluster.k8_kube_config = args.kube_config
    k8_util.k8_lib_init(k8_cluster.k8_kube_config)

    if args.secrets_json:
        with open(args.secrets_json) as fp:
            k8_cluster.k8_secrets = json.load(fp)
        k8_util.k8_init_cluster(k8_cluster)
        #create_registry_secrets(k8_cluster, args.secrets_json, args.namespace)
    else:
        print("WARNING: --secrets-json not provided. Registry secrets will not be created.")

    values_yaml = args.values_yaml
    images = handle_image_manifest(values_yaml, args)

    # Run helm install
    helm_chart = getattr(args, 'helm_chart', None)
    release_name = getattr(args, 'release_name', 'gpu-operator')
    gpu_operator_version = getattr(args, 'gpu_operator_version', 'latest')

    helm_result = run_helm_install(helm_chart, release_name, args.namespace, values_yaml, gpu_operator_version)
    if helm_result is not None and helm_result is False:
        print("ERROR: Helm install failed.")
        sys.exit(1)

    # After helm install, patch DeviceConfig CR with configManager image
    patch_result = setConfigManagerImage(args.namespace, images, args.dcm_config_file)
    if patch_result is not None and patch_result is False:
        print("ERROR: Patching DeviceConfig CR with configManager image failed.")
        sys.exit(1)

if __name__ == "__main__":
    main()