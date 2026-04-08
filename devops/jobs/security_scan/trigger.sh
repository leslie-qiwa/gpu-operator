#!/usr/bin/env sh

set -eu
(set -o pipefail) >/dev/null 2>&1 && set -o pipefail

# Downloads the security scan bundle via asset-pull, installs the wheel,
# and invokes jobd-security-scan-orchestrator to run the security scan.

usage() {
	echo "Usage: $0 <repo_name> [release=\$RELEASE] [branch=\$JOB_BASE_BRANCH] [poll_interval=60] [poll_timeout=7200] [venv_dir=.venv] [config_file=./config.yaml]" >&2
}

if [ "$#" -lt 1 ]; then
	echo "Error: missing required argument <repo_name>." >&2
	usage
	return 1 2>/dev/null || exit 1
fi

REPO_NAME="${1}"
RELEASE="${2:-${RELEASE:-}}"
BRANCH="${3:-${JOB_BASE_BRANCH:-}}"
POLL_INTERVAL="${4:-60}"
POLL_TIMEOUT="${5:-7200}"
VENV_DIR="${6:-.venv}"
CONFIG_FILE="${7:-./config.yaml}"

if [ -z "${RELEASE}" ]; then
	echo "Warning: release is not set. Skipping security scan. Provide <release> argument or set RELEASE environment variable." >&2
	usage
	return 0 2>/dev/null || exit 0
fi

if [ -z "${BRANCH}" ]; then
	echo "Warning: branch is not set. Skipping security scan. Provide <branch> argument or set JOB_BASE_BRANCH environment variable." >&2
	usage
	return 0 2>/dev/null || exit 0
fi

BUNDLE_ASSET_NAME="jobd_security_scan_bundle.tar.gz"
BUNDLE_PACKAGE_NAME="jobd_security_scan_utils"
BUNDLE_PACKAGE_VERSION="1.0"
WHEEL_NAME="jobd_security_scan_orchestrator-0.1.0-py3-none-any.whl"
JENKINS_CONFIG_NAME="jenkins_config.json"

if ! command -v python3 >/dev/null 2>&1; then
	echo "Error: python3 is not installed or not in PATH."
	return 1 2>/dev/null || exit 1
fi

if ! command -v asset-pull >/dev/null 2>&1; then
	echo "Error: asset-pull is not installed or not in PATH."
	return 1 2>/dev/null || exit 1
fi

# --- Download bundle via asset-pull ---
echo "Downloading security scan bundle: ${BUNDLE_ASSET_NAME}"
asset-pull \
	-b sw-repository \
	-a assets-hq.pensando.io:9000 \
	-n "${BUNDLE_ASSET_NAME}" \
	"${BUNDLE_PACKAGE_NAME}" \
	"${BUNDLE_PACKAGE_VERSION}" \
	"${BUNDLE_ASSET_NAME}"

if [ ! -f "${BUNDLE_ASSET_NAME}" ]; then
	echo "Error: Bundle not found after asset-pull: ${BUNDLE_ASSET_NAME}"
	return 1 2>/dev/null || exit 1
fi

# --- Extract bundle ---
BUNDLE_EXTRACT_DIR="jobd_security_scan_bundle"
echo "Extracting bundle to: ${BUNDLE_EXTRACT_DIR}"
mkdir -p "${BUNDLE_EXTRACT_DIR}"
tar -xzf "${BUNDLE_ASSET_NAME}" -C "${BUNDLE_EXTRACT_DIR}"

JENKINS_CONFIG="${BUNDLE_EXTRACT_DIR}/${JENKINS_CONFIG_NAME}"
if [ ! -f "${JENKINS_CONFIG}" ]; then
	echo "Error: Jenkins config not found after extraction: ${JENKINS_CONFIG}"
	return 1 2>/dev/null || exit 1
fi

WHEEL_PATH="${BUNDLE_EXTRACT_DIR}/${WHEEL_NAME}"
if [ ! -f "${WHEEL_PATH}" ]; then
	echo "Error: Wheel file not found after extraction: ${WHEEL_PATH}"
	return 1 2>/dev/null || exit 1
fi

# --- Set up virtual environment ---
if [ ! -d "${VENV_DIR}" ]; then
	echo "Creating virtual environment at: ${VENV_DIR}"
	python3 -m venv "${VENV_DIR}"
else
	echo "Virtual environment already exists at: ${VENV_DIR}"
fi

if [ ! -f "${VENV_DIR}/bin/activate" ]; then
	echo "Error: activation script not found at ${VENV_DIR}/bin/activate"
	return 1 2>/dev/null || exit 1
fi

# shellcheck disable=SC1091
. "${VENV_DIR}/bin/activate"

echo "Virtual environment ready: ${VENV_DIR}"

# --- Install wheel ---
echo "Installing wheel: ${WHEEL_PATH}"
python -m pip install "${WHEEL_PATH}"

echo "Wheel installed successfully."

# --- Resolve config file ---
if [ ! -f "${CONFIG_FILE}" ]; then
	if [ -f "./devops/jobs/security_scan/sanity/config.yaml" ]; then
		echo "Config file not found at specified path: ${CONFIG_FILE}"
		echo "Using default config file: ./devops/jobs/security_scan/sanity/config.yaml"
		CONFIG_FILE="./devops/jobs/security_scan/sanity/config.yaml"
	else
		echo "Error: config file not found: ${CONFIG_FILE}"
		return 1 2>/dev/null || exit 1
	fi
fi

# --- Invoke orchestrator ---
echo "Invoking: jobd-security-scan-orchestrator --release ${RELEASE} --repo-name ${REPO_NAME} --branch ${BRANCH} --config ${CONFIG_FILE} --jenkins-config ${JENKINS_CONFIG} --poll-interval ${POLL_INTERVAL} --poll-timeout ${POLL_TIMEOUT}"
jobd-security-scan-orchestrator \
	--release "${RELEASE}" \
	--repo-name "${REPO_NAME}" \
	--branch "${BRANCH}" \
	--config "${CONFIG_FILE}" \
	--jenkins-config "${JENKINS_CONFIG}" \
	--poll-interval "${POLL_INTERVAL}" \
	--poll-timeout "${POLL_TIMEOUT}"
