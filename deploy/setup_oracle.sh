#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/musicbot}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SERVICE_NAME="${SERVICE_NAME:-musicbot}"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_PATH="${APP_DIR}/.env"

if [[ ! -f "${APP_DIR}/requirements.txt" || ! -f "${APP_DIR}/deploy/musicbot.service" ]]; then
  echo "Could not find requirements.txt or deploy/musicbot.service inside ${APP_DIR}."
  echo ""
  echo "This script expects the PyxeeBot repo to already be cloned at ${APP_DIR}."
  echo "Clone it there first, then re-run this script from inside that directory:"
  echo ""
  echo "  git clone https://github.com/Pylxyr/PyxeeBot.git ${APP_DIR}"
  echo "  cd ${APP_DIR}"
  echo "  bash deploy/setup_oracle.sh"
  echo ""
  echo "(Cloned it somewhere else? Set APP_DIR=/that/path before running this script.)"
  exit 1
fi

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi

info()    { echo "${CYAN}==>${RESET} $*"; }
success() { echo "${GREEN}✓${RESET} $*"; }
warn()    { echo "${YELLOW}!${RESET} $*"; }
error()   { echo "${RED}✗${RESET} $*"; }

# ── Live validation helpers ─────────────────────────────────────────────────
# Each returns 0=valid, 1=confirmed invalid, 2=could not verify (network issue
# or unexpected response) — callers decide whether 2 is acceptable to proceed on.

validate_discord_token() {
  local token="$1" code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "Authorization: Bot ${token}" \
    "https://discord.com/api/v10/users/@me" 2>/dev/null) || code="000"
  if [[ "$code" == "200" ]]; then
    return 0
  elif [[ "$code" == "401" ]]; then
    return 1
  else
    return 2
  fi
}

# Sets DISCORD_CLIENT_ID and DISCORD_BOT_NAME. Only call after a 200 from
# validate_discord_token — does not re-check status itself.
fetch_bot_identity() {
  local token="$1" body
  body=$(curl -s --max-time 10 -H "Authorization: Bot ${token}" \
    "https://discord.com/api/v10/users/@me" 2>/dev/null) || body=""
  DISCORD_CLIENT_ID=$(echo "$body" | grep -oP '"id":\s*"\K[0-9]+' | head -1) || DISCORD_CLIENT_ID=""
  DISCORD_BOT_NAME=$(echo "$body" | grep -oP '"username":\s*"\K[^"]+' | head -1) || DISCORD_BOT_NAME=""
}

validate_lastfm_key() {
  local key="$1" raw http_code body
  raw=$(curl -s --max-time 10 -w '\n%{http_code}' \
    "https://ws.audioscrobbler.com/2.0/?method=chart.gettopartists&api_key=${key}&format=json&limit=1" \
    2>/dev/null) || raw=""
  if [[ -z "$raw" ]]; then
    return 2
  fi
  http_code="${raw##*$'\n'}"
  body="${raw%$'\n'*}"
  if [[ "$http_code" != "200" ]]; then
    return 2
  fi
  if echo "$body" | grep -q '"error"'; then
    return 1
  fi
  if echo "$body" | grep -q '"artist"\|"chart"'; then
    return 0
  fi
  return 2
}

# ── curl must exist before the wizard can validate anything ────────────────
if ! command -v curl >/dev/null 2>&1; then
  info "Installing curl (needed to verify your Discord token and Last.fm key)"
  sudo apt-get update -qq
  sudo apt-get install -y -qq curl
fi

echo ""
echo "${BOLD}PyxeeBot setup${RESET}"
echo "This installs everything and starts the bot. It asks a few questions"
echo "up front, then the rest runs unattended."
echo ""

DISCORD_TOKEN_VALUE=""
DISCORD_CLIENT_ID=""
DISCORD_BOT_NAME=""
BOT_OWNERS_VALUE=""
DEFAULT_PREFIX_VALUE="!"
LASTFM_API_KEY_VALUE=""
RUN_WIZARD=true

HAS_EXISTING_ENV=false
if [[ -f "$ENV_PATH" ]] \
   && grep -q "^DISCORD_TOKEN=" "$ENV_PATH" 2>/dev/null \
   && ! grep -qE "^DISCORD_TOKEN=(replace_me)?$" "$ENV_PATH" 2>/dev/null; then
  HAS_EXISTING_ENV=true
fi

if [[ ! -t 0 ]]; then
  # No terminal attached to stdin — every `read` below would hit EOF
  # immediately and, under set -e, silently kill the script with no
  # explanation. Handle it explicitly instead.
  if [[ "$HAS_EXISTING_ENV" == true ]]; then
    RUN_WIZARD=false
    info "No interactive terminal detected — reusing the existing .env without prompting."
  else
    error "This is the first run and needs an interactive terminal to ask for your"
    error "Discord token, etc. — but none is attached to stdin."
    echo ""
    echo "If you're connecting over SSH, reconnect with a pseudo-terminal allocated:"
    echo "  ssh -t user@host"
    echo "Then run this script directly on the machine, not piped through a command."
    exit 1
  fi
elif [[ "$HAS_EXISTING_ENV" == true ]]; then
  echo "Found an existing, filled-in .env at ${ENV_PATH}."
  read -rp "Reconfigure it? [y/N] " reconf
  if [[ ! "$reconf" =~ ^[Yy]$ ]]; then
    RUN_WIZARD=false
    info "Keeping the existing .env — skipping configuration questions."
  fi
fi

if [[ "$RUN_WIZARD" == true ]]; then
  # ── 1. Discord Bot Token ───────────────────────────────────────────────
  echo ""
  echo "${BOLD}1. Discord Bot Token${RESET} (required)"
  echo "This is how the bot logs in to Discord. If you don't have one yet:"
  echo "  1. Go to ${CYAN}https://discord.com/developers/applications${RESET} and create a New Application"
  echo "  2. Open the ${BOLD}Bot${RESET} tab → click Reset Token (or Copy if shown) to get the token"
  echo "  3. On that same tab, under ${BOLD}Privileged Gateway Intents${RESET}, enable"
  echo "     ${BOLD}MESSAGE CONTENT INTENT${RESET} — the bot reads message content for ! commands"
  echo "     and won't respond to anything without it"
  echo "  Full walkthrough: ${CYAN}https://discordpy.readthedocs.io/en/stable/discord.html${RESET}"
  echo ""
  while true; do
    read -rsp "Paste your Discord Bot Token (input hidden): " input_token
    echo ""
    if [[ -z "$input_token" ]]; then
      error "Token can't be empty."
      continue
    fi
    info "Checking with Discord..."
    result=0
    validate_discord_token "$input_token" || result=$?
    if [[ $result -eq 0 ]]; then
      fetch_bot_identity "$input_token"
      success "Token valid — connected as ${DISCORD_BOT_NAME:-your bot}${DISCORD_CLIENT_ID:+ (ID ${DISCORD_CLIENT_ID})}"
      DISCORD_TOKEN_VALUE="$input_token"
      break
    elif [[ $result -eq 1 ]]; then
      error "Discord rejected this token (401 Unauthorized)."
      warn "Common cause: pasting the Client Secret instead of the Bot Token —"
      warn "double-check the Bot tab specifically, not General Information."
      echo ""
    else
      warn "Could not reach Discord to verify the token (network issue?)."
      read -rp "Use it anyway without verifying? [y/N] " skip_verify
      if [[ "$skip_verify" =~ ^[Yy]$ ]]; then
        DISCORD_TOKEN_VALUE="$input_token"
        break
      fi
    fi
  done

  # ── 2. Bot Owner ────────────────────────────────────────────────────────
  echo ""
  echo "${BOLD}2. Bot Owner${RESET} (optional, recommended)"
  echo "Whoever created the application above is automatically treated as an"
  echo "owner — able to use owner-only commands like !stats. BOT_OWNERS lets"
  echo "you grant that to extra people too, by Discord User ID (not username)."
  echo "To find a user ID: User Settings → Advanced → enable Developer Mode,"
  echo "then right-click any user → Copy User ID."
  echo ""
  while true; do
    read -rp "Comma-separated Discord User IDs, or press Enter to skip: " owners_input
    if [[ -z "$owners_input" ]]; then
      BOT_OWNERS_VALUE=""
      break
    fi
    if [[ "$owners_input" == *" "* && "$owners_input" != *","* ]]; then
      error "Separate multiple IDs with commas, not spaces."
      continue
    fi
    cleaned="${owners_input//[[:space:]]/}"
    if [[ "$cleaned" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
      BOT_OWNERS_VALUE="$cleaned"
      success "Saved: ${BOT_OWNERS_VALUE}"
      break
    else
      error "Must be one or more numeric Discord user IDs, comma-separated."
    fi
  done

  # ── 3. Default command prefix ────────────────────────────────────────────
  echo ""
  echo "${BOLD}3. Command Prefix${RESET} (optional, default: !)"
  echo "Any server can also override this later with !setprefix."
  read -rp "Default prefix [!]: " prefix_input
  DEFAULT_PREFIX_VALUE="${prefix_input:-!}"

  # ── 4. Last.fm API key ───────────────────────────────────────────────────
  echo ""
  echo "${BOLD}4. Last.fm API Key${RESET} (optional)"
  echo "${BOLD}Needed for:${RESET} !vibe / !vibe-load (similar-track discovery) and the"
  echo "  per-server !autoplay toggle (queues a similar track when the queue"
  echo "  empties)."
  echo "${BOLD}Not needed for:${RESET} !play, !search, !queue, playlists, or anything"
  echo "  else — those all work fully without it."
  echo "Free key, ~30 seconds, no approval wait: ${CYAN}https://www.last.fm/api/account/create${RESET}"
  echo ""
  while true; do
    read -rsp "Last.fm API key (input hidden), or press Enter to skip: " lastfm_input
    echo ""
    if [[ -z "$lastfm_input" ]]; then
      LASTFM_API_KEY_VALUE=""
      break
    fi
    info "Checking with Last.fm..."
    result=0
    validate_lastfm_key "$lastfm_input" || result=$?
    if [[ $result -eq 0 ]]; then
      success "Last.fm key valid."
      LASTFM_API_KEY_VALUE="$lastfm_input"
      break
    elif [[ $result -eq 1 ]]; then
      error "Last.fm rejected this key."
      warn "Double-check it at https://www.last.fm/api/accounts"
      echo ""
    else
      warn "Could not verify with Last.fm (network issue?)."
      read -rp "Use it anyway without verifying? [y/N] " skip_verify
      if [[ "$skip_verify" =~ ^[Yy]$ ]]; then
        LASTFM_API_KEY_VALUE="$lastfm_input"
        break
      fi
    fi
  done
fi

echo ""
echo "${BOLD}Configuration done — the rest installs unattended.${RESET}"
echo ""

echo "[1/9] Installing system packages"
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg logrotate libopus0 libsodium-dev curl

echo "[2/9] Validating Python runtime"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  error "Python runtime '${PYTHON_BIN}' is not installed. Set PYTHON_BIN to an available interpreter."
  exit 1
fi
if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  error "Python 3.11 or newer is required. '${PYTHON_BIN}' does not meet that requirement."
  exit 1
fi

echo "[3/9] Preparing app directories"
mkdir -p "${APP_DIR}"
mkdir -p "${APP_DIR}/logs"

echo "[4/9] Creating virtual environment"
cd "${APP_DIR}"
if [[ ! -d .venv ]]; then
  "${PYTHON_BIN}" -m venv .venv
fi

echo "[5/9] Installing Python dependencies"
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
deactivate

echo "[6/9] Writing environment file"
if [[ "$RUN_WIZARD" == true ]]; then
  cat > "${ENV_PATH}" << EOF
# Generated by setup_oracle.sh — see deploy/.env.example for every available option.

# Required
DISCORD_TOKEN=${DISCORD_TOKEN_VALUE}
BOT_OWNERS=${BOT_OWNERS_VALUE}

# Command prefix (default: !) — servers can override with !setprefix
DEFAULT_PREFIX=${DEFAULT_PREFIX_VALUE}

# Logging
# LOG_LEVEL=INFO
# LOG_TO_FILE=true
# LOG_DIR=logs

# Queue limits
# MAX_QUEUE_SIZE=100
# MAX_QUEUE_SIZE_PER_USER=0
# MAX_PLAYLIST_SIZE=25

# Idle / empty-channel disconnect timeouts (seconds)
# IDLE_TIMEOUT_SECONDS=180
# EMPTY_CHANNEL_TIMEOUT_SECONDS=60

# yt-dlp tuning
# YTDLP_CONCURRENT_EXTRACTS=1
# YTDLP_PREFETCH_COUNT=1
# YTDLP_CURATION_CONCURRENCY=3
# YTDLP_SEARCH_RESULTS=5
# YTDLP_RESOLVE_CACHE_SIZE=128
# YTDLP_RESOLVE_CACHE_TTL_SECONDS=1800
# YTDLP_EXTRACT_TIMEOUT_SECONDS=45
# YTDLP_SOCKET_TIMEOUT=15
# NEAR_END_PREFETCH_SECONDS=30
# YTDLP_COOKIES_FILE=cookies.txt
# YTDLP_JS_RUNTIME_PATH=

# Audio quality (kbps, 64-256)
# OPUS_BITRATE_KBPS=64

# Now-playing panel auto-refresh
# NP_AUTO_REFRESH=false
# NP_AUTO_REFRESH_INTERVAL=30

# Error announcements in voice channels
# ERROR_ANNOUNCE=true

# Restore queue after bot restart
# RESTORE_QUEUE_ON_RESTART=true

# Last.fm API key — enables !vibe / !vibe-load and the per-server !autoplay toggle
LASTFM_API_KEY=${LASTFM_API_KEY_VALUE}
EOF
  success "Wrote ${ENV_PATH}"
else
  info "Kept the existing .env unchanged."
fi

echo "[7/9] Installing logrotate config"
sudo install -m 0644 deploy/musicbot-logrotate "/etc/logrotate.d/${SERVICE_NAME}"

echo "[8/9] Installing systemd unit"
sudo install -m 0644 deploy/musicbot.service "${SYSTEMD_UNIT_PATH}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

echo "[9/9] Starting the bot"
sudo systemctl restart "${SERVICE_NAME}"
sleep 3
if sudo systemctl is-active --quiet "${SERVICE_NAME}"; then
  success "musicbot is running."
else
  error "musicbot failed to start. Recent logs:"
  sudo journalctl -u "${SERVICE_NAME}" -n 30 --no-pager
  exit 1
fi

echo ""
echo "${BOLD}Setup complete.${RESET}"

if [[ -n "$DISCORD_CLIENT_ID" ]]; then
  echo ""
  echo "Invite your bot to a server:"
  echo "  ${CYAN}https://discord.com/oauth2/authorize?client_id=${DISCORD_CLIENT_ID}&permissions=3230720&scope=bot%20applications.commands${RESET}"
  echo "  (View Channels, Send Messages, Embed Links, Read Message History, Connect, Speak)"
fi

echo ""
echo "Useful commands:"
echo "  sudo systemctl status ${SERVICE_NAME}      — check it's running"
echo "  sudo journalctl -u ${SERVICE_NAME} -f       — follow live logs"
echo "  tail -f ${APP_DIR}/logs/musicbot.log        — follow the log file"
echo "  sudo systemctl restart ${SERVICE_NAME}     — restart after editing .env"
echo ""
echo "In Discord, run !commands to see everything the bot can do."
