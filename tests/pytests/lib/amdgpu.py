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

import os
import sys
import pdb
import logging
import json
import lib.common
from datetime import datetime
from collections import defaultdict

Logger = logging.getLogger("lib.amdgpu")

def get_matching_driver_version(rocm_version):
    # Use absolute path relative to this module
    module_dir = os.path.dirname(os.path.abspath(__file__))
    json_file = os.path.join(module_dir, "files", "gpu-operator-rocm-info.json")

    with open(json_file, "r") as fp:
        rocm_info = json.load(fp)

    for entry in rocm_info['rocm-driver-matrix']:
        if entry['rocm-version'] == rocm_version:
            return entry['amdgpu-driver-version']
    return None

def get_amdgpu_device_series(device_id) -> str:
    """
    Lookup device-id in the amdgpu-features.json to retrieve GPU Series Name
    """
    # Use absolute path relative to this module
    module_dir = os.path.dirname(os.path.abspath(__file__))
    json_file = os.path.join(module_dir, "files", "amdgpu-features.json")

    with open(json_file, "r") as fp:
        amdgpu_feature_data = json.load(fp)

    dev_id_str = str(device_id).strip()
    if not dev_id_str.lower().startswith("0x"):
        dev_id_str = f"0x{dev_id_str}"

    for entry in amdgpu_feature_data['amd-gpu-devs']:
        if dev_id_str in entry.get("device-id", []):
            return entry.get("series", "UNKNOWN")
    return "UNKNOWN"
