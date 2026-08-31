#!/usr/bin/env bash
# Generic installer — use this on anything that isn't Oracle Cloud or Google Cloud
# (DigitalOcean, Hetzner, AWS, a home server, bare metal, etc). If you *are* on
# Oracle or GCP, use deploy/setup_oracle.sh or deploy/setup_gcp.sh instead — same
# installer underneath, with a couple of host-specific checks and reminders added.
# Requires a systemd-based Linux distro (Ubuntu/Debian and most others) — this
# installs the bot as a systemd service and uses apt for system packages.
set -euo pipefail

# Default to wherever this script actually lives (repo_root/deploy/setup.sh), not a
# hardcoded path. This is what you're cloning-and-running from in practice, and it
# means a fresh `git clone` + `bash deploy/setup.sh` always targets the real
# checkout instead of silently checking a stale/mismatched default path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_NAME="${SERVICE_NAME:-musicbot}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_PATH="${APP_DIR}/.env"
PROVIDER_LABEL="generic host"

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
run_setup
