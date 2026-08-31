#!/usr/bin/env bash
# Google Cloud installer. For other hosts, use deploy/setup_oracle.sh (Oracle Cloud)
# or deploy/setup.sh (anything else) — same installer underneath, with a couple of
# GCP-specific checks and reminders added here.
set -euo pipefail

# Default to wherever this script actually lives (repo_root/deploy/setup_gcp.sh), not
# a hardcoded path. This is what you're cloning-and-running from in practice, and it
# means a fresh `git clone` + `bash deploy/setup_gcp.sh` always targets the real
# checkout instead of silently checking a stale/mismatched default path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_NAME="${SERVICE_NAME:-musicbot}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_PATH="${APP_DIR}/.env"
PROVIDER_LABEL="Google Cloud"

FREE_TIER_REGIONS=("us-west1" "us-central1" "us-east1")

provider_preflight() {
  # Always Free's e2-micro instance is only free in three specific US regions —
  # everywhere else, this is a normal billed VM regardless of machine type. Ask the
  # instance metadata server (only reachable from inside an actual GCP VM) rather
  # than guessing from the hostname.
  local zone_path region found=false
  zone_path=$(curl -s -m 3 --fail -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/zone" 2>/dev/null) || zone_path=""
  if [[ -z "$zone_path" ]]; then
    warn "Could not reach the GCP metadata server to check the region — skipping that check."
    warn "(Expected if you're testing this script outside an actual GCP VM.)"
    return
  fi
  # zone_path looks like "projects/123456789/zones/us-central1-a"
  region="$(basename "$zone_path" | sed -E 's/-[a-z]$//')"
  for r in "${FREE_TIER_REGIONS[@]}"; do
    [[ "$region" == "$r" ]] && found=true
  done
  if [[ "$found" == true ]]; then
    success "Region ${region} is Always Free eligible."
  else
    warn "This VM is in ${region}, which is NOT one of the Always Free regions"
    warn "(${FREE_TIER_REGIONS[*]}). You'll be billed for compute, disk, and egress"
    warn "here regardless of machine type — nothing this script does changes that."
    echo ""
  fi
}

provider_postflight() {
  info "GCP Always Free gives this VM 1 GB of outbound data transfer per month —"
  info "a much tighter budget than Oracle's ~10 TB. At the bot's minimum 64 kbps"
  info "voice bitrate, that's roughly 36 hours of total playback per month before"
  info "small (~\$0.12/GB) egress charges kick in. Two more things worth checking"
  info "in the console (this script can't verify them from inside the VM):"
  info "  - Network Service Tier is Premium, not Standard — Standard gets no free"
  info "    egress allowance at all."
  info "  - The boot disk is Standard persistent disk (≤30 GB) — the console"
  info "    defaults new VMs to Balanced persistent disk, which isn't free."
  info "Consider setting a small budget alert (Billing → Budgets & alerts) as a"
  info "safety net."
}

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
run_setup
