# Pod Monitor - Kubernetes Pod Monitoring Utility

Real-time monitoring utility for Kubernetes pods using kr8s. Designed to run in the background during pytest execution to track pod state changes (phase, readiness, restarts).

## Installation

Install dependencies from ci-internal requirements:

```bash
pip install -r ci-internal/requirements.txt
```

## Usage

### Control Script (Recommended)

Use `pod_monitor.sh` to start/stop the monitor:

```bash
# Start monitoring
./ci-internal/pod_monitor.sh start

# Check status
./ci-internal/pod_monitor.sh status

# Stop monitoring
./ci-internal/pod_monitor.sh stop

# Restart
./ci-internal/pod_monitor.sh restart
```

### With Environment Variables

```bash
# Monitor specific namespaces
POD_MONITOR_NAMESPACES="gpu-operator,node-problem-detector" \
./ci-internal/pod_monitor.sh start

# Filter by pod pattern
POD_MONITOR_PATTERN="device-plugin" \
./ci-internal/pod_monitor.sh start

# JSON output with custom log
POD_MONITOR_FORMAT="json" \
POD_MONITOR_LOG="/tmp/my-test-pods.log" \
./ci-internal/pod_monitor.sh start

# All options
POD_MONITOR_NAMESPACES="gpu-operator" \
POD_MONITOR_PATTERN="npd" \
POD_MONITOR_FORMAT="json" \
POD_MONITOR_LOG="/tmp/npd-monitor.log" \
POD_MONITOR_INTERVAL="10" \
./ci-internal/pod_monitor.sh start
```

### Integration with Test Scripts

Example integration in `run_sanity.sh`:

```bash
#!/bin/bash

# Start pod monitor before tests
export POD_MONITOR_NAMESPACES="gpu-operator,kube-system"
export POD_MONITOR_LOG="/tmp/sanity-pod-monitor.log"
./ci-internal/pod_monitor.sh start

# Run pytest
pytest tests/pytests/k8/gpu-operator/
PYTEST_EXIT_CODE=$?

# Stop pod monitor after tests
./ci-internal/pod_monitor.sh stop

# Preserve pytest exit code
exit ${PYTEST_EXIT_CODE}
```

### Standalone Python Script

Run the pod monitor directly (without the control script):

```bash
# Monitor all pods in a namespace
./ci-internal/pod_monitor.py --namespace gpu-operator

# Monitor multiple namespaces
./ci-internal/pod_monitor.py --namespaces gpu-operator,node-problem-detector

# Filter by pod name pattern
./ci-internal/pod_monitor.py --namespace gpu-operator --pod-pattern "device-plugin"

# JSON output for parsing
./ci-internal/pod_monitor.py --namespace gpu-operator --output-format json

# Custom log file and poll interval
./ci-internal/pod_monitor.py --namespace gpu-operator \
    --log-file /tmp/my-monitor.log \
    --poll-interval 10
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `POD_MONITOR_NAMESPACES` | Comma-separated namespace list | `kube-amd-gpu,kube-amd-gpu-dra,kube-amd-exporter,default,openshift-amd-gpu` |
| `POD_MONITOR_PATTERN` | Pod name filter (substring match) | _(all pods)_ |
| `POD_MONITOR_FORMAT` | Output format: `text` or `json` | `text` |
| `POD_MONITOR_LOG` | Log file path | `/tmp/pod-monitor-<timestamp>.log` |

## Output Formats

### Text Format (default)

Human-readable output:

```
2026-03-24 18:30:15 [INFO] Starting pod monitor for namespaces: kube-amd-gpu,kube-amd-gpu-dra,kube-amd-exporter,default,openshift-amd-gpu
2026-03-24 18:30:15 [INFO] New Pod detected: 'test-deviceconfig-device-plugin-8f7px' in gpu-operator
2026-03-24 18:30:20 [INFO] Pod 'test-deviceconfig-device-plugin-8f7px' (gpu-operator): Pending → Running | Ready: 1/1 | Restarts: 0
2026-03-24 18:30:25 [INFO] Pod 'test-deviceconfig-metrics-exporter-27gq9' (gpu-operator): ContainerCreating → Running | Ready: 2/2 | Restarts: 0
```

### JSON Format

Machine-parseable output:

```json
{"timestamp": "2026-03-24T18:30:15.123456", "event_type": "pod_created", "name": "test-deviceconfig-device-plugin-8f7px", "namespace": "gpu-operator", "phase": "Pending", "ready": 0, "total": 1, "restarts": 0}
{"timestamp": "2026-03-24T18:30:20.234567", "event_type": "pod_state_change", "name": "test-deviceconfig-device-plugin-8f7px", "namespace": "gpu-operator", "old_phase": "Pending", "phase": "Running", "ready": 1, "total": 1, "restarts": 0}
```

## State Change Types

The monitor tracks the following pod state changes:

- **pod_created**: New pod detected in monitored namespace
- **pod_state_change**: Pod phase, readiness, or restart count changed
- **pod_deleted**: Pod removed from cluster

## Monitored Metrics

For each pod:

- **Phase**: Current pod phase (Pending, Running, Succeeded, Failed, Unknown)
- **Ready**: Number of ready containers vs total containers
- **Restarts**: Total container restart count
- **Node**: Node IP where pod is running
- **Container States**: Individual container states (running, waiting, terminated)
- **Conditions**: Pod conditions (Ready, ContainersReady, PodScheduled, etc.)

## Automatic Failure Diagnostics

When any pod transitions to `Failed` state, the monitor automatically collects comprehensive diagnostics using kubectl:

**Collected Information:**
- `kubectl describe pod` output
- Container logs (last 100 lines)
- Previous container logs (if container restarted)
- Kubernetes events related to the pod
- Final pod state (phase, readiness, container states)

**Failure Report Location:**
- Saved to: `<logfile>_failures/<namespace>_<pod-name>_<timestamp>.json`
- Example: `/gpu-operator/tests/pytests/logs/pdo-monitor_failures/openshift-amd-gpu_devcfg-clusterwide-gpu-build-gg6rc-build_20260324_181106.json`

**Failure Report Format:**
```json
{
  "pod": "devcfg-clusterwide-gpu-build-gg6rc-build",
  "namespace": "openshift-amd-gpu",
  "timestamp": "2026-03-24T18:13:08.123456",
  "final_state": {
    "phase": "Failed",
    "ready": 0,
    "total": 1,
    "container_states": ["terminated:Error"]
  },
  "kubectl_describe": "Name: devcfg-clusterwide-gpu-build...",
  "container_logs": {
    "container-0": "... last 100 lines of logs ..."
  },
  "previous_logs": {
    "container-0": "... previous run logs if restarted ..."
  },
  "events": {
    "items": [
      {
        "type": "Warning",
        "reason": "Failed",
        "message": "Error: ImagePullBackOff"
      }
    ]
  }
}
```

This allows you to investigate failures even after the pod has been deleted.

## Final Reports

When the monitor stops, it automatically generates three types of reports:

### 1. Failure Diagnostic Reports (auto-collected)

Individual JSON files for each pod that transitions to Failed state.

### 2. Summary Report (stdout + file)

Printed to stdout and logged to the main log file:

```
======================================================================
POD MONITOR SUMMARY
======================================================================
Duration:          0:15:23.456789
Namespaces:        kube-amd-gpu,kube-amd-gpu-dra,kube-amd-exporter,default,openshift-amd-gpu
Total pods:        15
State changes:     42

Pod Status:
  Running        : 12
  Pending        : 1
  Failed         : 2

Pods with Restarts (3):
  - kube-amd-gpu/device-plugin-xyz: 5 restarts
  - kube-amd-gpu/metrics-exporter-abc: 2 restarts
  - default/test-workload-123: 1 restarts

Failed/Problem Pods (2):
  - kube-amd-gpu/driver-daemon-456
    Phase: Failed, Ready: 0/1
    Containers: terminated:Error
  - default/test-pod-789
    Phase: Running, Ready: 1/2
    Containers: running, waiting:CrashLoopBackOff
======================================================================
```

### 3. Detailed State Dump (JSON file)

Saved to `<logfile>.final.json` (e.g., `/tmp/pod-monitor-20260324_183015.final.json`)

```json
{
  "summary": {
    "start_time": "2026-03-24T18:30:15.123456",
    "end_time": "2026-03-24T18:45:38.654321",
    "duration_seconds": 923.53,
    "namespaces": ["kube-amd-gpu", "kube-amd-gpu-dra", "kube-amd-exporter", "default", "openshift-amd-gpu"],
    "pod_pattern": null,
    "total_pods": 15,
    "total_events": 42
  },
  "final_pod_states": {
    "kube-amd-gpu/device-plugin-xyz": {
      "name": "device-plugin-xyz",
      "namespace": "kube-amd-gpu",
      "phase": "Running",
      "ready": 1,
      "total": 1,
      "restarts": 5,
      "node": "10.0.1.5",
      "container_states": ["running"],
      "state_changes": [
        {
          "timestamp": "2026-03-24T18:30:20.123456",
          "event": "state_change",
          "old_phase": "Pending",
          "new_phase": "Running",
          "ready": "1/1",
          "restarts": 0
        },
        {
          "timestamp": "2026-03-24T18:35:10.789012",
          "event": "state_change",
          "old_phase": "Running",
          "new_phase": "Running",
          "ready": "0/1",
          "restarts": 1
        }
      ]
    }
  },
  "phase_distribution": {
    "Running": 12,
    "Pending": 1,
    "Failed": 2
  }
}
```

## Background Operation

The control script (`pod_monitor.sh`) handles:

1. Starting pod monitor in background with nohup
2. Tracking process via PID file (`/tmp/pod_monitor.pid`)
3. Graceful shutdown with SIGTERM (fallback to SIGKILL)
4. Status checking and stale PID cleanup
5. Automatic kr8s installation if missing

## Integration Examples

### GitLab CI

```yaml
test:
  script:
    - pip install -r ci-internal/requirements.txt
    - POD_MONITOR_LOG="$CI_PROJECT_DIR/pod-monitor.log"
      ./ci-internal/run_with_pod_monitor.sh pytest tests/pytests/k8/
  artifacts:
    when: always
    paths:
      - pod-monitor.log
```

### Manual Background Execution

```bash
# Using control script (recommended)
./ci-internal/pod_monitor.sh start
pytest tests/pytests/k8/gpu-operator/
./ci-internal/pod_monitor.sh stop

# Or directly with Python script
./ci-internal/pod_monitor.py --namespace gpu-operator --log-file /tmp/pods.log &
MONITOR_PID=$!
pytest tests/pytests/k8/gpu-operator/
kill $MONITOR_PID
```

## Troubleshooting

### kr8s Not Found

```bash
pip install kr8s
# or
pip install -r ci-internal/requirements.txt
```

### Permission Issues

Ensure kubeconfig is accessible:

```bash
export KUBECONFIG=~/.kube/config
# or
export KUBECONFIG=/path/to/kubeconfig
```

### No Pods Detected

Check namespace exists and contains pods:

```bash
kubectl get pods -n gpu-operator
```

Verify kubeconfig context:

```bash
kubectl config current-context
```

## Advanced Usage

### Parse JSON Logs

```bash
# Filter for pod state changes from main log
grep '"event_type": "pod_state_change"' /tmp/pod-monitor.log | jq .

# Count restarts
grep '"event_type": "pod_state_change"' /tmp/pod-monitor.log | \
    jq -r 'select(.restarts > 0) | "\(.name): \(.restarts) restarts"'

# Track specific pod
grep '"name": "device-plugin"' /tmp/pod-monitor.log | jq .

# Parse final state dump
jq '.summary' /tmp/pod-monitor-20260324_183015.final.json
jq '.phase_distribution' /tmp/pod-monitor-20260324_183015.final.json

# Find pods with restarts in final dump
jq '.final_pod_states | to_entries[] | select(.value.restarts > 0) | {pod: .key, restarts: .value.restarts}' \
    /tmp/pod-monitor-20260324_183015.final.json

# Get full history of a specific pod
jq '.final_pod_states["kube-amd-gpu/device-plugin-xyz"].state_changes' \
    /tmp/pod-monitor-20260324_183015.final.json
```

### Monitor Until Condition

```bash
# Run until specific pod reaches Running state
./ci-internal/pod_monitor.py --namespace gpu-operator --pod-pattern "my-pod" &
MONITOR_PID=$!

# Wait for condition
while ! kubectl get pod -n gpu-operator | grep "my-pod.*Running"; do
    sleep 5
done

kill $MONITOR_PID
```

## Current Limitations

- **Polling-based**: Uses 5s polling interval rather than Kubernetes watch API
- **Pod state only**: Tracks phase, readiness, and restart counts only
- **No resource metrics**: Does not monitor CPU/memory usage
- **No Kubernetes events**: Does not track Kubernetes Event objects
- **No log analysis**: Does not parse container logs

## Future Enhancements

- [ ] Use kr8s watch API for real-time updates instead of polling
- [ ] Add Kubernetes Event object monitoring
- [ ] Add resource usage monitoring (CPU, memory via metrics API)
- [ ] Track container logs for errors
- [ ] Alert on specific conditions (crash loops, OOMKilled, etc.)
- [ ] Metrics export to Prometheus
- [ ] Web UI for real-time visualization
