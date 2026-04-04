#!/bin/bash
#
# pod_monitor.sh - Control script for pod monitoring
#
# Usage:
#   ./pod_monitor.sh start   - Start pod monitor in background
#   ./pod_monitor.sh stop    - Stop pod monitor
#   ./pod_monitor.sh status  - Check if monitor is running
#   ./pod_monitor.sh restart - Restart pod monitor
#
# Environment variables:
#   POD_MONITOR_NAMESPACES - Comma-separated namespaces (default: kube-amd-gpu,kube-amd-gpu-dra,kube-amd-exporter,default,openshift-amd-gpu)
#   POD_MONITOR_PATTERN    - Pod name pattern to filter
#   POD_MONITOR_FORMAT     - Output format: text or json (default: text)
#   POD_MONITOR_LOG        - Log file path (default: /tmp/pod-monitor-<timestamp>.log)
#   POD_MONITOR_INTERVAL   - Poll interval in seconds (default: 5)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POD_MONITOR_PY="${SCRIPT_DIR}/pod_monitor.py"
PID_FILE="/tmp/pod_monitor.pid"
LOG_FILE_TRACKER="/tmp/pod_monitor.logfile"

# Default configuration
NAMESPACES="${POD_MONITOR_NAMESPACES:-kube-amd-gpu,kube-amd-gpu-dra,kube-amd-exporter,default,openshift-amd-gpu}"
PATTERN="${POD_MONITOR_PATTERN:-}"
FORMAT="${POD_MONITOR_FORMAT:-text}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="${POD_MONITOR_LOG:-/tmp/pod-monitor-${TIMESTAMP}.log}"
INTERVAL="${POD_MONITOR_INTERVAL:-5}"

start_monitor() {
    # Check if already running
    if [ -f "${PID_FILE}" ]; then
        local pid=$(cat "${PID_FILE}")
        if kill -0 "${pid}" 2>/dev/null; then
            echo "Pod monitor is already running (PID: ${pid})"
            return 0
        else
            # Stale PID file
            rm -f "${PID_FILE}"
        fi
    fi

    # Check if kr8s is installed
    if ! python3 -c "import kr8s" 2>/dev/null; then
        echo "INFO: kr8s not installed. Installing from ci-internal/requirements.txt..."
        python3 -m pip install --disable-pip-version-check -q -r "${SCRIPT_DIR}/requirements.txt"
    fi

    # Build pod monitor command as array to prevent shell injection
    local cmd_args=(
        "${POD_MONITOR_PY}"
        "--namespaces" "${NAMESPACES}"
        "--output-format" "${FORMAT}"
        "--log-file" "${LOG_FILE}"
        "--poll-interval" "${INTERVAL}"
    )
    if [ -n "${PATTERN}" ]; then
        cmd_args+=("--pod-pattern" "${PATTERN}")
    fi

    echo "========================================="
    echo "Starting Pod Monitor"
    echo "========================================="
    echo "Namespaces:    ${NAMESPACES}"
    echo "Pattern:       ${PATTERN:-<all pods>}"
    echo "Format:        ${FORMAT}"
    echo "Log file:      ${LOG_FILE}"
    echo "Poll interval: ${INTERVAL}s"
    echo "========================================="

    # Start pod monitor in background with proper quoting
    nohup python3 "${cmd_args[@]}" > /dev/null 2>&1 &
    local pid=$!
    echo "${pid}" > "${PID_FILE}"

    # Wait a moment to check if it started successfully
    sleep 2
    if kill -0 "${pid}" 2>/dev/null; then
        # Save log file path for stop command
        echo "${LOG_FILE}" > "${LOG_FILE_TRACKER}"
        echo "Pod monitor started successfully (PID: ${pid})"
        echo "Log file: ${LOG_FILE}"
        echo "PID file: ${PID_FILE}"
        return 0
    else
        echo "ERROR: Pod monitor failed to start"
        rm -f "${PID_FILE}"
        return 1
    fi
}

stop_monitor() {
    if [ ! -f "${PID_FILE}" ]; then
        echo "Pod monitor is not running (no PID file found)"
        return 0
    fi

    local pid=$(cat "${PID_FILE}")

    if ! kill -0 "${pid}" 2>/dev/null; then
        echo "Pod monitor is not running (stale PID file)"
        rm -f "${PID_FILE}"
        return 0
    fi

    echo "Stopping pod monitor (PID: ${pid})..."
    kill "${pid}" 2>/dev/null || true

    # Wait for process to stop (max 5 seconds)
    local count=0
    while kill -0 "${pid}" 2>/dev/null && [ ${count} -lt 10 ]; do
        sleep 0.5
        count=$((count + 1))
    done

    if kill -0 "${pid}" 2>/dev/null; then
        echo "Pod monitor did not stop gracefully, forcing..."
        kill -9 "${pid}" 2>/dev/null || true
    fi

    rm -f "${PID_FILE}"
    echo "Pod monitor stopped"
    echo ""

    # Display summary if available
    local summary_file=""
    if [ -f "${LOG_FILE_TRACKER}" ]; then
        local log_file=$(cat "${LOG_FILE_TRACKER}")
        local log_dir=$(dirname "${log_file}")
        local log_base=$(basename "${log_file}" .log)
        summary_file="${log_dir}/${log_base}.summary.txt"
        rm -f "${LOG_FILE_TRACKER}"
    fi

    if [ -f "${summary_file}" ]; then
        cat "${summary_file}"
        echo ""
    else
        echo "No summary file found"
    fi
}

status_monitor() {
    if [ ! -f "${PID_FILE}" ]; then
        echo "Pod monitor is not running"
        return 1
    fi

    local pid=$(cat "${PID_FILE}")

    if kill -0 "${pid}" 2>/dev/null; then
        echo "Pod monitor is running (PID: ${pid})"

        # Try to find log file
        if [ -n "${POD_MONITOR_LOG}" ] && [ -f "${POD_MONITOR_LOG}" ]; then
            echo "Log file: ${POD_MONITOR_LOG}"
        else
            # Search for recent log files
            local recent_log=$(ls -t /tmp/pod-monitor-*.log 2>/dev/null | head -1)
            if [ -n "${recent_log}" ]; then
                echo "Log file: ${recent_log}"
            fi
        fi
        return 0
    else
        echo "Pod monitor is not running (stale PID file)"
        rm -f "${PID_FILE}"
        return 1
    fi
}

restart_monitor() {
    echo "Restarting pod monitor..."
    stop_monitor
    sleep 1
    start_monitor
}

case "${1:-}" in
    start)
        start_monitor
        ;;
    stop)
        stop_monitor
        ;;
    status)
        status_monitor
        ;;
    restart)
        restart_monitor
        ;;
    *)
        echo "Usage: $0 {start|stop|status|restart}"
        echo ""
        echo "Environment variables:"
        echo "  POD_MONITOR_NAMESPACES - Comma-separated namespaces (default: kube-amd-gpu,kube-amd-gpu-dra,kube-amd-exporter,default,openshift-amd-gpu)"
        echo "  POD_MONITOR_PATTERN    - Pod name pattern to filter"
        echo "  POD_MONITOR_FORMAT     - Output format: text or json (default: text)"
        echo "  POD_MONITOR_LOG        - Log file path (default: /tmp/pod-monitor-<timestamp>.log)"
        echo "  POD_MONITOR_INTERVAL   - Poll interval in seconds (default: 5)"
        echo ""
        echo "Examples:"
        echo "  $0 start"
        echo "  POD_MONITOR_NAMESPACES='gpu-operator,kube-system' $0 start"
        echo "  POD_MONITOR_PATTERN='device-plugin' $0 start"
        echo "  $0 status"
        echo "  $0 stop"
        exit 1
        ;;
esac
