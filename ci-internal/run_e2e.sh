#!/bin/bash

set -e

if ! command -v uuidgen >/dev/null 2>&1; then
  echo "uuidgen not found, installing..."
  sudo apt-get update && sudo apt-get install -y uuid-runtime
fi

trap '
  echo ">>> Running techsupport before exit"
  tools/techsupport_dump.sh -k ~/.kube/config all || true

  echo ">>> Waiting for techsupport .tgz to be created (timeout: 2 minutes)"
  for i in {1..24}; do
    tgz_file=$(ls techsupport-*.tgz 2>/dev/null | head -n1)
    if [ -n "$tgz_file" ]; then
      echo ">>> Found $tgz_file, copying and cleaning up..."
      cp "$tgz_file" techsupport.tgz
      rm -f "$tgz_file"
      find . -name "techsupport.tgz"
      break
    fi
    sleep 5
  done

  # Bundle e2e-artifacts (per-test logs + snapshots from TestMonitor)
  if [ -d tests/e2e/e2e-artifacts ]; then
    echo ">>> Packaging e2e-artifacts..."
    tar czf e2e-artifacts.tgz -C tests/e2e e2e-artifacts
    echo ">>> e2e-artifacts.tgz created ($(du -sh e2e-artifacts.tgz | cut -f1))"
  else
    echo ">>> No e2e-artifacts directory found, skipping"
  fi
' EXIT

# Run the e2e tests
CI_ENV=1 SIM_ENABLE=1 make -C tests/e2e/