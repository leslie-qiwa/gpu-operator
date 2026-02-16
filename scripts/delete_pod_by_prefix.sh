#!/bin/sh

# Usage: ./delete_pod_by_prefix.sh <prefix> [namespace]

PREFIX="$1"
NAMESPACE="${2:-kube-amd-gpu}"  # Use "default" namespace if none is provided

if [ -z "$PREFIX" ]; then
	  echo "Usage: $0 <pod-name-prefix> [namespace]"
	    exit 1
fi

# Get the pod name that matches the prefix
POD_NAME=$(kubectl get pods -n "$NAMESPACE" --no-headers -o custom-columns=":metadata.name" | grep "^$PREFIX" | head -n 1)

if [ -z "$POD_NAME" ]; then
	  echo "No pod found with prefix '$PREFIX' in namespace '$NAMESPACE'"
	    exit 1
fi

# Delete the pod
echo "Deleting pod: $POD_NAME in namespace: $NAMESPACE"
kubectl delete pod "$POD_NAME" -n "$NAMESPACE"

