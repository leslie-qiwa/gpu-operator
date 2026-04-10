# Remediation Config Utils Container

This container packages a Kubernetes ConfigMap that defines remediation strategies for AMD GPU errors.

## Overview

The ConfigMap describes how a node should be remediated when a specific AMD GPU error or problem is detected. The GPU Operator uses this container to deploy the ConfigMap into the cluster at runtime.

## Build Instructions

1. Parse the SAG JSON file to generate the remediation ConfigMap.
2. Copy the generated ConfigMap into this directory.
3. Build the container image - the ConfigMap will be embedded in the image during the build.
