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

"""
Pytest configuration for AMD Exporter Docker Container test suite.

This module configures pytest for the standalone Docker container validation tests.
It customizes the HTML report title to clearly identify test results for the
AMD Device Metrics Exporter Docker container deployment and validation.
"""

import pdb
import pytest
import os
import logging
from lib import common
from lib import k8_util
from lib import helm_util

Logger = logging.getLogger("standalone.docker.conftest")

def pytest_html_report_title(report):
    """
    Customize the HTML test report title.

    This hook is called by pytest-html to set the title of the generated
    HTML test report. It identifies the report as AMD Exporter Docker
    Container validation results.

    Args:
        report: The pytest-html report object to customize
    """
    report.title = f"AMD Exporter Docker Container Validation Test Results"
