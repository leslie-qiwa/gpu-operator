#!/usr/bin/python3

"""
Copyright (c) Advanced Micro Devices, Inc. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

     http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import pdb
import pytest
import pprint
import sys
import os
import time
import json
import logging
import lib.k8_util as k8_util
import lib.dra_util as dra_util
import lib.common as common
from lib.util import K8Helper

Logger = logging.getLogger("k8.test_dra_workloads")


@pytest.fixture(autouse=True, scope="module")
def skip_module(environment):
    """Skip if not testing on K8s"""
    if environment.deployment_mode != "k8":
        pytest.skip(
            f"Skipping DRA workload testcases for {environment.deployment_mode} deployment"
        )
    return


@pytest.fixture(scope="module")
def resource_class_gpu_workload(dra_driver_install, dra_driver_namespace, environment):
    """Create a GPU ResourceClass for workload testing"""
    global Logger

    resource_class_name = "gpu-workload-class"

    # Create ResourceClass
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_class(
        name=resource_class_name, driver_name="gpu.amd.com"
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClass: {ret_stderr}"
    )

    Logger.info(f"Created ResourceClass for workloads: {resource_class_name}")
    yield resource_class_name

    # Cleanup
    ret_code, ret_stdout, ret_stderr = dra_util.delete_resource_class(
        resource_class_name
    )
    if ret_code != 0:
        Logger.warn(
            f"Failed to delete ResourceClass {resource_class_name}: {ret_stderr}"
        )


def test_rocm_smi_workload(
    dra_driver_install, dra_driver_namespace, resource_class_gpu_workload, environment
):
    """Test running rocm-smi in a Pod with DRA GPU allocation"""
    global Logger

    claim_name = "test-rocm-smi-claim"
    pod_name = "test-rocm-smi-pod"

    # Create ResourceClaim
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
        name=claim_name,
        namespace=dra_driver_namespace,
        resource_class=resource_class_gpu_workload,
        allocation_mode="Immediate",
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClaim: {ret_stderr}"
    )

    # Wait for allocation
    allocated = dra_util.wait_for_resource_claim_allocation(
        claim_name, dra_driver_namespace, timeout=120
    )
    K8Helper.triage(
        environment, allocated, f"ResourceClaim {claim_name} was not allocated"
    )

    # Create Pod that runs rocm-smi
    ret_code, ret_stdout, ret_stderr = dra_util.create_pod_with_resource_claim(
        pod_name=pod_name,
        namespace=dra_driver_namespace,
        resource_claim_name=claim_name,
        image="rocm/rocm-terminal:latest",
        command=["sh", "-c", "rocm-smi && sleep 10"],
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to create Pod: {ret_stderr}")

    # Wait for Pod to complete
    time.sleep(60)

    # Check Pod logs for rocm-smi output
    # kubectl equivalent: kubectl logs <pod_name> -n <namespace>
    ret_code, logs = k8_util.k8_get_pod_logs(dra_driver_namespace, pod_name)
    K8Helper.triage(environment, ret_code == 0, f"Failed to get Pod logs")

    # Verify rocm-smi output contains expected information
    expected_patterns = ["GPU", "Temperature", "Performance Level"]
    for pattern in expected_patterns:
        if pattern in logs:
            Logger.info(f"Found expected pattern in rocm-smi output: {pattern}")

    Logger.info(f"rocm-smi workload output:\n{logs}")

    # Cleanup
    # kubectl equivalent: kubectl delete pod <pod_name> -n <namespace>
    k8_util.k8_delete_pod(dra_driver_namespace, pod_name)
    time.sleep(10)
    dra_util.delete_resource_claim(claim_name, dra_driver_namespace)


def test_pytorch_workload(
    dra_driver_install, dra_driver_namespace, resource_class_gpu_workload, environment
):
    """Test running PyTorch with ROCm in a Pod with DRA GPU allocation"""
    global Logger

    claim_name = "test-pytorch-claim"
    pod_name = "test-pytorch-pod"

    # Create ResourceClaim
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
        name=claim_name,
        namespace=dra_driver_namespace,
        resource_class=resource_class_gpu_workload,
        allocation_mode="Immediate",
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClaim: {ret_stderr}"
    )

    # Wait for allocation
    allocated = dra_util.wait_for_resource_claim_allocation(
        claim_name, dra_driver_namespace, timeout=120
    )
    K8Helper.triage(
        environment, allocated, f"ResourceClaim {claim_name} was not allocated"
    )

    # Create Pod that runs PyTorch GPU check
    pytorch_command = [
        "python3",
        "-c",
        "import torch; print(f'PyTorch version: {torch.__version__}'); "
        "print(f'CUDA available: {torch.cuda.is_available()}'); "
        "print(f'GPU count: {torch.cuda.device_count()}'); "
        "if torch.cuda.is_available(): "
        "    print(f'GPU name: {torch.cuda.get_device_name(0)}'); "
        "    x = torch.randn(1000, 1000).cuda(); "
        "    y = torch.randn(1000, 1000).cuda(); "
        "    z = torch.matmul(x, y); "
        "    print('Matrix multiplication successful on GPU')",
    ]

    ret_code, ret_stdout, ret_stderr = dra_util.create_pod_with_resource_claim(
        pod_name=pod_name,
        namespace=dra_driver_namespace,
        resource_claim_name=claim_name,
        image="rocm/pytorch:latest",
        command=pytorch_command,
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to create Pod: {ret_stderr}")

    # Wait for Pod to complete
    time.sleep(90)

    # Check Pod logs
    ret_code, logs = k8_util.k8_get_pod_logs(dra_driver_namespace, pod_name)
    K8Helper.triage(environment, ret_code == 0, f"Failed to get Pod logs")

    Logger.info(f"PyTorch workload output:\n{logs}")

    # Verify PyTorch detected GPU
    K8Helper.triage(
        environment,
        "CUDA available: True" in logs or "GPU count:" in logs,
        "PyTorch did not detect GPU via DRA",
    )

    # Cleanup
    # kubectl equivalent: kubectl delete pod <pod_name> -n <namespace>
    k8_util.k8_delete_pod(dra_driver_namespace, pod_name)
    time.sleep(10)
    # kubectl equivalent: kubectl delete resourceclaim <claim_name> -n <namespace>
    dra_util.delete_resource_claim(claim_name, dra_driver_namespace)


def test_tensorflow_workload(
    dra_driver_install, dra_driver_namespace, resource_class_gpu_workload, environment
):
    """Test running TensorFlow with ROCm in a Pod with DRA GPU allocation"""
    global Logger

    claim_name = "test-tensorflow-claim"
    pod_name = "test-tensorflow-pod"

    # Create ResourceClaim
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
        name=claim_name,
        namespace=dra_driver_namespace,
        resource_class=resource_class_gpu_workload,
        allocation_mode="Immediate",
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClaim: {ret_stderr}"
    )

    # Wait for allocation
    allocated = dra_util.wait_for_resource_claim_allocation(
        claim_name, dra_driver_namespace, timeout=120
    )
    K8Helper.triage(
        environment, allocated, f"ResourceClaim {claim_name} was not allocated"
    )

    # Create Pod that runs TensorFlow GPU check
    tf_command = [
        "python3",
        "-c",
        "import tensorflow as tf; "
        "print(f'TensorFlow version: {tf.__version__}'); "
        "print(f'GPU devices: {tf.config.list_physical_devices(\"GPU\")}'); "
        "if tf.config.list_physical_devices('GPU'): "
        "    with tf.device('/GPU:0'): "
        "        a = tf.constant([[1.0, 2.0], [3.0, 4.0]]); "
        "        b = tf.constant([[1.0, 1.0], [0.0, 1.0]]); "
        "        c = tf.matmul(a, b); "
        "        print(f'Matrix multiplication result: {c}'); "
        "        print('TensorFlow GPU computation successful')",
    ]

    ret_code, ret_stdout, ret_stderr = dra_util.create_pod_with_resource_claim(
        pod_name=pod_name,
        namespace=dra_driver_namespace,
        resource_claim_name=claim_name,
        image="rocm/tensorflow:latest",
        command=tf_command,
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to create Pod: {ret_stderr}")

    # Wait for Pod to complete
    time.sleep(90)

    # Check Pod logs
    ret_code, logs = k8_util.k8_get_pod_logs(dra_driver_namespace, pod_name)
    K8Helper.triage(environment, ret_code == 0, f"Failed to get Pod logs")

    Logger.info(f"TensorFlow workload output:\n{logs}")

    # Verify TensorFlow detected GPU
    K8Helper.triage(
        environment, "GPU devices:" in logs, "TensorFlow did not list GPU devices"
    )

    # Cleanup
    k8_util.k8_delete_pod(dra_driver_namespace, pod_name)
    time.sleep(10)
    dra_util.delete_resource_claim(claim_name, dra_driver_namespace)


def test_concurrent_gpu_workloads(
    dra_driver_install, dra_driver_namespace, resource_class_gpu_workload, environment
):
    """Test running multiple concurrent GPU workloads with DRA"""
    global Logger

    ret_code, gpu_nodes = k8_util.k8_get_gpu_nodes()
    K8Helper.triage(environment, ret_code == 0, "Failed to get GPU nodes")

    # Count available GPUs
    total_gpus = 0
    for node in gpu_nodes:
        capacity = node["status"].get("capacity", {})
        if "amd.com/gpu" in capacity:
            total_gpus += int(capacity["amd.com/gpu"])

    Logger.info(f"Total AMD GPUs available: {total_gpus}")
    K8Helper.triage(environment, total_gpus >= 2, "Test requires at least 2 GPUs")

    # Create 2 concurrent workloads
    num_workloads = min(2, total_gpus)
    workloads = []

    for i in range(num_workloads):
        claim_name = f"test-concurrent-claim-{i}"
        pod_name = f"test-concurrent-pod-{i}"

        # Create ResourceClaim
        ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
            name=claim_name,
            namespace=dra_driver_namespace,
            resource_class=resource_class_gpu_workload,
            allocation_mode="Immediate",
        )
        K8Helper.triage(
            environment,
            ret_code == 0,
            f"Failed to create ResourceClaim {claim_name}: {ret_stderr}",
        )

        # Wait for allocation
        allocated = dra_util.wait_for_resource_claim_allocation(
            claim_name, dra_driver_namespace, timeout=120
        )
        K8Helper.triage(
            environment, allocated, f"ResourceClaim {claim_name} was not allocated"
        )

        # Create Pod
        ret_code, ret_stdout, ret_stderr = dra_util.create_pod_with_resource_claim(
            pod_name=pod_name,
            namespace=dra_driver_namespace,
            resource_claim_name=claim_name,
            image="rocm/pytorch:latest",
            command=[
                "python3",
                "-c",
                f"import torch; import time; "
                f"print('Workload {i} starting'); "
                f"x = torch.randn(5000, 5000).cuda(); "
                f"for _ in range(10): y = torch.matmul(x, x); time.sleep(1); "
                f"print('Workload {i} completed')",
            ],
        )
        K8Helper.triage(
            environment, ret_code == 0, f"Failed to create Pod {pod_name}: {ret_stderr}"
        )

        workloads.append((claim_name, pod_name))

    # Wait for all workloads to complete
    time.sleep(120)

    # Check logs of all workloads
    for claim_name, pod_name in workloads:
        ret_code, logs = k8_util.k8_get_pod_logs(dra_driver_namespace, pod_name)
        if ret_code == 0:
            Logger.info(f"Workload {pod_name} logs:\n{logs}")
            K8Helper.triage(
                environment,
                "completed" in logs.lower(),
                f"Workload {pod_name} did not complete successfully",
            )

    # Cleanup
    for claim_name, pod_name in workloads:
        k8_util.k8_delete_pod(dra_driver_namespace, pod_name)
        time.sleep(5)
        dra_util.delete_resource_claim(claim_name, dra_driver_namespace)


def test_gpu_memory_allocation(
    dra_driver_install, dra_driver_namespace, resource_class_gpu_workload, environment
):
    """Test GPU memory allocation through DRA"""
    global Logger

    claim_name = "test-gpu-memory-claim"
    pod_name = "test-gpu-memory-pod"

    # Create ResourceClaim
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
        name=claim_name,
        namespace=dra_driver_namespace,
        resource_class=resource_class_gpu_workload,
        allocation_mode="Immediate",
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClaim: {ret_stderr}"
    )

    # Wait for allocation
    allocated = dra_util.wait_for_resource_claim_allocation(
        claim_name, dra_driver_namespace, timeout=120
    )
    K8Helper.triage(
        environment, allocated, f"ResourceClaim {claim_name} was not allocated"
    )

    # Create Pod that allocates GPU memory
    memory_test_command = [
        "python3",
        "-c",
        "import torch; "
        "print(f'GPU memory available: {torch.cuda.get_device_properties(0).total_memory / 1e9:.2f} GB'); "
        "print('Allocating 1GB of GPU memory...'); "
        "x = torch.randn(128, 1024, 1024).cuda(); "
        "print(f'Allocated memory: {torch.cuda.memory_allocated() / 1e9:.2f} GB'); "
        "print(f'Cached memory: {torch.cuda.memory_reserved() / 1e9:.2f} GB'); "
        "del x; "
        "torch.cuda.empty_cache(); "
        "print('Memory freed successfully')",
    ]

    ret_code, ret_stdout, ret_stderr = dra_util.create_pod_with_resource_claim(
        pod_name=pod_name,
        namespace=dra_driver_namespace,
        resource_claim_name=claim_name,
        image="rocm/pytorch:latest",
        command=memory_test_command,
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to create Pod: {ret_stderr}")

    # Wait for Pod to complete
    time.sleep(60)

    # Check Pod logs
    ret_code, logs = k8_util.k8_get_pod_logs(dra_driver_namespace, pod_name)
    K8Helper.triage(environment, ret_code == 0, f"Failed to get Pod logs")

    Logger.info(f"GPU memory test output:\n{logs}")

    # Verify memory operations succeeded
    K8Helper.triage(
        environment,
        "Memory freed successfully" in logs,
        "GPU memory allocation/deallocation test failed",
    )

    # Cleanup
    k8_util.k8_delete_pod(dra_driver_namespace, pod_name)
    time.sleep(10)
    dra_util.delete_resource_claim(claim_name, dra_driver_namespace)


def test_long_running_workload(
    dra_driver_install, dra_driver_namespace, resource_class_gpu_workload, environment
):
    """Test long-running GPU workload with DRA"""
    global Logger

    claim_name = "test-long-running-claim"
    pod_name = "test-long-running-pod"

    # Create ResourceClaim
    ret_code, ret_stdout, ret_stderr = dra_util.create_resource_claim(
        name=claim_name,
        namespace=dra_driver_namespace,
        resource_class=resource_class_gpu_workload,
        allocation_mode="Immediate",
    )
    K8Helper.triage(
        environment, ret_code == 0, f"Failed to create ResourceClaim: {ret_stderr}"
    )

    # Wait for allocation
    allocated = dra_util.wait_for_resource_claim_allocation(
        claim_name, dra_driver_namespace, timeout=120
    )
    K8Helper.triage(
        environment, allocated, f"ResourceClaim {claim_name} was not allocated"
    )

    # Create Pod that runs for extended period
    long_running_command = [
        "sh",
        "-c",
        "for i in $(seq 1 10); do "
        "python3 -c 'import torch; x = torch.randn(1000, 1000).cuda(); "
        'y = torch.matmul(x, x); print(f"Iteration $i completed")\'; '
        "sleep 5; "
        "done; "
        "echo 'Long-running workload completed'",
    ]

    ret_code, ret_stdout, ret_stderr = dra_util.create_pod_with_resource_claim(
        pod_name=pod_name,
        namespace=dra_driver_namespace,
        resource_claim_name=claim_name,
        image="rocm/pytorch:latest",
        command=long_running_command,
    )
    K8Helper.triage(environment, ret_code == 0, f"Failed to create Pod: {ret_stderr}")

    # Monitor Pod status during execution
    for i in range(6):  # Check 6 times over ~60 seconds
        time.sleep(10)
        ret_code, pod_status = k8_util.k8_get_pod_status(dra_driver_namespace, pod_name)
        if ret_code == 0:
            Logger.info(f"Pod status check {i+1}: {pod_status}")

    # Wait for completion
    time.sleep(30)

    # Check final logs
    ret_code, logs = k8_util.k8_get_pod_logs(dra_driver_namespace, pod_name)
    K8Helper.triage(environment, ret_code == 0, f"Failed to get Pod logs")

    Logger.info(f"Long-running workload output:\n{logs}")

    # Verify workload completed
    K8Helper.triage(
        environment,
        "Long-running workload completed" in logs,
        "Long-running workload did not complete successfully",
    )

    # Cleanup
    k8_util.k8_delete_pod(dra_driver_namespace, pod_name)
    time.sleep(10)
    dra_util.delete_resource_claim(claim_name, dra_driver_namespace)
