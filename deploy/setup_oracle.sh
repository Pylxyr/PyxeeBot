#!/usr/bin/env bash
# Oracle Cloud installer. For other hosts, use deploy/setup_gcp.sh (Google Cloud) or
# deploy/setup.sh (anything else) — same installer underneath, with a couple of
# Oracle-specific checks and reminders added here.
set -euo pipefail

# Default to wherever this script actually lives (repo_root/deploy/setup_oracle.sh),
# not a hardcoded path. This is what you're cloning-and-running from in practice, and
# it means a fresh `git clone` + `bash deploy/setup_oracle.sh` always targets the real
# checkout instead of silently checking a stale/mismatched default path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_NAME="${SERVICE_NAME:-musicbot}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_PATH="${APP_DIR}/.env"
PROVIDER_LABEL="Oracle Cloud"

provider_preflight() {
  # Oracle's Always Free compute includes both the AMD-based E2.1.Micro shape
  # (x86_64) and the Ampere A1 shape (aarch64). Both are fully supported —
  # curl_cffi (a yt-dlp dependency) ships prebuilt manylinux wheels for aarch64,
  # so there's nothing to work around either way. Purely informational.
  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64)  info "Detected x86_64 — this looks like an E2.1.Micro (AMD) shape." ;;
    aarch64) info "Detected aarch64 — this looks like an Ampere A1 (ARM) shape." ;;
    *)       info "Detected architecture: ${arch}." ;;
  esac
}

provider_postflight() {
  info "Oracle's Always Free tier includes a generous ~10 TB/month outbound data"
  info "allowance, so normal use of this bot won't come close to any transfer cap."
}

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
run_setup
