#!/usr/bin/env python3
"""
Pod Monitor - Real-time Kubernetes Pod Monitoring Utility

This utility monitors Kubernetes pods in specified namespaces using kr8s.
It runs in the background and logs pod state changes, events, and resource usage.

Usage:
    # Monitor all pods in a namespace
    python pod_monitor.py --namespace gpu-operator

    # Monitor specific pod patterns
    python pod_monitor.py --namespace gpu-operator --pod-pattern "device-plugin"

    # Monitor multiple namespaces
    python pod_monitor.py --namespaces gpu-operator,node-problem-detector

    # Enable JSON output for parsing
    python pod_monitor.py --namespace gpu-operator --output-format json

    # Run with custom log file
    python pod_monitor.py --namespace gpu-operator --log-file /tmp/pod-monitor.log
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
import subprocess
from datetime import datetime
from typing import Dict, List, Optional, Set
from pathlib import Path

try:
    import kr8s
    from kr8s.asyncio.objects import Pod
except ImportError:
    print("ERROR: kr8s library not found. Install with: pip install kr8s", file=sys.stderr)
    sys.exit(1)


class PodMonitor:
    """
    Monitors Kubernetes pods for state changes and events.
    """

    def __init__(
        self,
        namespaces: List[str],
        pod_pattern: Optional[str] = None,
        output_format: str = "text",
        log_file: Optional[str] = None,
        poll_interval: int = 5,
    ):
        """
        Initialize the pod monitor.

        Args:
            namespaces: List of namespaces to monitor
            pod_pattern: Optional pattern to filter pod names (substring match)
            output_format: Output format - "text" or "json"
            log_file: Optional log file path
            poll_interval: Seconds between poll cycles
        """
        self.namespaces = namespaces
        self.pod_pattern = pod_pattern
        self.output_format = output_format
        self.poll_interval = poll_interval
        self.running = False
        self.log_file = log_file
        self.pod_states: Dict[str, Dict] = {}  # pod_name -> {phase, ready, restarts, ...}
        self.state_history: Dict[str, List[Dict]] = {}  # pod_name -> [state changes]
        self.event_count = 0
        self.start_time = datetime.now()
        self.failure_reports_dir = None

        # Setup failure reports directory
        if log_file:
            log_path = Path(log_file)
            self.failure_reports_dir = log_path.parent / f"{log_path.stem}_failures"
            self.failure_reports_dir.mkdir(exist_ok=True)

        # Setup logging
        self.logger = self._setup_logger(log_file)

    def _setup_logger(self, log_file: Optional[str]) -> logging.Logger:
        """Setup logger with file handler only (no console output)."""
        logger = logging.getLogger("PodMonitor")
        logger.setLevel(logging.INFO)

        # Formatter
        if self.output_format == "json":
            formatter = logging.Formatter('%(message)s')
        else:
            formatter = logging.Formatter(
                '%(asctime)s [%(levelname)s] %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )

        # File handler only - no console output to avoid cluttering test output
        if log_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        else:
            # If no log file specified, add a null handler to suppress warnings
            logger.addHandler(logging.NullHandler())

        return logger

    def _format_output(self, event_type: str, data: Dict) -> str:
        """Format output based on configured format."""
        if self.output_format == "json":
            output = {
                "timestamp": datetime.now().isoformat(),
                "event_type": event_type,
                **data
            }
            return json.dumps(output)
        else:
            # Text format
            return self._format_text(event_type, data)

    def _format_text(self, event_type: str, data: Dict) -> str:
        """Format as human-readable text."""
        if event_type == "pod_state_change":
            return (
                f"Pod '{data['name']}' ({data['namespace']}): "
                f"{data.get('old_phase', 'Unknown')} → {data['phase']} | "
                f"Ready: {data['ready']}/{data['total']} | "
                f"Restarts: {data['restarts']}"
            )
        elif event_type == "pod_event":
            return (
                f"Event for '{data['pod_name']}' ({data['namespace']}): "
                f"[{data['event_type']}] {data['reason']}: {data['message']}"
            )
        elif event_type == "pod_created":
            return f"New Pod detected: '{data['name']}' in {data['namespace']}"
        elif event_type == "pod_deleted":
            return f"Pod deleted: '{data['name']}' from {data['namespace']}"
        else:
            return json.dumps(data)

    def _should_monitor_pod(self, pod: Pod) -> bool:
        """Check if pod matches monitoring criteria."""
        if self.pod_pattern:
            return self.pod_pattern in pod.name
        return True

    def _extract_pod_state(self, pod: Pod) -> Dict:
        """Extract current state from pod object."""
        status = pod.status

        # Container statuses
        container_statuses = status.get("containerStatuses", [])
        ready_count = sum(1 for cs in container_statuses if cs.get("ready", False))
        total_count = len(container_statuses)
        restart_count = sum(cs.get("restartCount", 0) for cs in container_statuses)

        # Get container states
        container_states = []
        for cs in container_statuses:
            state_info = cs.get("state", {})
            if "running" in state_info:
                container_states.append("running")
            elif "waiting" in state_info:
                reason = state_info["waiting"].get("reason", "Unknown")
                container_states.append(f"waiting:{reason}")
            elif "terminated" in state_info:
                reason = state_info["terminated"].get("reason", "Unknown")
                container_states.append(f"terminated:{reason}")

        return {
            "name": pod.name,
            "namespace": pod.namespace,
            "phase": status.get("phase", "Unknown"),
            "ready": ready_count,
            "total": total_count,
            "restarts": restart_count,
            "node": status.get("hostIP", "Unknown"),
            "container_states": container_states,
            "conditions": {c["type"]: c["status"] for c in status.get("conditions", [])},
        }

    def _has_state_changed(self, pod_key: str, new_state: Dict) -> bool:
        """Check if pod state has changed significantly."""
        if pod_key not in self.pod_states:
            return True

        old_state = self.pod_states[pod_key]

        # Check critical fields
        return (
            old_state.get("phase") != new_state["phase"] or
            old_state.get("ready") != new_state["ready"] or
            old_state.get("restarts") != new_state["restarts"] or
            old_state.get("container_states") != new_state["container_states"]
        )

    async def _monitor_pods_once(self, api: kr8s.asyncio.Api):
        """Single monitoring cycle - check all pods."""
        current_pods: Set[str] = set()

        for namespace in self.namespaces:
            try:
                # Get all pods in namespace (api.get returns async generator)
                # This will raise an exception if namespace doesn't exist
                pods = [pod async for pod in api.get("pods", namespace=namespace)]

                for pod in pods:
                    if not self._should_monitor_pod(pod):
                        continue

                    pod_key = f"{namespace}/{pod.name}"
                    current_pods.add(pod_key)

                    # Extract current state
                    new_state = self._extract_pod_state(pod)

                    # Check for new pod
                    if pod_key not in self.pod_states:
                        self.logger.info(self._format_output("pod_created", new_state))
                        self.pod_states[pod_key] = new_state
                        continue

                    # Check for state change
                    if self._has_state_changed(pod_key, new_state):
                        old_state = self.pod_states[pod_key]
                        new_state["old_phase"] = old_state.get("phase", "Unknown")
                        self.logger.info(self._format_output("pod_state_change", new_state))

                        # Detect transition to Failed state
                        old_phase = old_state.get("phase", "Unknown")
                        new_phase = new_state["phase"]
                        if new_phase == "Failed" and old_phase != "Failed":
                            self.logger.warning(f"Pod {pod_key} transitioned to Failed state - collecting diagnostics")
                            self._collect_failure_diagnostics(pod.name, namespace, new_state)

                        self.pod_states[pod_key] = new_state

                        # Track state change in history
                        if pod_key not in self.state_history:
                            self.state_history[pod_key] = []
                        self.state_history[pod_key].append({
                            "timestamp": datetime.now().isoformat(),
                            "event": "state_change",
                            "old_phase": old_state.get("phase", "Unknown"),
                            "new_phase": new_state["phase"],
                            "ready": f"{new_state['ready']}/{new_state['total']}",
                            "restarts": new_state["restarts"]
                        })
                        self.event_count += 1

            except Exception as e:
                # Silently skip non-existent namespaces or other errors
                # Common case: namespace doesn't exist (e.g., openshift-amd-gpu on K8s)
                error_msg = str(e).lower()
                if "not found" not in error_msg and "does not exist" not in error_msg:
                    # Only log non-namespace errors
                    self.logger.debug(f"Error monitoring namespace {namespace}: {e}")

        # Detect deleted pods
        deleted_pods = set(self.pod_states.keys()) - current_pods
        for pod_key in deleted_pods:
            state = self.pod_states.pop(pod_key)
            self.logger.info(self._format_output("pod_deleted", state))

    def _collect_failure_diagnostics(self, pod_name: str, namespace: str, state: Dict):
        """Collect diagnostic information for a failed pod using kubectl."""
        if not self.failure_reports_dir:
            return

        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_file = self.failure_reports_dir / f"{namespace}_{pod_name}_{timestamp}.json"

            diagnostics = {
                "pod": pod_name,
                "namespace": namespace,
                "timestamp": datetime.now().isoformat(),
                "final_state": state,
                "kubectl_describe": None,
                "container_logs": {},
                "previous_logs": {},
                "events": None,
            }

            # Run kubectl describe
            try:
                result = subprocess.run(
                    ["kubectl", "describe", "pod", pod_name, "-n", namespace],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    diagnostics["kubectl_describe"] = result.stdout
                else:
                    diagnostics["kubectl_describe"] = f"Error: {result.stderr}"
            except Exception as e:
                diagnostics["kubectl_describe"] = f"Failed to run kubectl describe: {e}"

            # Get container logs for each container
            containers = state.get("container_states", [])
            for i, container_state in enumerate(containers):
                container_name = f"container-{i}"

                # Try to get container name from describe output
                # This is a simplified approach - in production, parse container names properly
                try:
                    # Get current logs
                    result = subprocess.run(
                        ["kubectl", "logs", pod_name, "-n", namespace, "--tail=100"],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    if result.returncode == 0:
                        diagnostics["container_logs"][container_name] = result.stdout
                    else:
                        diagnostics["container_logs"][container_name] = f"Error: {result.stderr}"

                    # Try to get previous logs (if container restarted)
                    result = subprocess.run(
                        ["kubectl", "logs", pod_name, "-n", namespace, "--previous", "--tail=100"],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    if result.returncode == 0 and result.stdout:
                        diagnostics["previous_logs"][container_name] = result.stdout
                except Exception as e:
                    self.logger.debug(f"Could not get logs for {container_name}: {e}")

            # Get events related to this pod
            try:
                result = subprocess.run(
                    ["kubectl", "get", "events", "-n", namespace,
                     "--field-selector", f"involvedObject.name={pod_name}",
                     "-o", "json"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    diagnostics["events"] = json.loads(result.stdout)
                else:
                    diagnostics["events"] = f"Error: {result.stderr}"
            except Exception as e:
                diagnostics["events"] = f"Failed to get events: {e}"

            # Write report to file
            with open(report_file, 'w') as f:
                json.dump(diagnostics, f, indent=2)

            self.logger.info(f"Failure diagnostics collected: {report_file}")
            return str(report_file)

        except Exception as e:
            self.logger.error(f"Failed to collect diagnostics for {namespace}/{pod_name}: {e}")
            return None

    async def run(self):
        """Main monitoring loop."""
        self.running = True
        self.logger.info(f"Starting pod monitor for namespaces: {', '.join(self.namespaces)}")

        if self.pod_pattern:
            self.logger.info(f"Filtering pods matching pattern: '{self.pod_pattern}'")

        api = await kr8s.asyncio.api()
        while self.running:
            try:
                await self._monitor_pods_once(api)
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                self.logger.info("Monitor cancelled")
                break
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(self.poll_interval)

        self.logger.info("Pod monitor stopped")

        # Generate final reports
        self.stop()

    def _generate_summary_json(self) -> Dict:
        """Generate a structured JSON summary of monitoring session."""
        duration = datetime.now() - self.start_time

        # Count pods by phase
        phase_counts = {}
        restart_pods = []
        failed_pods = []

        for pod_key, state in self.pod_states.items():
            phase = state.get("phase", "Unknown")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1

            if state.get("restarts", 0) > 0:
                restart_pods.append({
                    "pod": pod_key,
                    "restarts": state.get("restarts", 0)
                })

            if phase in ["Failed", "Unknown"] or state.get("ready", 0) < state.get("total", 1):
                failed_pods.append({
                    "pod": pod_key,
                    "phase": phase,
                    "ready": f"{state.get('ready', 0)}/{state.get('total', 1)}",
                    "container_states": state.get("container_states", [])
                })

        return {
            "summary_type": "pod_monitor_final_summary",
            "duration_seconds": duration.total_seconds(),
            "namespaces": self.namespaces,
            "total_pods": len(self.pod_states),
            "state_changes": self.event_count,
            "phase_distribution": phase_counts,
            "pods_with_restarts": restart_pods,
            "failed_pods": failed_pods
        }

    def _generate_summary(self) -> str:
        """Generate a final summary of monitoring session."""
        duration = datetime.now() - self.start_time

        # Count pods by phase
        phase_counts = {}
        restart_pods = []
        failed_pods = []

        for pod_key, state in self.pod_states.items():
            phase = state.get("phase", "Unknown")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1

            if state.get("restarts", 0) > 0:
                restart_pods.append((pod_key, state))

            if phase in ["Failed", "Unknown"] or state.get("ready", 0) < state.get("total", 1):
                failed_pods.append((pod_key, state))

        # Build summary
        lines = [
            "",
            "=" * 70,
            "POD MONITOR SUMMARY",
            "=" * 70,
            f"Duration:          {duration}",
            f"Namespaces:        {', '.join(self.namespaces)}",
            f"Total pods:        {len(self.pod_states)}",
            f"State changes:     {self.event_count}",
            "",
            "Pod Status:"
        ]

        for phase in sorted(phase_counts.keys()):
            lines.append(f"  {phase:15s}: {phase_counts[phase]}")

        if restart_pods:
            lines.extend([
                "",
                f"Pods with Restarts ({len(restart_pods)}):"
            ])
            for pod_key, state in sorted(restart_pods, key=lambda x: x[1].get("restarts", 0), reverse=True):
                lines.append(f"  - {pod_key}: {state.get('restarts', 0)} restarts")

        if failed_pods:
            lines.extend([
                "",
                f"Failed/Problem Pods ({len(failed_pods)}):"
            ])
            for pod_key, state in failed_pods:
                ready = f"{state.get('ready', 0)}/{state.get('total', 1)}"
                phase = state.get('phase', 'Unknown')
                container_states = ', '.join(state.get('container_states', []))
                lines.append(f"  - {pod_key}")
                lines.append(f"    Phase: {phase}, Ready: {ready}")
                if container_states:
                    lines.append(f"    Containers: {container_states}")

        lines.append("=" * 70)
        return "\n".join(lines)

    def _dump_final_state(self) -> Optional[str]:
        """Dump detailed final state to JSON file."""
        if not self.log_file:
            return None

        # Create final state file path
        log_path = Path(self.log_file)
        final_state_file = log_path.parent / f"{log_path.stem}.final.json"

        final_state = {
            "summary": {
                "start_time": self.start_time.isoformat(),
                "end_time": datetime.now().isoformat(),
                "duration_seconds": (datetime.now() - self.start_time).total_seconds(),
                "namespaces": self.namespaces,
                "pod_pattern": self.pod_pattern,
                "total_pods": len(self.pod_states),
                "total_events": self.event_count,
            },
            "final_pod_states": {
                pod_key: {
                    **state,
                    "state_changes": self.state_history.get(pod_key, [])
                }
                for pod_key, state in self.pod_states.items()
            },
            "phase_distribution": {},
        }

        # Calculate phase distribution
        for state in self.pod_states.values():
            phase = state.get("phase", "Unknown")
            final_state["phase_distribution"][phase] = \
                final_state["phase_distribution"].get(phase, 0) + 1

        try:
            with open(final_state_file, 'w') as f:
                json.dump(final_state, f, indent=2)
            return str(final_state_file)
        except Exception as e:
            self.logger.error(f"Failed to write final state dump: {e}")
            return None

    def stop(self):
        """Stop the monitoring loop and generate final reports."""
        self.running = False

        # Generate and write summary to file (for shell script to display)
        summary_file = None
        if self.log_file:
            log_path = Path(self.log_file)
            summary_file = log_path.parent / f"{log_path.stem}.summary.txt"

            if self.output_format == "json":
                # In JSON mode, emit structured summary
                summary_data = self._generate_summary_json()
                with open(summary_file, 'w') as f:
                    json.dump(summary_data, f, indent=2)
            else:
                # In text mode, write human-readable summary
                summary = self._generate_summary()
                with open(summary_file, 'w') as f:
                    f.write(summary)

        # Dump detailed state to file
        final_state_file = self._dump_final_state()

        # Append final state file location and failure reports to summary
        if summary_file and final_state_file:
            with open(summary_file, 'a') as f:
                if self.output_format == "json":
                    f.write(f'\n{{"final_state_file": "{final_state_file}"}}\n')
                else:
                    f.write(f"\nFinal state dump saved to: {final_state_file}\n")

                # Add failure reports info
                if self.failure_reports_dir and self.failure_reports_dir.exists():
                    failure_files = list(self.failure_reports_dir.glob("*.json"))
                    if failure_files:
                        if self.output_format == "json":
                            f.write(f'{{"failure_reports": {json.dumps([str(f) for f in failure_files])}}}\n')
                        else:
                            f.write(f"\nFailure Reports ({len(failure_files)}):\n")
                            for failure_file in sorted(failure_files):
                                f.write(f"  - {failure_file}\n")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Monitor Kubernetes pods in real-time",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        "--namespace", "-n",
        help="Single namespace to monitor"
    )
    parser.add_argument(
        "--namespaces",
        help="Comma-separated list of namespaces to monitor"
    )
    parser.add_argument(
        "--pod-pattern", "-p",
        help="Filter pods by name pattern (substring match)"
    )
    parser.add_argument(
        "--output-format", "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)"
    )
    parser.add_argument(
        "--log-file", "-l",
        help="Write logs to file (in addition to stdout)"
    )
    parser.add_argument(
        "--poll-interval", "-i",
        type=int,
        default=5,
        help="Seconds between poll cycles (default: 5)"
    )

    args = parser.parse_args()

    # Determine namespaces to monitor
    if args.namespaces:
        namespaces = [ns.strip() for ns in args.namespaces.split(",")]
    elif args.namespace:
        namespaces = [args.namespace]
    else:
        print("ERROR: Must specify --namespace or --namespaces", file=sys.stderr)
        sys.exit(1)

    # Create monitor
    monitor = PodMonitor(
        namespaces=namespaces,
        pod_pattern=args.pod_pattern,
        output_format=args.output_format,
        log_file=args.log_file,
        poll_interval=args.poll_interval,
    )

    # Setup signal handlers
    def signal_handler(signum, frame):
        monitor.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Run monitor
    await monitor.run()


if __name__ == "__main__":
    asyncio.run(main())
