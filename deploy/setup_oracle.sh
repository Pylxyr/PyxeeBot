#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/musicbot}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_NAME="${SERVICE_NAME:-musicbot}"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

echo "[1/8] Installing system packages"
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg logrotate libopus0 libsodium-dev

echo "[2/8] Validating Python runtime"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "Python runtime '${PYTHON_BIN}' is not installed. Set PYTHON_BIN to an available interpreter."
  exit 1
fi
if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11 or newer is required. '${PYTHON_BIN}' does not meet that requirement."
  exit 1
fi

echo "[3/8] Preparing app directories"
mkdir -p "${APP_DIR}"
mkdir -p "${APP_DIR}/logs"

echo "[4/8] Creating virtual environment"
cd "${APP_DIR}"
if [[ ! -d .venv ]]; then
  "${PYTHON_BIN}" -m venv .venv
fi

echo "[5/8] Installing Python dependencies"
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "[6/8] Validating environment file"
if [[ ! -f .env ]]; then
  # Write a minimal template so the service file's EnvironmentFile= directive
  # doesn't fail even before the operator fills in real values.
  cat > .env << 'EOF'
# Edit this file and set DISCORD_TOKEN before starting the bot.
# See deploy/.env.example for all available options.
DISCORD_TOKEN=replace_me
BOT_OWNERS=replace_me
EOF
  echo "Created .env stub. Edit ${APP_DIR}/.env with your Discord token before starting the service."
fi

echo "[7/8] Installing logrotate config"
sudo install -m 0644 deploy/musicbot-logrotate "/etc/logrotate.d/${SERVICE_NAME}"

echo "[8/8] Installing systemd unit"
sudo install -m 0644 deploy/musicbot.service "${SYSTEMD_UNIT_PATH}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

echo "Done"
echo "If .env already contains DISCORD_TOKEN, start the bot with:"
echo "  sudo systemctl restart ${SERVICE_NAME}"
echo "Check logs with:"
echo "  tail -f ${APP_DIR}/logs/musicbot.log"
echo "  sudo journalctl -u ${SERVICE_NAME} -f"
