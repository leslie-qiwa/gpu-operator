
#!/usr/bin/python3

'''
 Copyright (c) Advanced Micro Devices, Inc. All rights reserved.

 Licensed under the Apache License, Version 2.0 (the \"License\");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an \"AS IS\" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
'''

import pytest
import pdb
import os
import re
import json
import shutil
import requests
import logging
from datetime import datetime
from lib import common
from lib import k8_util
import lib.amdgpu as amdgpu_util
from py.xml import html
from pathlib import Path
from urllib.parse import urlparse
import getpass

Logger = logging.getLogger("root.conftest")
logging.getLogger("paramiko").setLevel(logging.WARNING)
logging.getLogger('invoke').setLevel(logging.WARNING)
logging.getLogger('kubernetes').setLevel(logging.WARNING)

def pytest_addoption(parser):
    parser.addoption(
            "--testbed",
            action="store",
            default=None,
            help="Testbed JSON file with details about platform/cluster",
    )

    parser.addoption(
            "--deployment",
            action = "store",
            default = "k8",
            choices = ["k8", "openshift", "standalone"],
            help = "Deployment model to test against",
    )

    parser.addoption(
            "--image-manifest",
            action = "store",
            default = None,
            required = True,
            help = "Image manifest listing images to use for testing"
    )

    parser.addoption(
            "--alternative-image-manifest",
            action = "store",
            default = None,
            help = "Alternative Image manifest listing images to use for upgrade testing"
    )

    parser.addoption(
            "--secrets-json",
            action = "store",
            default = None,
            help = "K8 secrets json file"
    )

    parser.addoption(
            "--amdgpu-driver-spec",
            action = "store",
            default = "lib/files/amd-deviceconfig-default-driver-spec.json",
            required = False,
            help = "AMDGPU Driver to use"
    )

    parser.addoption(
            "--tech-support-tool",
            action="store",
            default=None,
            help="Path to tech-support tool to collect information",
    )
    parser.addoption(
            "--workload-selection",
            action="store",
            default="alexnet-tf-gpu",
            help="Workload template to use",
    )

def pytest_html_results_summary(prefix, summary, postfix):
    # Insert custom HTML into the summary section of the report
    '''
    prefix.extend([html.h3("Testbed Information")])
    summary.extend([html.h3("Additional Summary Information")])
    postfix.extend([html.h3("Post Run Information")])
    '''
    if hasattr(pytest, "_amdgpu_driver_spec"):
        ver = pytest._amdgpu_driver_spec.get('default-version', 'NA')
        deployment_mode = pytest._amdgpu_driver_spec.get('driver-deployment', 'NA')
        summary.append(html.h3(f"AMDGPU Driver Version : {ver}/{deployment_mode}"))
        summary.append(html.br())
    if hasattr(pytest, "_k8_cluster_inst") and hasattr(pytest, "_nodes_version"):
        summary.append(html.h3("Cluster Information"))
        summary.append(cluster_info_table())
        summary.append(html.br())
    if hasattr(pytest, "_image_info"):
        summary.append(html.h3("Images Used"))
        summary.append(transform_image_info())
        summary.append(html.br())
    
def cluster_info_table():
    gpu_series_by_host = {
        node.host_name: node.gpu_series
        for node in pytest._k8_cluster_inst.cluster_nodes
    }

    table_style = "border: 1px solid black; border-collapse: collapse; min-width: 300px; margin-bottom: 10px;"
    cell_style = "border: 1px solid black; padding: 5px; min-width: 50px;"

    table = html.table(style=table_style)
    header_row = html.tr([
        html.th("Node Name", scope="col", style=cell_style),
        html.th("K8-Version", scope="col", style=cell_style),
        html.th("GPU-Series", scope="col", style=cell_style),
    ])
    table.append(header_row)

    for node_name, k8_version in pytest._nodes_version.items():
        table.append(html.tr([
            html.td(node_name, scope="col", style=cell_style),
            html.td(k8_version, scope="col", style=cell_style),
            html.td(gpu_series_by_host.get(node_name, "N/A"), scope="col", style=cell_style),
        ]))

    return table

def transform_image_info():
    # convert dict image_info to table format
    pytest._image_info.pop("image_folder")
    transformed_dict = {}
 
    for key, value in  pytest._image_info.items():
        sub_key = key.split(".")[-1]
        base_key = ".".join(key.split(".")[:-1])
        if value:
            transformed_dict.setdefault(base_key, {})[sub_key] = value

    table_style = "border: 1px solid black; border-collapse: collapse; min-width: 300px; margin-bottom: 10px;"
    cell_style = "border: 1px solid black; padding: 5px; min-width: 50px;"
    table = html.table(style=table_style)
    header_row = html.tr([
        html.th("Image", scope="col", style=cell_style),
        html.th("Location", scope="col", style=cell_style),
        html.th("Version", scope="col", style=cell_style),
        ])
    table.append(header_row)

    for key, value in transformed_dict.items():
        if 'repository' in value.keys() or 'version' in value.keys():
            row = html.tr([
                  html.td(key, scope="col", style=cell_style),
                  html.td(value.get('repository', 'N/A'), scope="col", style=cell_style),
                  html.td(value.get('version', 'N/A'), scope="col", style=cell_style),
                  ])
            table.append(row)
    return table

@pytest.hookimpl(optionalhook=True)
def pytest_metadata(metadata):
    metadata.clear()

@pytest.fixture(scope="function", autouse=True)
def update_environment(request, environment):
    global Logger
    setattr(environment, 'current_tc_name', request.node.name)
    Logger.debug(f"Starting Testcase: {request.node.name}")
    yield
    delattr(environment, 'current_tc_name')
    Logger.debug(f"Testcase Completed: {request.node.name}")

@pytest.fixture(scope="session")
def environment(request):
    global Logger
    class Env(object):
        pass

    tenv = Env()
    setattr(tenv, 'deployment_mode', request.config.option.deployment)
    setattr(tenv, 'download_folder', 'downloads')
    setattr(tenv, 'logdir', "logs")
    if request.config.option.amdgpu_driver_spec:
        with open(request.config.option.amdgpu_driver_spec, "r") as fp:
            driver_spec = json.load(fp)
            setattr (pytest, "_amdgpu_driver_spec", driver_spec)
            setattr(tenv, 'amdgpu_driver_spec', driver_spec)
    kube_config_file = os.path.join(Path.home(), ".kube", "config")
    if os.path.exists(kube_config_file):
        setattr(tenv, 'kube_config_file', kube_config_file)
    else:
        pytest.fail("Failed to find kube_config_file for cluster operator - Aborting")

    # Secrets file
    secrets_json_file = os.path.join(Path.home(), ".kube", "secrets.json")
    if request.config.option.secrets_json:
        secrets_json_file = request.config.option.secrets_json
    if os.path.exists(secrets_json_file):
        setattr(tenv, 'k8_secrets_file', secrets_json_file)

    # Tech-support tool
    setattr(tenv, 'tech_support_tool', None)
    if request.config.option.tech_support_tool:
        if os.path.exists(request.config.option.tech_support_tool):
            tst_info = {
                "tool" : request.config.option.tech_support_tool,
                "args" : [],
            }
            setattr(tenv, 'tech_support_tool', tst_info)
            os.makedirs(os.path.join(tenv.logdir, "tech-support"), exist_ok=True)

    # Workload Template
    setattr(tenv, 'default_workload', request.config.option.workload_selection)
    setattr(tenv, 'exporter_namespace', os.getenv('EXPORTER_NAMESPACE', 'kube-amd-exporter'))
    setattr(tenv, 'gpu_operator_namespace', os.getenv('GPU_OPERATOR_NAMESPACE', 'kube-amd-gpu'))
    setattr(tenv, "amd_smi_collection_complete", False)
    return tenv

@pytest.fixture(scope="session")
def gpu_cluster(request, environment):
    global Logger
    k8_util.k8_lib_init(environment.kube_config_file)
    ret_code, k8_nodes = k8_util.k8_get_nodes()
    assert ret_code == 0, "Failed to collect nodes from cluster"
    nodes = list()
    nodes_version = {}
    for node in k8_nodes:
        nodes_version[node['metadata']['name']] = node['status']['node_info']['kubelet_version']
        node_ip = k8_util.k8_get_node_address(node)
        if 'node-role.kubernetes.io/control-plane' in node['metadata']['labels']:
            nodes.append(common.Node(node_ip, None, None, None, "master", None))
        else:
            nodes.append(common.Node(node_ip, None, None, None, "worker", None))
    k8_cluster_inst = common.k8_cluster.BuildK8Cluster(nodes)
    k8_cluster_inst.k8_kube_config = environment.kube_config_file
    assert len(k8_cluster_inst.cluster_nodes) > 0, f"Failed to collect worker nodes from k8/cluster"
    if hasattr(environment, "k8_secrets_file"):
        with open(environment.k8_secrets_file) as fp:
            k8_cluster_inst.k8_secrets = json.load(fp)
    setattr(pytest, "_nodes_version",  nodes_version)
    setattr(pytest, "_k8_cluster_inst", k8_cluster_inst)
    return k8_cluster_inst

@pytest.fixture(scope="session")
def images(request, gpu_cluster, environment):
    image_info = None
    from ruamel.yaml import YAML
    from ruamel.yaml import comments
    from ruamel.yaml import scalarstring
    import shutil

    yaml = YAML()
    yaml.preserve_quotes = True

    file_obj = Path(request.config.option.image_manifest)
    if not file_obj.exists():
        pytest.fail(f"Missing {request.config.option.image_manifest}")

    image_manifest = dict(yaml.load(file_obj))

    # Process metadata section of image-manifest
    image_metadata = image_manifest['images'].get('meta', {})
    registry = 'docker.io'
    if 'registry' in image_metadata:
        registry = image_metadata['registry'].get('default', 'docker.io')
        if 'mirror' in image_metadata['registry']:
            if image_metadata['registry']['mirror'].get('enable', 'no') == 'yes':
                registry = image_metadata['registry']['mirror']['url']
    setattr(environment, 'default_registry', registry)
    if 'packaging' in image_metadata:
        if image_metadata['packaging'].get('gpuctl', 'enabled') == 'disabled':
            setattr(environment, "builtin_gpuctl_support", False)
        else:
            setattr(environment, "builtin_gpuctl_support", True)
    else:
        setattr(environment, "builtin_gpuctl_support", True)
    assert environment.deployment_mode in image_manifest['images'], f"Missing images for {environment.deployment_mode}"
    if environment.deployment_mode == "standalone":
        image_info = _build_image_info(environment, image_manifest['images'])

    if environment.deployment_mode in ["k8", "openshift"]:
        image_info = _build_image_info(environment, image_manifest['images'])

    assert image_info != None, f"Failed to build images for {environment.deployment_mode}"
    gpu_cluster.k8_registry = environment.default_registry
    image_info['driver.imageBuild.baseImageRegistry'] = environment.default_registry
    setattr(pytest, "_image_info", image_info)
    return image_info

@pytest.fixture(scope="session")
def alternative_images(request, gpu_cluster, environment):
    image_info = None
    from ruamel.yaml import YAML
    from ruamel.yaml import comments
    from ruamel.yaml import scalarstring
    import shutil

    yaml = YAML()
    yaml.preserve_quotes = True

    if not request.config.option.alternative_image_manifest:
        pytest.skip("No alterative image-manifest given. Skipping associated testcases")

    file_obj = Path(request.config.option.alternative_image_manifest)
    if not file_obj.exists():
        pytest.fail("Missing alterative image-manifest")

    image_manifest = dict(yaml.load(file_obj))

    # Process metadata section of image-manifest
    image_metadata = image_manifest['images'].get('meta', {})
    registry = 'docker.io'
    if 'registry' in image_metadata:
        registry = image_metadata['registry'].get('default', 'docker.io')
        if 'mirror' in image_metadata['registry']:
            if image_metadata['registry']['mirror'].get('enable', 'no') == 'yes':
                registry = image_metadata['registry']['mirror']['url']
    assert environment.deployment_mode in image_manifest['images'], f"Missing images for {environment.deployment_mode}"
    if environment.deployment_mode == "standalone":
        image_info = _build_image_info(environment, image_manifest['images'])

    if environment.deployment_mode in ["k8", "openshift"]:
        image_info = _build_image_info(environment, image_manifest['images'])

    assert image_info != None, f"Failed to build images for {environment.deployment_mode}"
    gpu_cluster.k8_registry = environment.default_registry
    image_info['driver.imageBuild.baseImageRegistry'] = environment.default_registry
    setattr(pytest, "_alternative_image_info", image_info)
    return image_info

def _build_image_info(environment, image_manifest):
    '''
    Build image-info used for testing
    '''
    global Logger
    image_info = dict()

    images = image_manifest[environment.deployment_mode]
    # prepare to download gpu-operator
    if images.get('gpu-operator', None) and images['gpu-operator']['kind'] in ['helm-chart', 'olm-bundle']:
        setattr(environment, 'gpu_operator_version', images['gpu-operator']['version'])
    if images.get('exporter', None) and images['exporter']['kind'] == 'helm-chart':
        setattr(environment, 'exporter_version', images['exporter']['version'])

    os.makedirs(environment.download_folder, exist_ok=True)
    image_info['image_folder'] = environment.download_folder

    for artifact, artifact_info in images.items():
        Logger.debug(f"Processing {artifact}")
        if 'repo://' in artifact_info['location']:
            pattern = r"repo://([a-zA-Z0-9.-]+/[^:]+):([^/]+)"
            match = re.search(pattern, artifact_info['location'])
            if match:
                image_info[f'{artifact}.repo-name'] = f"{artifact}-repo"
                image_info[f'{artifact}.repo'] = f"https://{match.group(1)}"
                image_info[f'{artifact}.repository'] = f"https://{match.group(1)}"
                image_info[f'{artifact}.helm-chart'] = f"{artifact}-repo/{match.group(2)}"
            else:
                pytest.fail(f"Failed to parse repo information from {artifact_info['location']}")
        elif 'file://' in artifact_info['location']:
            # Copy the files
            local_file = artifact_info['location'].split('file://')[-1]
            if not os.path.exists(local_file):
                pytest.fail(f"Invalid file name or path not found : {local_file}")
            # If not local, upload these files to the master node
            file_path = local_file
            if artifact_info['kind'] == 'helm-chart':
                image_info[f'{artifact}.helm-chart'] = file_path
                image_info[f'{artifact}.helm-chart.version'] = artifact_info['version']
                image_info[f'{artifact}.helm-chart.repository'] = file_path
            elif artifact_info['kind'] == 'olm-bundle':
                image_info[f'{artifact}.olm-bundle'] = file_path
                image_info[f'{artifact}.olm-bundle.version'] = artifact_info['version']
                image_info[f'{artifact}.olm-bundle.repository'] = file_path
                if 'secret' in artifact_info:
                    image_info[f"{artifact}.olm-bundle.secret"] = artifact_info['secret']
            elif artifact_info['kind'] == 'debian':
                image_info[f'{artifact}.debian'] = file_path
                image_info[f'{artifact}.debian.version'] = artifact_info['version']
                image_info[f'{artifact}.debian.repository'] = file_path
        elif 'http://' in artifact_info['location'] or 'https://' in artifact_info['location']:
            # Download the file
            url = artifact_info['location']
            local_file = os.path.join(environment.download_folder, os.path.basename(urlparse(url).path))
            if not os.path.exists(local_file):
                try:
                    resp = requests.get(url)
                    if resp.status_code == 200:
                        with open(local_file, 'wb') as fp:
                            fp.write(resp.content)
                    else:
                        raise Exception(f"Failed to download file {local_file}, error: {resp.status_code}")
                except Exception as e:
                    Logger.error(f"Failed to download {local_file} from {url}, error : {e}")
                    pytest.fail("Could not download images - abort")

            file_path = local_file
            if artifact_info['kind'] == 'helm-chart':
                image_info[f'{artifact}.helm-chart'] = file_path
            elif artifact_info['kind'] == 'olm-bundle':
                image_info[f'{artifact}.olm-bundle'] = file_path
                if 'secret' in artifact_info:
                    image_info[f"{artifact}.olm-bundle.secret"] = artifact_info['secret']
        elif 'container://' in artifact_info['location']:
            location = artifact_info['location']
            if '<registry>' in location and environment.default_registry:
                url = location.replace('<registry>', environment.default_registry)
            else:
                url = location
            parsed_data = urlparse(url)
            if artifact_info['kind'] == 'container':
                image_info[f"{artifact_info['key']}.repository"] = f"{parsed_data.netloc}{parsed_data.path}"
                if artifact_info.get('version'):
                    image_info[f"{artifact_info['key']}.version"] = artifact_info['version']
                if 'secret' in artifact_info:
                    image_info[f"{artifact_info['key']}.secret"] = artifact_info['secret']
            elif artifact_info['kind'] == 'olm-bundle':
                version = artifact_info['version']
                image_info[f'{artifact}.olm-bundle'] = f"{parsed_data.netloc}{parsed_data.path}:{version}"
                image_info[f'{artifact}.olm-bundle.version'] = version
                image_info[f'{artifact}.olm-bundle.repository'] = f"{parsed_data.netloc}{parsed_data.path}"
                if 'secret' in artifact_info:
                    image_info[f'{artifact}.olm-bundle.secret'] = artifact_info['secret']
    return image_info

@pytest.fixture(scope="session", autouse=True)
def gather_device_info(gpu_cluster, images, environment):
    # Derive gpu information using shared node_gpu_collector helper
    import lib.node_gpu_collector as node_collector

    success, error_msg = node_collector.populate_all_cluster_nodes_with_gpu_info(gpu_cluster)
    if not success:
        pytest.exit(f"Failed to collect node GPU information: {error_msg}")

    Logger.info("Collected amd-gpu information for all cluster nodes")

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()

    if report.when == 'call':
        # Get the docstring from the test function
        description = str(item.function.__doc__) if item.function.__doc__ else ""

        # Add the description to the report object
        if description:
            report.description = description

        if report.failed:
            # 1. Get the raw error message
            error_msg = str(call.excinfo.value) if call.excinfo else "Unknown Error"

            # Store these on the report object so the table hooks can see them
            report.error_summary = error_msg[:50] + "..." # Truncated message
        else:
            # Default values for passing tests
            report.error_summary = "-"

def pytest_html_results_table_header(cells):
    cells.insert(2, html.th("Description"))
    cells.insert(3, html.th("Failure Message"))

def pytest_html_results_table_row(report, cells):
    # Retrieve the description we stored in the previous hook
    description = getattr(report, 'description', "")
    cells.insert(2, html.td(description))
    msg = getattr(report, 'error_summary', "-")
    cells.insert(3, html.td(msg))
