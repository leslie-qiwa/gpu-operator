
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
    parser.addoption(
            "--base-version",
            action="store",
            default=None,
            help="Base version for upgrade tests (e.g., v1.4.1)"
    )

def pytest_html_results_summary(prefix, summary, postfix):
    """
    Add custom information to the summary section of the report.

    Adds Environment info (AMDGPU Driver, Cluster Nodes) and Images table
    to the prefix section in a 2-column grid layout.
    """
    # Build left column (Images Used)
    left_column_content = []
    if hasattr(pytest, "_image_info"):
        left_column_content.extend([
            html.h3("Images"),
            transform_image_info(),
        ])

    # Add test results summary table to the summary section
    # The summary section shows test result counts
    # We'll add a styled table showing the breakdown
    results_table = None
    if hasattr(pytest, "_config"):
        # Get test results from terminalreporter
        config = pytest._config
        if hasattr(config, 'pluginmanager'):
            terminalreporter = config.pluginmanager.get_plugin('terminalreporter')
            if terminalreporter:
                stats = terminalreporter.stats

                # Count results by category
                passed = len(stats.get('passed', []))
                failed = len(stats.get('failed', []))
                skipped = len(stats.get('skipped', []))
                error = len(stats.get('error', []))
                xfailed = len(stats.get('xfailed', []))
                xpassed = len(stats.get('xpassed', []))
                rerun = len(stats.get('rerun', []))

                # Create results summary table
                table_style = "border: 1px solid black; border-collapse: collapse; min-width: 400px; margin: 20px 0;"
                cell_style = "border: 1px solid black; padding: 10px; min-width: 50px;"

                results_table = html.table(style=table_style)
                results_table.append(html.tr([
                    html.th("Result Type", scope="col", style=cell_style),
                    html.th("Count", scope="col", style=cell_style),
                ]))

                # Add rows for each result type
                results_table.append(html.tr([
                    html.td("Failed", style=cell_style + " color: #dc3545; font-weight: 600;"),
                    html.td(str(failed), style=cell_style + " text-align: center; font-weight: 600;"),
                ]))
                results_table.append(html.tr([
                    html.td("Passed", style=cell_style + " color: #28a745; font-weight: 600;"),
                    html.td(str(passed), style=cell_style + " text-align: center; font-weight: 600;"),
                ]))
                results_table.append(html.tr([
                    html.td("Skipped", style=cell_style + " color: #ffc107; font-weight: 600;"),
                    html.td(str(skipped), style=cell_style + " text-align: center; font-weight: 600;"),
                ]))
                results_table.append(html.tr([
                    html.td("Expected Failures", style=cell_style),
                    html.td(str(xfailed), style=cell_style + " text-align: center;"),
                ]))
                results_table.append(html.tr([
                    html.td("Unexpected Passes", style=cell_style),
                    html.td(str(xpassed), style=cell_style + " text-align: center;"),
                ]))
                results_table.append(html.tr([
                    html.td("Errors", style=cell_style + " color: #dc3545;"),
                    html.td(str(error), style=cell_style + " text-align: center;"),
                ]))
                results_table.append(html.tr([
                    html.td("Reruns", style=cell_style),
                    html.td(str(rerun), style=cell_style + " text-align: center;"),
                ]))


    # Build right column (AMDGPU Driver Version and Cluster Nodes)
    right_column_content = []
    if hasattr(pytest, "_k8_cluster_inst"):
        right_column_content.extend([
            html.h3("Cluster Nodes"),
            cluster_info_table(),
        ])

    # If result-table is available, add it below Cluster Nodes
    if results_table:
        right_column_content.extend([
            html.h3("Test Results Summary"),
            results_table,
        ])

    if hasattr(pytest, "_amdgpu_driver_spec"):
        ver = pytest._amdgpu_driver_spec.get('default-version', 'NA')
        deployment_mode = pytest._amdgpu_driver_spec.get('driver-deployment', 'NA')
        right_column_content.extend([
            html.h3("AMDGPU Driver Version", style="margin-top: 30px;"),
            html.p(
                html.strong(f"Version: {ver} | Deployment: {deployment_mode}"),
                style="font-size: 16px; color: #212529; margin: 10px 0;"
            ),
        ])

    # Create 2-column grid layout with 60/40 split
    if left_column_content or right_column_content:
        grid_container = html.div(
            html.div(*left_column_content, style="grid-column: 1;") if left_column_content else html.div(),
            html.div(*right_column_content, style="grid-column: 2;") if right_column_content else html.div(),
            style="display: grid; grid-template-columns: 60% 40%; gap: 30px; margin: 20px 0;"
        )
        prefix.append(grid_container)
        prefix.append(html.br())

    
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
        html.th("IP Address", scope="col", style=cell_style),
        html.th("GPU-Series", scope="col", style=cell_style),
        html.th("GPU Count", scope="col", style=cell_style),
        html.th("K8-Version", scope="col", style=cell_style),
        html.th("Host OS Type", scope="col", style=cell_style),
        html.th("Host OS Name", scope="col", style=cell_style),
        html.th("Host OS Version", scope="col", style=cell_style),
    ])
    table.append(header_row)

    for node in pytest._k8_cluster_inst.cluster_nodes:
        table.append(html.tr([
            html.td(node.host_name, scope="col", style=cell_style),
            html.td(node.ip_address, scope="col", style=cell_style),
            html.td(node.gpu_series, scope="col", style=cell_style),
            html.td(node.num_gpus, scope="col", style=cell_style),
            html.td(node.k8_version, scope="col", style=cell_style),
            html.td(node.host_os_type, scope="col", style=cell_style),
            html.td(node.host_os_name, scope="col", style=cell_style),
            html.td(node.host_os_version, scope="col", style=cell_style)
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

def pytest_metadata(metadata):
    """
    Populate metadata with command-line options.

    This hook is called by pytest-metadata plugin and receives the metadata dict.
    We populate it with our custom command-line options and remove default pytest
    metadata fields that are not relevant for our test reports.

    Note: Custom environment information (AMDGPU Driver, Cluster Nodes, Images)
    is added via pytest_html_results_summary hook instead, which runs after
    fixtures execute and have populated the necessary pytest._ attributes.
    """
    # Remove default pytest-html metadata fields we don't need in the report
    fields_to_remove = ['Python', 'Platform', 'Packages', 'Plugins']
    for field in fields_to_remove:
        metadata.pop(field, None)

    # This hook runs after pytest_configure, so config should be available
    if hasattr(pytest, "_config"):
        config = pytest._config
        option = config.option

        # Add custom pytest options we defined in pytest_addoption
        custom_options = {
            'Deployment': getattr(option, 'deployment', None),
            'Image Manifest': getattr(option, 'image_manifest', None),
            'Alternative Image Manifest': getattr(option, 'alternative_image_manifest', None),
            'Secrets JSON': getattr(option, 'secrets_json', None),
            'AMDGPU Driver Spec': getattr(option, 'amdgpu_driver_spec', None),
            'Tech Support Tool': getattr(option, 'tech_support_tool', None),
            'Workload Selection': getattr(option, 'workload_selection', None),
        }

        for key, value in custom_options.items():
            if value:
                metadata[key] = value

class Context(object):
    pass

@pytest.fixture(scope="function", autouse=True)
def context(request, environment):
    global Logger
    environment.context = Context()
    setattr(environment.context, 'current_tc_name', request.node.name)
    Logger.debug(f"Starting Testcase: {request.node.name}")
    yield
    environment.context = None
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
    tenv.context = Context()
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
    tech_support_path = None

    # Use user-provided tool if specified
    if request.config.option.tech_support_tool:
        if os.path.exists(request.config.option.tech_support_tool):
            tech_support_path = request.config.option.tech_support_tool
    else:
        # Fall back to default tech-support script
        default_script = os.path.join(os.path.dirname(__file__), "scripts", "default-tech-support.sh")
        if os.path.exists(default_script):
            tech_support_path = default_script
            Logger.info(f"Using default tech-support script: {default_script}")

    if tech_support_path:
        tst_info = {
            "tool": tech_support_path,
            "args": [],
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
    for node in k8_nodes:
        node_name = node['metadata']['name']
        k8_version = node['status']['node_info']['kubelet_version']
        node_ip = k8_util.k8_get_node_address(node)
        if 'node-role.kubernetes.io/control-plane' in node['metadata']['labels']:
            nodes.append(common.Node(node_ip, None, None, None, "master", k8_version, node_name))
        else:
            nodes.append(common.Node(node_ip, None, None, None, "worker", k8_version, node_name))
    k8_cluster_inst = common.k8_cluster.BuildK8Cluster(nodes)
    k8_cluster_inst.k8_kube_config = environment.kube_config_file
    assert len(k8_cluster_inst.cluster_nodes) > 0, f"Failed to collect nodes from k8/cluster"
    if hasattr(environment, "k8_secrets_file"):
        with open(environment.k8_secrets_file) as fp:
            k8_cluster_inst.k8_secrets = json.load(fp)
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

    # Optional metadata validation (warn if missing, don't fail)
    if 'operator' not in image_metadata:
        Logger.warning(f"{file_obj.name}: missing 'operator' field in images.meta")
    if 'version' not in image_metadata:
        Logger.warning(f"{file_obj.name}: missing 'version' field in images.meta")

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
def all_image_versions(request, environment):
    """
    Load all released operator image manifests from image-manifest/ directory.

    Scans tests/pytests/image-manifest/{operator}/ subdirectories
    and loads all *_external_images.yaml files.

    Returns:
        dict[str, dict[str, dict]]: Nested dictionary structure
        {
            "gpu-operator": {
                "v1.4.1": {image_info_dict},
                "v1.4.0": {image_info_dict},
                ...
            },
            "network-operator": {
                "v1.0.0": {image_info_dict},
                ...
            }
        }

    Validation:
        - Operator from directory name should match images.meta.operator
        - Version from filename should match images.meta.version
        - Warns on mismatch but doesn't fail (allows gradual migration)
    """
    global Logger
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True

    manifest_base_dir = Path(__file__).parent / "image-manifest"
    version_map = {}

    if not manifest_base_dir.exists():
        Logger.warning(f"Image manifest directory not found: {manifest_base_dir}")
        return version_map

    # Iterate through operator subdirectories
    for operator_dir in manifest_base_dir.iterdir():
        if not operator_dir.is_dir():
            continue

        operator_type = operator_dir.name  # "gpu-operator" or "network-operator"
        version_map[operator_type] = {}

        # Load all *_external_images.yaml files in this operator directory
        for manifest_file in sorted(operator_dir.glob("*_external_images.yaml")):
            try:
                # Extract version from filename: v1.4.1_external_images.yaml -> v1.4.1
                filename_version = manifest_file.stem.replace("_external_images", "")

                # Load manifest
                manifest_data = yaml.load(manifest_file)

                if not manifest_data or 'images' not in manifest_data:
                    Logger.warning(f"{manifest_file}: Invalid manifest structure")
                    continue

                # Validate metadata (optional - warn on mismatch)
                if 'meta' in manifest_data['images']:
                    meta = manifest_data['images']['meta']

                    # Check operator field
                    if 'operator' in meta:
                        if meta['operator'] != operator_type:
                            Logger.warning(
                                f"{manifest_file.name}: metadata operator '{meta['operator']}' "
                                f"doesn't match directory '{operator_type}'"
                            )
                    else:
                        Logger.debug(f"{manifest_file.name}: missing 'operator' field in metadata")

                    # Check version field
                    if 'version' in meta:
                        if meta['version'] != filename_version:
                            Logger.warning(
                                f"{manifest_file.name}: metadata version '{meta['version']}' "
                                f"doesn't match filename '{filename_version}'"
                            )
                    else:
                        Logger.debug(f"{manifest_file.name}: missing 'version' field in metadata")
                else:
                    Logger.debug(f"{manifest_file.name}: missing 'meta' section")

                # Build image_info WITHOUT mutating environment
                # (to avoid overwriting RC version with released version)
                image_info = _build_image_info_no_env_mutation(environment, manifest_data['images'])

                # Store in nested dict
                version_map[operator_type][filename_version] = image_info

                Logger.debug(f"Loaded {operator_type} {filename_version} from {manifest_file.name}")

            except Exception as e:
                Logger.error(f"Failed to load {manifest_file.name}: {e}")
                continue

    # Log summary
    for op_type, versions in version_map.items():
        if versions:
            Logger.info(f"Loaded {len(versions)} version(s) for {op_type}: {sorted(versions.keys())}")

    return version_map

def _build_image_info_no_env_mutation(environment, image_manifest):
    '''
    Build image-info WITHOUT mutating environment object.
    Used by all_image_versions to avoid overwriting RC version with released versions.
    '''
    # Temporarily save current environment versions
    saved_gpu_op_ver = getattr(environment, 'gpu_operator_version', None)
    saved_exporter_ver = getattr(environment, 'exporter_version', None)

    # Call the regular helper
    image_info = _build_image_info(environment, image_manifest)

    # Restore original environment versions
    if saved_gpu_op_ver is not None:
        setattr(environment, 'gpu_operator_version', saved_gpu_op_ver)
    if saved_exporter_ver is not None:
        setattr(environment, 'exporter_version', saved_exporter_ver)

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

        # Format the description to preserve structure
        if description:
            # Clean up the docstring (remove common leading whitespace)
            import textwrap
            import re
            description = textwrap.dedent(description).strip()

            # Extract first paragraph as summary (before first blank line)
            parts = description.split('\n\n', 1)
            summary = parts[0].replace('\n', ' ')

            # Store only the summary (first paragraph) for the description column
            report.description = summary
        else:
            report.description = ""

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
    # Format the test ID (nodeid) to be more readable
    # Example: openshift/gpu-operator/test_metrics_values.py::test_exporter_metrics_value_accuracy[GPU_CLOCK:GPU_CLOCK_TYPE_DATA]
    # Should become multi-line format with deployment, application, module, test case, and parameters

    import re
    from py.xml import html as html_builder

    nodeid = getattr(report, 'nodeid', '')

    if nodeid:
        # Parse the nodeid components
        # Format: <path>::<test_name>[<params>]
        parts = nodeid.split('::')
        path_part = parts[0] if len(parts) > 0 else ''
        test_part = parts[1] if len(parts) > 1 else ''

        # Split path into deployment/application/module
        path_components = path_part.split('/')

        # Extract test name and parameters
        param_match = re.match(r'([^\[]+)(\[.+\])?', test_part)
        test_name = param_match.group(1) if param_match else test_part
        params = param_match.group(2) if param_match and param_match.group(2) else ''

        # Build formatted elements using html builder
        formatted_parts = []

        # Add path components (deployment/application)
        if len(path_components) > 2:
            # First component: deployment
            formatted_parts.append(
                html_builder.div(
                    html_builder.strong("Deployment: "),
                    path_components[0],
                    style="color: #6c757d; font-size: 11px;"
                )
            )
            # Second component: application
            formatted_parts.append(
                html_builder.div(
                    html_builder.strong("Application: "),
                    path_components[1],
                    style="color: #6c757d; font-size: 11px;"
                )
            )
            # Module (last component of path)
            formatted_parts.append(
                html_builder.div(
                    html_builder.strong("Module: "),
                    path_components[-1],
                    style="color: #212529; font-size: 12px;"
                )
            )
        else:
            # Just show the full path if it doesn't match expected format
            formatted_parts.append(
                html_builder.div(
                    html_builder.strong("Path: "),
                    path_part,
                    style="color: #212529; font-size: 12px;"
                )
            )

        # Add test name
        formatted_parts.append(
            html_builder.div(
                test_name,
                style="color: #212529; font-weight: 600; margin-top: 4px;"
            )
        )

        # Add parameters if present
        if params:
            # Remove brackets and format parameters
            params_clean = params.strip('[]')
            formatted_parts.append(
                html_builder.div(
                    html_builder.em(params_clean),
                    style="color: #17a2b8; font-size: 11px; margin-top: 2px;"
                )
            )

        # Replace the Test ID cell (cells[1]) with formatted version
        cells[1] = html.td(
            html_builder.div(*formatted_parts),
            style="white-space: normal; max-width: 350px;"
        )

    # Retrieve the description we stored in the previous hook
    description = getattr(report, 'description', "")
    cells.insert(2, html.td(description))
    msg = getattr(report, 'error_summary', "-")
    cells.insert(3, html.td(msg))

def pytest_html_report_title(report):
    """
    Set a custom title for the HTML report based on test suite.
    """
    report.title = "AMD GPU Operator Test Report"

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    """Add custom CSS to beautify HTML reports."""
    yield

@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "upgrade: mark test as operator/operand upgrade test"
    )

    """
    Add custom CSS styling to the HTML report for better aesthetics.
    Writes CSS to a temporary file and registers it with pytest-html.
    Also stores config for access in pytest_metadata hook.
    """
    # Store config FIRST (tryfirst ensures this runs before pytest_metadata)
    setattr(pytest, "_config", config)

    # Define custom CSS content
    css_content = """
        /* ==================== Color Scheme ==================== */
        :root {
            --amd-red: #ed1c24;
            --amd-dark: #2d2d2d;
            --primary-gradient: linear-gradient(135deg, #ed1c24 0%, #c8102e 100%);
            --success-color: #28a745;
            --warning-color: #ffc107;
            --danger-color: #dc3545;
            --info-color: #17a2b8;
            --light-bg: #f8f9fa;
            --white: #ffffff;
            --border-color: #dee2e6;
            --text-dark: #212529;
            --text-muted: #6c757d;
        }

        /* ==================== Global Styles ==================== */
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(to bottom, #f5f7fa 0%, #e9ecef 100%);
            color: var(--text-dark);
            line-height: 1.6;
            margin: 0;
            padding: 20px;
        }

        /* ==================== Header Styling ==================== */
        h1 {
            background: var(--primary-gradient) !important;
            color: var(--white) !important;
            padding: 40px 30px !important;
            margin: 0 0 30px 0 !important;
            border-radius: 12px !important;
            box-shadow: 0 8px 16px rgba(237, 28, 36, 0.3) !important;
            font-size: 32px !important;
            font-weight: 700 !important;
            text-align: center !important;
            letter-spacing: 0.5px !important;
        }

        h2 {
            color: var(--amd-red) !important;
            border-bottom: 3px solid var(--amd-red) !important;
            padding-bottom: 12px !important;
            margin: 30px 0 20px 0 !important;
            font-weight: 600 !important;
            font-size: 24px !important;
        }

        h3 {
            color: var(--text-dark) !important;
            font-weight: 600 !important;
            margin: 20px 0 15px 0 !important;
            font-size: 18px !important;
        }

        /* ==================== Summary Section ==================== */
        #environment, .metadata {
            background: var(--white);
            padding: 25px;
            border-radius: 10px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
            margin: 20px 0;
            border-left: 5px solid var(--amd-red);
        }

        /* Environment table styling to match summary tables */
        #environment {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif !important;
        }

        #environment td {
            padding: 12px 15px !important;
            vertical-align: top !important;
            border-bottom: 1px solid var(--border-color) !important;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif !important;
        }

        #environment tr:first-child td {
            font-weight: 600;
            color: var(--text-dark);
            min-width: 200px;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif !important;
        }

        #environment tr:nth-child(odd) {
            background-color: #f8f9fa !important;
        }

        #environment tr:last-child td {
            border-bottom: none !important;
        }

        /* Format nested lists in environment section */
        #environment ul {
            margin: 0 !important;
            padding: 0 0 0 20px !important;
            list-style-type: disc !important;
        }

        #environment ul li {
            margin: 4px 0 !important;
            line-height: 1.6 !important;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif !important;
        }

        .summary {
            display: flex;
            flex-wrap: wrap;
            gap: 20px;
            margin: 25px 0;
        }

        .summary > span {
            background: var(--white);
            padding: 20px 30px;
            border-radius: 10px;
            box-shadow: 0 3px 10px rgba(0, 0, 0, 0.1);
            font-size: 16px;
            font-weight: 600;
            min-width: 180px;
            text-align: center;
            transition: transform 0.2s ease;
        }

        .summary > span:hover {
            transform: translateY(-3px);
            box-shadow: 0 6px 20px rgba(0, 0, 0, 0.15);
        }

        /* ==================== Results Table ==================== */
        #results-table {
            background: var(--white);
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 6px 20px rgba(0, 0, 0, 0.1);
            margin: 30px 0;
            border: none !important;
        }

        #results-table-head {
            background: var(--primary-gradient) !important;
        }

        #results-table th {
            color: var(--white) !important;
            font-weight: 700 !important;
            text-transform: uppercase !important;
            font-size: 13px !important;
            letter-spacing: 1px !important;
            padding: 18px 15px !important;
            border: none !important;
            text-align: left !important;
        }

        #results-table td {
            padding: 15px !important;
            border-bottom: 1px solid var(--border-color) !important;
            font-size: 14px !important;
            vertical-align: middle !important;
        }

        #results-table tbody tr {
            transition: all 0.2s ease;
        }

        #results-table tbody tr:hover {
            background-color: #f8f9fa !important;
            transform: scale(1.01);
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
        }

        #results-table tbody tr:last-child td {
            border-bottom: none;
        }

        /* ==================== Test Status Styling ==================== */
        .passed, tr.passed td {
            background: linear-gradient(90deg, #d4edda 0%, #e8f5e9 100%) !important;
            color: #155724 !important;
            font-weight: 700 !important;
            border-left: 5px solid var(--success-color) !important;
        }

        .failed, tr.failed td {
            background: linear-gradient(90deg, #f8d7da 0%, #ffebee 100%) !important;
            color: #721c24 !important;
            font-weight: 700 !important;
            border-left: 5px solid var(--danger-color) !important;
        }

        .skipped, tr.skipped td {
            background: linear-gradient(90deg, #fff3cd 0%, #fffbf0 100%) !important;
            color: #856404 !important;
            font-weight: 700 !important;
            border-left: 5px solid var(--warning-color) !important;
        }

        .error, tr.error td {
            background: linear-gradient(90deg, #f5c6cb 0%, #ffcccc 100%) !important;
            color: #721c24 !important;
            font-weight: 700 !important;
            border-left: 5px solid var(--danger-color) !important;
        }

        .xfailed, tr.xfailed td,
        .xpassed, tr.xpassed td {
            background: linear-gradient(90deg, #d1ecf1 0%, #e0f7fa 100%) !important;
            color: #0c5460 !important;
            font-weight: 700 !important;
            border-left: 5px solid var(--info-color) !important;
        }

        /* ==================== Result Column Badges ==================== */
        .col-result {
            text-align: center !important;
            font-weight: 800 !important;
            padding: 10px 15px !important;
            border-radius: 6px !important;
            text-transform: uppercase !important;
            font-size: 12px !important;
            letter-spacing: 0.5px !important;
        }

        /* ==================== Other Columns ==================== */
        .col-name {
            font-weight: 600 !important;
            color: var(--text-dark) !important;
        }

        .col-duration {
            font-family: 'Courier New', Consolas, monospace !important;
            color: var(--text-muted) !important;
            font-weight: 500 !important;
        }

        .col-links a {
            color: var(--amd-red) !important;
            text-decoration: none !important;
            font-weight: 600 !important;
            transition: all 0.2s ease !important;
            padding: 4px 8px !important;
            border-radius: 4px !important;
        }

        .col-links a:hover {
            background-color: var(--amd-red) !important;
            color: var(--white) !important;
            text-decoration: none !important;
        }

        /* ==================== Description & Failure Message ==================== */
        .col-description {
            color: var(--text-dark);
            max-width: 500px;
            line-height: 1.8;
            white-space: normal;
            word-wrap: break-word;
        }

        /* First paragraph/line should be bold (summary) */
        .col-description::first-line {
            font-weight: 700;
            color: var(--amd-red);
            font-size: 14px;
        }

        /* Add spacing between paragraphs */
        td.col-description {
            padding: 15px !important;
        }

        /* Make description more readable */
        td.col-description br {
            line-height: 2.5;
        }

        /* ==================== Collapsible Sections ==================== */
        .collapsible {
            background-color: var(--white);
            border-radius: 8px;
            margin: 15px 0;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
            overflow: hidden;
        }

        /* ==================== Log Sections ==================== */
        .log {
            background: #1e1e1e !important;
            color: #d4d4d4 !important;
            padding: 20px !important;
            border-radius: 8px !important;
            font-family: 'Courier New', Consolas, monospace !important;
            font-size: 13px !important;
            line-height: 1.6 !important;
            overflow-x: auto !important;
            margin: 15px 0 !important;
            box-shadow: inset 0 2px 8px rgba(0, 0, 0, 0.2) !important;
        }

        .log::-webkit-scrollbar {
            height: 10px;
        }

        .log::-webkit-scrollbar-track {
            background: #2d2d2d;
            border-radius: 5px;
        }

        .log::-webkit-scrollbar-thumb {
            background: var(--amd-red);
            border-radius: 5px;
        }

        /* ==================== Custom Tables (Cluster Info, Images) ==================== */
        table[style*="border: 1px solid black"] {
            background: var(--white) !important;
            border-radius: 8px !important;
            overflow: hidden !important;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08) !important;
            border: none !important;
            margin: 20px 0 !important;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif !important;
        }

        table[style*="border: 1px solid black"] th {
            background: var(--primary-gradient) !important;
            color: var(--white) !important;
            font-weight: 700 !important;
            text-transform: uppercase !important;
            padding: 15px !important;
            border: none !important;
            letter-spacing: 0.5px !important;
            font-size: 13px !important;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif !important;
        }

        table[style*="border: 1px solid black"] td {
            padding: 12px 15px !important;
            border: none !important;
            border-bottom: 1px solid var(--border-color) !important;
            font-size: 14px !important;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif !important;
            color: var(--text-dark) !important;
            line-height: 1.6 !important;
        }

        /* First column in custom tables (labels/names) should be bold */
        table[style*="border: 1px solid black"] td:first-child {
            font-weight: 600 !important;
            color: var(--text-dark) !important;
        }

        /* Data columns should use monospace for technical values */
        table[style*="border: 1px solid black"] td:not(:first-child) {
            font-family: 'Consolas', 'Monaco', 'Courier New', monospace !important;
            font-size: 13px !important;
            color: #495057 !important;
        }

        table[style*="border: 1px solid black"] tr:last-child td {
            border-bottom: none !important;
        }

        table[style*="border: 1px solid black"] tr:hover {
            background-color: #f8f9fa !important;
        }

        table[style*="border: 1px solid black"] tr:hover td {
            color: var(--text-dark) !important;
        }

        /* ==================== AMD Branding Elements ==================== */
        h1::before {
            content: "🚀 ";
            font-size: 28px;
            margin-right: 10px;
        }

        /* ==================== Responsive Design ==================== */
        @media (max-width: 768px) {
            body {
                padding: 10px;
            }

            h1 {
                font-size: 24px !important;
                padding: 25px 20px !important;
            }

            #results-table {
                font-size: 12px !important;
            }

            #results-table th,
            #results-table td {
                padding: 10px 8px !important;
            }

            .summary {
                flex-direction: column;
            }

            .summary > span {
                width: 100%;
            }
        }

        /* ==================== Print Styles ==================== */
        @media print {
            body {
                background: white;
            }

            #results-table tbody tr:hover {
                transform: none;
                box-shadow: none;
            }

            .col-links {
                display: none;
            }
        }

        /* ==================== Additional Enhancements ==================== */
        .sortable {
            cursor: pointer;
            user-select: none;
        }

        .sortable:hover {
            background-color: rgba(255, 255, 255, 0.1) !important;
        }

        /* Success/Failure Count Styling */
        span.passed {
            color: var(--success-color) !important;
            font-weight: 800 !important;
        }

        span.failed {
            color: var(--danger-color) !important;
            font-weight: 800 !important;
        }

        span.skipped {
            color: var(--warning-color) !important;
            font-weight: 800 !important;
        }

        /* Timestamp styling */
        #environment p {
            margin: 8px 0;
            line-height: 1.8;
        }

        /* AMD Logo-inspired accent */
        h1::after {
            content: "";
            display: block;
            height: 4px;
            background: var(--white);
            margin-top: 15px;
            border-radius: 2px;
            width: 100px;
            margin-left: auto;
            margin-right: auto;
        }
    """

    # For pytest-html 4.x, we need to write CSS to a file and add it via --css option
    # Only do this if HTML reporting is enabled
    if config.getoption('htmlpath'):
        import tempfile
        import os

        # Create a temporary CSS file in the logs directory (so it persists for debugging)
        log_dir = os.path.join(os.getcwd(), 'logs')
        os.makedirs(log_dir, exist_ok=True)

        css_file_path = os.path.join(log_dir, 'pytest_custom.css')

        # Write CSS content to file
        with open(css_file_path, 'w') as css_file:
            css_file.write(css_content)

        # Add CSS file to pytest-html's css option
        # This is how pytest-html 4.x expects custom CSS
        if not hasattr(config.option, 'css'):
            config.option.css = []
        config.option.css.append(css_file_path)
