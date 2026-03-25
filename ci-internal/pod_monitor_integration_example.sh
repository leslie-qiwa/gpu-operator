#!/bin/bash
#
# Example: How to integrate pod_monitor.sh into run_sanity.sh
#
# Add this snippet to run_sanity.sh before calling pytest
#

# ============================================================================
# START POD MONITOR INTEGRATION
# ============================================================================

# Configure pod monitor (optional - defaults to AMD GPU namespaces)
export POD_MONITOR_NAMESPACES="${POD_MONITOR_NAMESPACES:-kube-amd-gpu,kube-amd-exporter,default,openshift-amd-gpu}"
export POD_MONITOR_LOG="${POD_MONITOR_LOG:-/tmp/sanity-pod-monitor-$(date +%Y%m%d_%H%M%S).log}"
export POD_MONITOR_FORMAT="${POD_MONITOR_FORMAT:-text}"
export POD_MONITOR_INTERVAL="${POD_MONITOR_INTERVAL:-5}"

# Optional: Filter specific pods
# export POD_MONITOR_PATTERN="device-plugin"

# Start pod monitor in background
echo "Starting pod monitor..."
./ci-internal/pod_monitor.sh start

# Ensure pod monitor stops on script exit (cleanup)
trap './ci-internal/pod_monitor.sh stop' EXIT INT TERM

# ============================================================================
# END POD MONITOR INTEGRATION
# ============================================================================

# Now run your pytest commands as usual
# Example:
# pytest tests/pytests/k8/gpu-operator/ -v
# PYTEST_EXIT_CODE=$?

# Pod monitor will be automatically stopped by the trap
# When stopped, it will:
#   1. Print summary to stdout
#   2. Save detailed JSON dump to ${POD_MONITOR_LOG%.log}.final.json
# exit ${PYTEST_EXIT_CODE}

# Example: Access the final state dump
# FINAL_STATE="${POD_MONITOR_LOG%.log}.final.json"
# if [ -f "$FINAL_STATE" ]; then
#     echo "Analyzing final pod states..."
#     jq '.summary' "$FINAL_STATE"
#     jq '.phase_distribution' "$FINAL_STATE"
# fi
