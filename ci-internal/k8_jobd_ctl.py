#!/usr/bin/python3

import os
import sys
import pdb
import json
import argparse
import time
import logging
import requests
import subprocess
from urllib.parse import urlparse


GlobalOptions = argparse.Namespace()

DOCKER_DAEMON_CONFIG = {
    "exec-opts": ["native.cgroupdriver=cgroupfs"],
    "log-driver": "json-file",
    "insecure-registries": ["registry.test.pensando.io:5000"],
    "log-opts": {
        "max-size": "100m",
        "max-file": "3"
    },
    "storage-driver": "overlay2"
}

def _init_logger():
    try:
        log_dir = os.path.join(os.getenv("PWD"), "logs")
        os.makedirs(log_dir, exist_ok = True)

        # Configure file logging
        logging.basicConfig(
            filename=os.path.join(log_dir, "k8_jobd_ctl.log"),
            level=logging.DEBUG,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        # Create a console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG) # Set level for console output
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(formatter)

        # Add the console handler to the root logger
        logging.getLogger().addHandler(console_handler)
        logging.getLogger("paramiko").setLevel(logging.WARNING)
        logging.getLogger('invoke').setLevel(logging.WARNING)
        logging.getLogger('kubernetes').setLevel(logging.WARNING)
    except Exception as e:
        print(f"Failed to initialize logger - using default settings, error {e}")
    finally:
        return logging.getLogger('k8_jobd_ctl')

def _init_cmdline_args():
    global GlobalOptions
    parser = argparse.ArgumentParser()

    subparsers = parser.add_subparsers(dest="command", help="jobd ctl commands")

    testbed_cmd = subparsers.add_parser("testbed", help="Testbed commands")
    testbed_cmd.add_argument("--testbed", required = True, help = "jobd testbed json file")
    testbed_cmd.add_argument("--target", default='k8', choices=["k8", "openshift", "standalone"], help = "Target deployment")
    testbed_cmd.add_argument("--fetch-kube-config", action='store_true', default=False, help = "Optionally download /etc/kubernetes/config file from master")
    testbed_cmd.add_argument("--reboot-workers", action='store_true', default=False, help = "Reboot worker nodes")

    image_cmd = subparsers.add_parser("image", help="Image management commands")
    image_cmd.add_argument("--testbed", required = True, help = "jobd testbed json file")
    image_cmd.add_argument("--load-images", action='store_true', default=False, help = "Load images into the registry")
    image_cmd.add_argument("--setup-insecure-registry", action='store_true', default=False, help = "Load images into the registry")
    image_cmd.add_argument("--pull-images", action='store_true', default=False, help = "Download images on each nodes of the cluster")
    image_cmd.add_argument("--registries", default=None, help = "Registry information as json file")
    image_cmd.add_argument("--image-manifest", default='/tmp/images.yaml', help = "generated images yaml")
    image_cmd.add_argument("--seed-image-manifest", default='/gpu-operator/ci-internal/sanity-images.yml', help = "generated images yaml")
    image_cmd.add_argument("--target", default='k8', choices=["k8", "openshift", "standalone"], help = "Target deployment")

    report_cmd = subparsers.add_parser("report", help="Reporting related commands")
    report_cmd.add_argument("--testbed", required = True, help = "jobd testbed json file")
    report_cmd.add_argument("--show", action='store_true', default=False, help = "Print test report, uses pytest junit-xml")
    report_cmd.add_argument("--target", default='k8', choices=["k8", "openshift", "standalone"], help = "Target deployment")
    cmd_opts = parser.parse_args()
    GlobalOptions.__dict__.update(cmd_opts.__dict__)
    return parser

def _load_testbed_json(testbed_json):
    if testbed_json:
        with open(testbed_json, "r") as fp:
            data = json.load(fp)

        if "Instances" in data:
            testbed_info = data["Instances"][0]["RawJSON"]
            return testbed_info
    return None

def _load_registries_info(registries_spec):
    if registries_spec:
        with open(registries_spec, "r") as fp:
            data = json.load(fp)
        return data
    return None

def _is_pull_images_enabled(testbed_json):
    if testbed_json:
        with open(testbed_json, "r") as fp:
            data = json.load(fp)

        return data["Instances"][0]["Resource"].get("pull-images", "no") == "yes"
    return False

def _get_cluster_name(testbed_json):
    if testbed_json:
        with open(testbed_json, "r") as fp:
            data = json.load(fp)

        return data["Instances"][0]["Resource"].get("cluster-name", None)
    return None


def run_command(node, cmd, timeout = 90):
    from fabric import Connection
    from invoke.exceptions import UnexpectedExit
    from invoke.exceptions import CommandTimedOut

    conn_kwargs = {
        "password" : node["password"],
    }
    with Connection(node["ip"], user = node["username"], connect_kwargs = conn_kwargs) as conn:
        try:
            result = conn.run(cmd, hide = True, in_stream=False, timeout = timeout)
            return result
        except UnexpectedExit as ue:
            return ue.result
        except CommandTimedOut as to:
            return to.result
    raise Exception(f"Failed to connect to node : {node}")

def put(node, src_file, dest_file, sudo = False, timeout = 90):
    from fabric import Connection
    from invoke.exceptions import UnexpectedExit
    from invoke.exceptions import CommandTimedOut

    conn_kwargs = {
        "password" : node["password"],
    }
    with Connection(node["ip"], user = node["username"], connect_kwargs = conn_kwargs) as conn:
        try:
            dest_tmp_file = os.path.join("/tmp", os.path.basename(dest_file))
            conn.put(src_file, dest_tmp_file)
            cp_cmd = []
            if sudo:
                cp_cmd.append("sudo")
            cp_cmd.extend(["cp", dest_tmp_file, dest_file])
            result = conn.run(" ".join(cp_cmd), hide = True, in_stream=False, timeout = timeout)
            return result
        except UnexpectedExit as ue:
            return ue.result
        except CommandTimedOut as to:
            return to.result
    raise Exception(f"Failed to connect to node : {node}")

def get(node, remote_file, local_file):
    from fabric import Connection
    from invoke.exceptions import UnexpectedExit
    from invoke.exceptions import CommandTimedOut

    conn_kwargs = {
        "password" : node["password"],
    }

    if not os.path.exists(os.path.dirname(local_file)):
        os.makedirs(os.path.dirname(local_file))
    
    with Connection(node["ip"], user = node["username"], connect_kwargs = conn_kwargs) as conn:
        conn.get(remote_file, local_file)

def k8_get_node_info(master, node_name = None):
    if node_name:
        cmd = ["kubectl", "get", "node", node_name, "-ojson"]
        result = run_command(master, " ".join(cmd))
        if result.return_code != 0:
            return result.return_code, None
        k8_node_info = json.loads(result.stdout)
        return 0, k8_node_info
    else:
        cmd = ["kubectl", "get", "nodes", "-ojson"]
        result = run_command(master, " ".join(cmd))
        if result.return_code != 0:
            return result.return_code, None
        k8_nodes_info = json.loads(result.stdout)
        return 0, k8_nodes_info.get("items", None)

def k8_get_worker_nodename(logger, master, worker_ip):
    ret_code, items = k8_get_node_info(master)
    if ret_code != 0:
        logger.error("Failed to retrieve node information from k8 cluster")
        return None
    for node_info in items:
        addresses = node_info["status"]["addresses"]
        for entry in addresses:
            if entry["address"] == worker_ip:
                return node_info["metadata"]["name"]
    return None

def _get_master_nodes(testbed_info):
    """
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
    """
    master_nodes = list(filter(lambda x: x["type"] == "master", testbed_info["instances"]))
    return master_nodes

def _get_worker_nodes(testbed_info):
    """
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
    """
    worker_nodes = list(filter(lambda x: x["type"] == "worker", testbed_info["instances"]))
    return worker_nodes

def _cordon_uncordon_node(logger, master, worker_nodename : str, cordon : bool):
    if cordon:
        cmd = ["kubectl", "cordon", worker_nodename]
        msg = f"Cordoning node {worker_nodename}"
    else:
        cmd = ["kubectl", "uncordon", worker_nodename]
        msg = f"Uncordoning node {worker_nodename}"
    result = run_command(master, " ".join(cmd))
    if result.return_code == 0:
        logger.info(f"{msg}, successful")
    else:
        logger.error(f"{msg}, failed")
    return result.return_code

def _reboot_node(logger, master, worker, worker_nodename):
    cmd = ["sudo", "reboot"]
    logger.debug(f"Rebooting worker node : {worker_nodename}")
    try:
        result = run_command(worker, " ".join(cmd))
    except:
        pass # Ignore timeout based errors
    while True:
        ret_code, node_info = k8_get_node_info(master, worker_nodename)
        if ret_code != 0:
            logger.error("Failed to retrieve node information from k8 cluster")
            return
        conditions = node_info["status"]["conditions"]
        for entry in conditions:
            if entry["type"] == "Ready":
                """
                {
                    "lastHeartbeatTime": "2025-06-20T07:23:15Z",
                    "lastTransitionTime": "2025-06-20T07:26:48Z",
                    "message": "Kubelet stopped posting node status.",
                    "reason": "NodeStatusUnknown",
                    "status": "Unknown",
                    "type": "Ready"
                }
                """
                if entry["status"] == "Unknown":
                    logger.info(f"Worker node : {worker_nodename} is offline")
                    return
                elif entry["status"] == "True":
                    time.sleep(10)
    return

def _wait_node_ready(logger, master, worker_nodename):
    logger.debug(f"Waiting for worker-node {worker_nodename} to come online")
    countdown = 100
    for _ in range(100):
        ret_code, node_info = k8_get_node_info(master, worker_nodename)
        if ret_code != 0:
            logger.error("Failed to retrieve node information from k8 cluster")
            return
        conditions = node_info["status"]["conditions"]
        for entry in conditions:
            if entry["type"] == "Ready":
                """
                {
                    "lastHeartbeatTime": "2025-06-20T07:23:15Z",
                    "lastTransitionTime": "2025-06-20T07:26:48Z",
                    "message": "Kubelet stopped posting node status.",
                    "reason": "NodeStatusUnknown",
                    "status": "Unknown",
                    "type": "Ready"
                }
                """
                if entry["status"] == "Unknown":
                    time.sleep(20)
                elif entry["status"] == "True":
                    logger.info(f"Worker-node {worker_nodename} is online")
                    return True
    logger.error(f"Failed to connect to worker-node {worker_nodename} post reboot")
    return False
        
def _fetch_kube_config(logger, master):
    cmd = ["sudo", "cp", "/etc/kubernetes/admin.conf", "/tmp/config"]
    result = run_command(master, " ".join(cmd))
    if result.return_code != 0:
        logger.error("Failed to copy /etc/kubernetes/admin.conf to /tmp folder")
        return False

    cmd = ["sudo", "chmod", "755", "/tmp/config"]
    result = run_command(master, " ".join(cmd))
    if result.return_code != 0:
        logger.error("Failed enable read/write permission to /tmp/config")
        return False

    dest_file = os.path.join(os.getenv("HOME"), ".kube", "config")
    try:
        get(master, "/tmp/config", dest_file)
        os.system(f"chmod 600 {dest_file}")
    except Exception as e:
        logger.error(f"Failed to fetch kube-config, error : {e}")
        return False
    return True

def _reboot_workers(logger, master, workers):
    ret = True
    for worker in workers:
        node_ip = worker["ip"]
        worker_nodename = k8_get_worker_nodename(logger, master, node_ip)
        if worker_nodename:
            if _cordon_uncordon_node(logger, master, worker_nodename, True) == 0:
                _reboot_node(logger, master, worker, worker_nodename)
                time.sleep(180)
                ret = _wait_node_ready(logger, master, worker_nodename)
            else:
                ret = False
            if _cordon_uncordon_node(logger, master, worker_nodename, False) != 0:
                ret = False
        else:
            logger.error(f"No such node found with ip-address {node_ip}")
            ret = False
    return ret

def _load_images(logger, registry_info, seed_image_manifest, image_manifest, target):
    import docker

    if not registry_info:
        return False

    image_registry_info  = registry_info["image-registry"]
    result = True
    # Load seed_image_manifest
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(sequence=4, offset=2)

    with open(seed_image_manifest, 'r') as fp:
        image_manifest_templ = yaml.load(fp)

    for tgt in ['k8', 'standalone', 'openshift']:
        if target != tgt and tgt in image_manifest_templ['images']:
            del image_manifest_templ['images'][tgt]

    client = docker.from_env(timeout=900)
    for artifact_name in list(image_manifest_templ['images'][target].keys()):
        logger.info(f"Processing {artifact_name} section")
        artifact_info = image_manifest_templ['images'][target][artifact_name]
        if artifact_info.get('image', None) != None:
            img = artifact_info['image']
            if 'file://' in img:
                image_file = img.split('file://')[-1]
                if not os.path.exists(image_file):
                    logger.warning(f"Specified image-file {img} missing - ignore for now")
                    del image_manifest_templ['images'][target][artifact_name]
                    continue
            elif 'container://' in img:
                image_file = img.split('container://')[-1]
            else:
                image_file = img

            metadata = {}
            if artifact_info.get('metadata', None):
                metadata_file = artifact_info['metadata'].split('file://')[-1]
                if os.path.exists(metadata_file):
                    with open(metadata_file) as fp:
                        metadata = json.load(fp)

            if artifact_info['kind'] == 'helm-chart':
                artifact_info['location'] = f"file://{image_file}"
                artifact_info['version'] = metadata.get("ReleaseTag", artifact_info.get("version", "NA"))
            elif artifact_info['kind'] == 'debian':
                artifact_info['location'] = f"file://{image_file}"
                artifact_info['version'] = metadata.get("ReleaseTag", artifact_info.get("version", "NA"))
            elif artifact_info['kind'] == 'olm-bundle':
                artifact_info['location'] = f"container://{image_file}"
                artifact_info['version'] = metadata.get("ReleaseTag", artifact_info.get("version", "NA"))
            elif artifact_info['kind'] in ['container']:
                try:
                    with open(image_file, 'rb') as fp:
                        loaded_images = client.images.load(fp)
                    if not loaded_images:
                        result = False
                        logger.error(f"Fatal Error: Failed to collect loaded-images information, image-file : {image_file}")
                        continue
                except docker.errors.APIError as de:
                    logger.error(f"Fatal Error: Unable to load image {image_file}, error : {de}")
                    result = False
                    continue
                except Exception as e:
                    logger.error(f"Fatal Error: Unknown error while loading image {image_file}, error : {e}")
                    result = False
                    continue

                loaded_image = loaded_images[0]
                loaded_image_tag = loaded_image.tags[0]
                _, image_ver = loaded_image_tag.split('/', 1)
                image_name, image_tag = image_ver.split(':')
                logger.info(f"For artifact: {artifact_name}, image-tag: {loaded_image_tag}, derived image-name: {image_name} tag: {image_tag}")

                tag = loaded_image.attrs['Config']['Labels'].get('HOURLY_TAG', metadata.get("ReleaseTag", ""))
                if "agfhc" in image_tag:
                    tag = f"agfhc-{tag}"
                if tag == "":
                    tag = image_tag

                if image_registry_info["type"] == "hosted":
                    # Push tag to specified registry
                    img_registry = image_registry_info["value"]
                    loaded_image.tag(repository = f"{img_registry}/rocm/{image_name}", tag = tag)
                    new_image = f"{img_registry}/rocm/{image_name}"

                    done = False
                    for _ in range(5):
                        successful = True
                        try:
                            for line in client.images.push(f"{new_image}:{tag}", stream=True, decode=True):
                                if 'errorDetail' in line:
                                    err_msg = line['errorDetail']['message']
                                    logger.error(f"Error while pushing image {new_image}:{tag}, error: {err_msg}")
                                    successful = False
                                    break
                                #logger.debug(f"{new_image}:{tag} => {line}")
                        except docker.errors.APIError as de:
                            logger.error(f"Fatal Error: Unable to tag image {image_file}, error : {de}")
                            result = False
                            break
                        except Exception as e:
                            logger.error(f"Fatal Error: Unknown error while tagging image {image_file}, error : {e}")
                            result = False
                            break
                        if successful:
                            logger.info(f"Successfully pushed {new_image}:{tag}")
                            done = True
                            break
                        else:
                            logger.warning("Failed to push docker images, retry after 10sec")
                            time.sleep(10)
                elif image_registry_info["type"] == "global": 
                    # images are already available in amdpsdo or rocm, but we need image_name!!
                    img_registry = image_registry_info["value"]
                    new_image = f"{img_registry}/{image_name}"

                if done:
                    artifact_info['location'] = f"container://{new_image}"
                    artifact_info['version'] = tag
                    logger.info(f"Using: {new_image}:{tag}")
                else:
                    logger.error(f"Failed to push image {new_image}:{tag}")
                    result = False
    if result:
        # Update driver section (only for k8 and standalone)
        driver_registry_info = registry_info["driver-registry"]
        drv_registry = driver_registry_info["value"]
        if target in ['k8', 'standalone']:
            artifact_name = 'driver'
            driver_info = image_manifest_templ['images'][target][artifact_name]
            driver_info['location'] = f"container://{drv_registry}/driver-builds"
            # driver_info['location'] = f"container://registry.test.pensando.io:5000/amdgpu_kmod"
        with open(image_manifest, 'w') as fp:
            yaml.dump(image_manifest_templ, fp)
    return result

def _upload_docker_daemon_conf(logger, registry_info, testbed_info):
    # Generate/Update /etc/docker/daemon.json file
    daemon_conf = DOCKER_DAEMON_CONFIG
    registries = []
    image_registry_info  = registry_info["image-registry"]
    driver_registry_info = registry_info["driver-registry"]
    if image_registry_info["secure"] == "no":
        registries.append(image_registry_info["value"])

    if driver_registry_info["secure"] == "no":
        registries.append(driver_registry_info["value"])

    daemon_conf["insecure-registries"].extend(registries)
    with open("/tmp/docker_daemon.json", "w") as fp:
        json.dump(daemon_conf, fp, indent=4)

    masters = _get_master_nodes(testbed_info)
    workers = _get_worker_nodes(testbed_info)
    cmd = ["sudo", "systemctl", "restart", "docker"]
    for node in masters + workers:
        result = put(node, "/tmp/docker_daemon.json", "/etc/docker/daemon.json", sudo = True) 
        if result.return_code != 0:
            logger.error(f"Failed to upload docker_daemon.json to /etc/docker/daemon.json for node {node}")
            return result.return_code
        result = run_command(node, " ".join(cmd))
        if result.return_code != 0:
            logger.error("Failed restart docker daemon, result: ")
            return result.return_code
    return 0

def _upload_crio_registry_conf(logger, registry_info, testbed_info):
    """
    Generates a CRI-O registry configuration file for an insecure registry.

    Args:
        registry_location (str): The hostname or hostname:port of the insecure registry.
        config_filename (str, optional): The name of the configuration file to create.
                                        Defaults to "runner-registry.conf".
    """
    import tomlkit

    # Define the output directory
    output_directory = os.path.join(os.getenv("PWD"), "logs")
    # Ensure the output directory exists
    os.makedirs(output_directory, exist_ok=True)

    config_filename="runner-registry.conf"
    local_file = os.path.join(output_directory, config_filename)

    # Create a new TOML document
    doc = tomlkit.document()
    doc.add("registry", tomlkit.aot())  # Add Array of Tables

    # Create the [[registry]] table
    image_registry_info  = registry_info["image-registry"]
    driver_registry_info = registry_info["driver-registry"]
    if image_registry_info["secure"] == "no":
        registry_table = tomlkit.table()
        registry_table.add("location", image_registry_info["value"])
        registry_table.add("insecure", True)
        # Add the registry table to the document
        doc["registry"].append(registry_table)

    if driver_registry_info["secure"] == "no":
        registry_table = tomlkit.table()
        registry_table.add("location", driver_registry_info["value"])
        registry_table.add("insecure", True)
        # Add the registry table to the document
        doc["registry"].append(registry_table)

    # Write the TOML content to the file
    try:
        with open(local_file, "w") as f:
            f.write(tomlkit.dumps(doc))
        logger.info(f"CRI-O insecure registry configuration written to: {local_file}")
    except Exception as e:
        logger.error(f"er occurred while writing the file: {e}")
        return -1

    masters = _get_master_nodes(testbed_info)
    workers = _get_worker_nodes(testbed_info)
    cmd_daemon = ["sudo", "systemctl", "daemon-reload"]
    cmd_crio = ["sudo", "systemctl", "restart", "crio"]
    cmd_kubelet = ["sudo", "systemctl", "restart", "kubelet"]
    cmd_crio_img_cleanup = ["sudo", "crictl", "rmi", "-a"]
    for node in masters + workers:
        result = put(node, local_file, os.path.join("/etc/containers/registries.conf.d/", config_filename), sudo = True) 
        if result.return_code != 0:
            logger.error(f"Failed to upload docker_daemon.json to /etc/docker/daemon.json for node {node}")
            return result.return_code
        result = run_command(node, " ".join(cmd_daemon))
        if result.return_code != 0:
            logger.error(f"Failed acheive systemctl daemon-reload, result: {result}")
            return result.return_code
        result = run_command(node, " ".join(cmd_crio))
        if result.return_code != 0:
            logger.error(f"Failed acheive systemctl restart crio, result: {result}")
            return result.return_code
        result = run_command(node, " ".join(cmd_kubelet))
        if result.return_code != 0:
            logger.error(f"Failed acheive systemctl restart kubelet, result: {result}")
            return result.return_code
        result = run_command(node, " ".join(cmd_crio_img_cleanup))
        # Ignore error as image cleanup would fail as some images are still in use
    return 0

def _upload_containerd_registry_conf(logger, registry_info, testbed_info):
    """
    Updates containerd configuration to add insecure registry using hosts.toml (v2.x strict mode).

    This function adds the dynamic test registries that are marked as insecure.
    It also ensures registry.test.pensando.io:5000 is configured (defensive check).

    Args:
        logger: Logger instance
        registry_info (dict): Registry information containing image-registry and driver-registry
        testbed_info: Testbed information containing node details

    Returns:
        0 on success, non-zero on failure
    """
    # Define the output directory
    output_directory = os.path.join(os.getenv("PWD"), "logs")
    os.makedirs(output_directory, exist_ok=True)

    # Permanent registry (should be configured by Ansible, but we check defensively)
    permanent_registry = "registry.test.pensando.io:5000"

    # Build list of registries to ensure are configured
    registries_to_check = [permanent_registry]

    # Add registries from registry_info if they are insecure
    image_registry_info = registry_info["image-registry"]
    driver_registry_info = registry_info["driver-registry"]

    if image_registry_info["secure"] == "no":
        img_registry = image_registry_info["value"]
        if img_registry != permanent_registry:
            registries_to_check.append(img_registry)

    if driver_registry_info["secure"] == "no":
        drv_registry = driver_registry_info["value"]
        if drv_registry != permanent_registry and drv_registry not in registries_to_check:
            registries_to_check.append(drv_registry)

    masters = _get_master_nodes(testbed_info)
    workers = _get_worker_nodes(testbed_info)

    cmd_daemon = ["sudo", "systemctl", "daemon-reload"]
    cmd_containerd = ["sudo", "systemctl", "restart", "containerd"]
    cmd_kubelet = ["sudo", "systemctl", "restart", "kubelet"]
    cmd_img_cleanup = ["sudo", "crictl", "rmi", "-a"]
    cmd_verify_containerd = ["sudo", "systemctl", "is-active", "containerd"]

    for node in masters + workers:
        registries_added = []

        for reg in registries_to_check:
            # Check if hosts.toml already exists for this registry
            hosts_toml_path = f"/etc/containerd/certs.d/{reg}/hosts.toml"
            check_cmd = ["sudo", "test", "-f", hosts_toml_path]
            result = run_command(node, " ".join(check_cmd))

            if result.return_code == 0:
                logger.info(f"Registry {reg} already configured (hosts.toml exists) on {node['ip']}")
                continue

            # Create registry directory
            mkdir_cmd = ["sudo", "mkdir", "-p", f"/etc/containerd/certs.d/{reg}"]
            result = run_command(node, " ".join(mkdir_cmd))
            if result.return_code != 0:
                logger.error(f"Failed to create directory for {reg} on {node['ip']}")
                return result.return_code

            # Generate hosts.toml content (containerd v2.x strict mode)
            hosts_content = f"""server = "http://{reg}"

[host."http://{reg}"]
  capabilities = ["pull", "resolve"]

[host."{reg}"]
  capabilities = ["pull", "resolve"]
"""

            # Write to local temp file
            local_hosts_file = os.path.join(output_directory, f"hosts-{reg.replace(':', '_')}.toml")
            with open(local_hosts_file, "w") as f:
                f.write(hosts_content)

            # Upload hosts.toml
            result = put(node, local_hosts_file, hosts_toml_path, sudo=True)
            if result.return_code != 0:
                logger.error(f"Failed to upload hosts.toml for {reg} to {node['ip']}")
                return result.return_code

            registries_added.append(reg)
            logger.info(f"Added registry {reg} via hosts.toml on {node['ip']}")

        if not registries_added:
            logger.info(f"No new registries to add on {node['ip']}")
            continue  # Skip restart if no changes

        # Reload daemon
        result = run_command(node, " ".join(cmd_daemon))
        if result.return_code != 0:
            logger.error(f"Failed systemctl daemon-reload on {node['ip']}")
            return result.return_code

        # Restart containerd
        result = run_command(node, " ".join(cmd_containerd))
        if result.return_code != 0:
            logger.error(f"Failed to restart containerd on {node['ip']}")
            return result.return_code

        # Restart kubelet
        result = run_command(node, " ".join(cmd_kubelet))
        if result.return_code != 0:
            logger.error(f"Failed to restart kubelet on {node['ip']}")
            return result.return_code

        # Verify containerd is running correctly
        result = run_command(node, " ".join(cmd_verify_containerd))
        if result.return_code != 0 or result.stdout.strip() != "active":
            logger.error(f"Containerd is not active on {node['ip']} after restart")
            return 1

        logger.info(f"Containerd verified active on {node['ip']}")

        # Cleanup images (ignore errors)
        result = run_command(node, " ".join(cmd_img_cleanup))
        # Ignore error as image cleanup would fail as some images are still in use

        logger.info(f"Successfully configured {len(registries_added)} registries on {node['ip']}: {', '.join(registries_added)}")

    logger.info("Containerd registry configuration completed successfully")
    return 0

def _pull_images(logger, node, image_manifest_file, target):
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(sequence=4, offset=2)

    with open(GlobalOptions.image_manifest, 'r') as fp:
        images = yaml.load(fp)

    # Process metadata section of image-manifest
    image_metadata = images['images'].get('meta', {})
    registry = 'docker.io'
    return_val = True
    if 'registry' in image_metadata:
        registry = image_metadata['registry'].get('default', 'docker.io')
        if 'mirror' in image_metadata['registry']:
            if image_metadata['registry']['mirror'].get('enable', 'no') == 'yes':
                registry = image_metadata['registry']['mirror']['url']

    for artifact_name in list(images['images'][target].keys()):
        logger.info(f"Processing {artifact_name} section")
        artifact_info = images['images'][target][artifact_name]
        if artifact_name == "driver":
            continue
        if artifact_info.get('location', None) != None and artifact_info['kind'] == 'container':
            location = artifact_info['location']
            if '<registry>' in location:
                url = location.replace('<registry>', registry)
            else:
                url = location
            parsed_data = urlparse(url)
            img = f"{parsed_data.netloc}{parsed_data.path}:{artifact_info['version']}"
            crictl_img_download = ["sudo", "crictl", "pull", img]
            result = run_command(node, " ".join(crictl_img_download), timeout=1200)
            if result.return_code != 0:
                logger.error(f"Failed to download image : {img} on node {node}")
                return_val = False
            else:
                logger.info(f"Successfully downloaded image {img} on node {node}")
    result = run_command(node, "sudo crictl images")
    if result.return_code == 0:
        logger.info(f"Following images are downloaded on node {node}, {result.stdout}")
    else:
        logger.error(f"Failed to collect downloaded images from node {node}")
    return return_val

def _run_testbed_commands(logger):
    if GlobalOptions.fetch_kube_config:
        testbed_info = _load_testbed_json(GlobalOptions.testbed)
        if testbed_info:
            masters = _get_master_nodes(testbed_info)
            workers = _get_worker_nodes(testbed_info)
            logger.debug(f"Found {len(masters)} master(s) and {len(workers)} workers in the k8-cluster")
        else:
            logger.error(f"Failed to load testbed.json file {GlobalOptions.testbed} - Abort")
            sys.exit(1)
        if GlobalOptions.target in ["k8", "standalone"]:
            if masters and _fetch_kube_config(logger, masters[0]):
                logger.debug("Successfully downloaded /etc/kubernetes/admin.conf file")
            else:
                logger.error("Failed to download /etc/kubernetes/admin.conf file - Abort")
                sys.exit(1)
        elif GlobalOptions.target in ["openshift"]:
            # - Fetch http://pm.test.pensando.io/systest/oc-config/<cluster-name>.config
            # - Fetch http://pm.test.pensando.io/systest/oc-config/<cluster-name>.hosts
            cluster_name = _get_cluster_name(GlobalOptions.testbed)
            if cluster_name:
                try:
                    resp = requests.get(f"http://pm.test.pensando.io/systest/oc-config/{cluster_name}.config")
                    if resp.status_code != 200:
                        logger.error(f"Failed to fetch cluster config for {cluster_name}")
                        sys.exit(1)
                    dest_file = os.path.join(os.getenv("HOME"), ".kube", "config")
                    with open(dest_file, "wb") as fp:
                        fp.write(resp.content)

                    resp = requests.get(f"http://pm.test.pensando.io/systest/oc-config/{cluster_name}.hosts")
                    if resp.status_code != 200:
                        logger.error(f"Failed to fetch cluster config for {cluster_name}")
                        sys.exit(1)
                    dest_file = os.path.join("/etc", "hosts")
                    with open(dest_file, "ab") as fp:
                        fp.write(resp.content)
                except Exception as e:
                    logger.error(f"Failed to fetch openshift cluster config and hosts files, {e}")
                    sys.exit(1)
            else:
                logger.error(f"Unknown cluster - missing cluster-name in the testbed.json file")
                sys.exit(1)
    if GlobalOptions.reboot_workers:
        testbed_info = _load_testbed_json(GlobalOptions.testbed)
        if testbed_info:
            if testbed_info.get("reboot-workers", "no") == "yes":
                masters = _get_master_nodes(testbed_info)
                workers = _get_worker_nodes(testbed_info)
                logger.debug(f"Found {len(masters)} master(s) and {len(workers)} workers in the k8-cluster")
                if workers and _reboot_workers(logger, masters[0], workers):
                    logger.info("Successfully rebooted all worker nodes - cluster is ready to use")
                else:
                    logger.error("Failed to reboot all worker nodes - test-result could be unreliable")
                    sys.exit(1)
        else:
            logger.error(f"Failed to load testbed.json file {GlobalOptions.testbed} - Abort")
            sys.exit(1)

def _run_image_commands(logger):
    if GlobalOptions.load_images:
        registry_info = _load_registries_info(GlobalOptions.registries)
        if _load_images(logger, registry_info, GlobalOptions.seed_image_manifest,
                        GlobalOptions.image_manifest, GlobalOptions.target):
            logger.info(f"Images loaded to specified registry, Successfully generated image-manifest - {GlobalOptions.image_manifest}")
        else:
            logger.error(f"Failed to load images into the registry: {registry_info}")
            sys.exit(1)
    if GlobalOptions.setup_insecure_registry:
        testbed_info = _load_testbed_json(GlobalOptions.testbed)
        registry_info = _load_registries_info(GlobalOptions.registries)
        if GlobalOptions.target in ["k8", "standalone"]:
            if _upload_docker_daemon_conf(logger, registry_info, testbed_info) == 0:
                logger.info("Successfully uploaded new /etc/docker/daemon.json to all nodes")
            else:
                logger.warning(f"Failed to upload /etc/docker/daemon.json to all nodes - ignoring this error")
            if _upload_crio_registry_conf(logger, registry_info, testbed_info) == 0:
                logger.info("Successfully uploaded new /etc/containers/registries.conf.d/runner-registry.conf to all nodes")
            else:
                logger.warning(f"Failed to upload /etc/containers/registries.conf.d/runner-registry.conf to all nodes - ignoring")
            if _upload_containerd_registry_conf(logger, registry_info, testbed_info) == 0:
                logger.info("Successfully updated containerd registry configuration on all nodes")
            else:
                logger.warning(f"Failed to update containerd registry configuration on all nodes - ignoring")
        elif GlobalOptions.target in ["openshift"]:
            # Patch machine-config on openshift cluster to make regsitry insecure (only if it is insecure)

            # Driver-registry is not applicable to openshift
            # Image-registry - update only if it is insecure
            # Remove older/stale insecure registry
            patch_json = {
                "spec": {
                    "registrySources": {
                        "insecureRegistries": [
                            "registry.test.pensando.io:5000",
                        ]
                    }
                }
            }
            kubectl_patch_cmd = ["kubectl", "patch", "image.config.openshift.io/cluster", "--type=merge",
                                 "--patch", json.dumps(patch_json)]
            result = subprocess.run(kubectl_patch_cmd, stdout=subprocess.PIPE, stderr = subprocess.PIPE)
            if result.returncode != 0:
                logger.error(f"Failed to run kubectl_patch_cmd : {kubectl_patch_cmd}, error: {result}")
                sys.exit(1)

            if registry_info["image-registry"]["secure"] == "no":
                # Include new self-hosted registry
                patch_json = {
                    "spec": {
                        "registrySources": {
                            "insecureRegistries": [
                                registry_info["image-registry"]["value"],
                                "registry.test.pensando.io:5000",
                            ]
                        }
                    }
                }
                kubectl_patch_cmd = ["kubectl", "patch", "image.config.openshift.io/cluster", "--type=merge",
                                     "--patch", json.dumps(patch_json)]
                result = subprocess.run(kubectl_patch_cmd, stdout=subprocess.PIPE, stderr = subprocess.PIPE)
                if result.returncode != 0:
                    logger.error(f"Failed to run kubectl_patch_cmd : {kubectl_patch_cmd}, error: {result}")
                    sys.exit(1)

    if GlobalOptions.pull_images:
        if _is_pull_images_enabled(GlobalOptions.testbed):
            testbed_info = _load_testbed_json(GlobalOptions.testbed)
            logger.info("pulling images for this cluster")
            workers = _get_worker_nodes(testbed_info)
            for node in workers:
                if _pull_images(logger, node, GlobalOptions.image_manifest, GlobalOptions.target):
                    logger.info("Successfully downloaded images in each node (for speedup)")
                else:
                    logger.warning("Failed to download images in each node - ignoring error")
        else:
            logger.info("not pulling images for this cluster")

def _generate_report(logger):
    import xmltodict
    import glob
    from pathlib import Path
    import prettytable

    test_results = dict()
    for report_xml in  glob.iglob("logs/**/*.xml", recursive = True):
        testsuite_name = Path(report_xml).stem
        with open(report_xml) as xml_file:
            tb_result_data = xmltodict.parse(xml_file.read())
            test_results[testsuite_name] = tb_result_data

    if not test_results:
        print(f"Test Result: NA (no test results available)")
        return False

    final_status = "Success"
    ts_statistcs = dict()
    for ts_name, results in test_results.items():
        result_table = prettytable.PrettyTable()
        result_table.title = f"Testsuite : {ts_name}"
        result_table.field_names = ["Test Module", "TestCase", "Status", "Time"]
        result_table.align["Test Module"] = 'l'
        result_table.align["TestCase"] = 'l'
        result_table.hrules = prettytable.prettytable.HRuleStyle.FRAME
        result_table.padding_width = 2

        module_name = ""
        module_statistics = dict()
        for idx, test in enumerate(results['testsuites']['testsuite']['testcase']):
            mod_name = test['@classname']
            row_entry = []
            if module_name != mod_name:
                module_name = mod_name
                row_entry.append(module_name)
                module_statistics[module_name] = { 
                    'tm_pass_count' : 0,
                    'tm_fail_count' : 0,
                    'tm_skip_count' : 0,
                    'module_time'   : 0.0,
                }
            else:
                row_entry.append("")

            if test.get('failure', None) or test.get('error', None):
                module_statistics[module_name]['tm_fail_count'] = module_statistics[module_name]['tm_fail_count'] + 1
                row_entry.extend([test['@name'], 'Failure', f"{test['@time']}s"])
                final_status = "Failure"
            elif test.get('skipped', None):
                module_statistics[module_name]['tm_skip_count'] = module_statistics[module_name]['tm_skip_count'] + 1
                row_entry.extend([test['@name'], 'Skipped', f"{test['@time']}s"])
            else:
                module_statistics[module_name]['tm_pass_count'] = module_statistics[module_name]['tm_pass_count'] + 1
                row_entry.extend([test['@name'], 'Success', f"{test['@time']}s"])
            result_table.add_row(row_entry)
            module_statistics[module_name]['module_time'] = module_statistics[module_name]['module_time'] + float(test['@time'])

        print("")
        print(result_table)
        print("")

        summary_table = prettytable.PrettyTable()
        summary_table.field_names = ["Test Module", "Passed", "Skipped", "Failed", "Total", "Success %", "Failure %", "Skip %", "Total Time"]
        summary_table.hrules = prettytable.prettytable.HRuleStyle.FRAME
        summary_table.align['Test Module'] = 'l'
        summary_table.padding_width = 2

        ts_testcases = 0
        ts_failures = 0
        ts_success = 0
        ts_skips = 0
        ts_time = 0
        for module_name, stats in module_statistics.items():
            total = stats['tm_pass_count'] + stats['tm_fail_count'] + stats['tm_skip_count']
            success = float(stats['tm_pass_count']/total) * 100.0
            skip = float(stats['tm_skip_count']/total) * 100.0
            failure = float(stats['tm_fail_count']/total) * 100.0
            module_time = stats['module_time']
            summary_table.add_row([module_name, stats['tm_pass_count'], stats['tm_skip_count'], stats['tm_fail_count'], total,
                                   f"{success:.2f}", f"{failure:.2f}", f"{skip:.2f}", f"{module_time/60:.2f}m"])
            ts_testcases += total
            ts_success += stats['tm_pass_count']
            ts_failures += stats['tm_fail_count']
            ts_skips += stats['tm_skip_count']
            ts_time += module_time

        print("")
        print(summary_table)

        ts_statistcs[ts_name] = (len(module_statistics), ts_success, ts_skips, ts_failures, ts_time)

    summary_table = prettytable.PrettyTable()
    summary_table.title = "Overall Status"
    summary_table.padding_width = 2
    summary_table.field_names = ["Testsuite", "Modules", "Passed", "Skipped", "Failed", "Total", "Success %", "Failure %", "Skipped %", "Total Time"]

    for ts_name, stats in ts_statistcs.items():
        modules, ts_success, ts_skips, ts_failures, ts_time = stats
        total = ts_success + ts_skips + ts_failures
        success = float(ts_success/total) * 100.0
        skip = float(ts_skips/total) * 100.0
        failure = float(ts_failures/total) * 100.0
        summary_table.add_row([ts_name, modules, ts_success, ts_skips, ts_failures, total,
                                f"{success:.2f}", f"{failure:.2f}", f"{skip:.2f}", f"{ts_time/60:.2f}m"])
    print("")
    print(summary_table)
    print(f"Test Result: {final_status}")
    print("")
    return True

def _run_report_commands(logger):
    if GlobalOptions.show:
        _generate_report(logger)

def main():
    parser = _init_cmdline_args()
    logger = _init_logger()

    if GlobalOptions.command == 'testbed':
        _run_testbed_commands(logger)
    elif GlobalOptions.command == 'image':
        _run_image_commands(logger)
    elif GlobalOptions.command == "report":
        _run_report_commands(logger)
    else:
        parser.print_help()
    return


if __name__ == '__main__':
    main()
