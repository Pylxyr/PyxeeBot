#!/usr/bin/env bash
# Shared install engine for PyxeeBot's deploy/setup_*.sh scripts.
#
# This file is sourced, never run directly. The three entry points —
# setup_oracle.sh, setup_gcp.sh, and setup.sh (for anything else) — each set
# a handful of variables and optional hook functions, then source this file
# and call run_setup. Keeping the actual install logic in one place means a
# fix here applies to all three hosts instead of having to be copy-pasted
# three times and inevitably drifting.
#
# Variables the caller must set before sourcing:
#   SCRIPT_DIR, APP_DIR, PYTHON_BIN, SERVICE_NAME, SERVICE_USER,
#   SYSTEMD_UNIT_PATH, ENV_PATH, PROVIDER_LABEL
#
# Optional hooks the caller may define before sourcing (no-ops if undefined):
#   provider_preflight()   — runs once, right after the repo-checkout check,
#                             before the wizard. Use for host-specific info
#                             (architecture, region checks, etc).
#   provider_postflight()  — runs once, after the bot is confirmed running,
#                             before the final "Useful commands" block. Use
#                             for host-specific reminders.

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi

info()    { echo "${CYAN}==>${RESET} $*"; }
success() { echo "${GREEN}✓${RESET} $*"; }
warn()    { echo "${YELLOW}!${RESET} $*"; }
error()   { echo "${RED}✗${RESET} $*"; }

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

fetch_bot_identity() {
  local token="$1" body
  body=$(curl -s --max-time 10 -H "Authorization: Bot ${token}" \
    "https://discord.com/api/v10/users/@me" 2>/dev/null) || body=""
  DISCORD_CLIENT_ID=$(echo "$body" | python3 -c "
import json,sys
try: print(json.load(sys.stdin).get('id',''))
except Exception: print('')
" 2>/dev/null) || DISCORD_CLIENT_ID=""
  DISCORD_BOT_NAME=$(echo "$body" | python3 -c "
import json,sys
try: print(json.load(sys.stdin).get('username',''))
except Exception: print('')
" 2>/dev/null) || DISCORD_BOT_NAME=""
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

run_setup() {
  if [[ ! -f "${APP_DIR}/requirements.txt" || ! -f "${APP_DIR}/deploy/musicbot.service" ]]; then
    echo "Could not find requirements.txt or deploy/musicbot.service inside ${APP_DIR}."
    echo ""
    echo "APP_DIR defaults to this script's own checkout (${SCRIPT_DIR}), so this usually"
    echo "means the clone is incomplete or corrupted rather than the wrong path. Re-clone"
    echo "fresh and re-run:"
    echo ""
    echo "  git clone https://github.com/Pylxyr/PyxeeBot.git ~/musicbot"
    echo "  cd ~/musicbot"
    echo "  bash $(basename "$0")"
    echo ""
    echo "(Deliberately pointing at a different, already-cloned checkout? Set"
    echo " APP_DIR=/that/path before running this script.)"
    exit 1
  fi

  if declare -f provider_preflight >/dev/null 2>&1; then
    provider_preflight
  fi

  if ! command -v curl >/dev/null 2>&1; then
    info "Installing curl (needed to verify your Discord token and Last.fm key)"
    sudo apt-get update -qq
    sudo apt-get install -y -qq curl
  fi

  echo ""
  echo "${BOLD}PyxeeBot setup — ${PROVIDER_LABEL}${RESET}"
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
     && ! grep -qE "^DISCORD_TOKEN=(replace_me|your_discord_bot_token_here)?$" "$ENV_PATH" 2>/dev/null; then
    HAS_EXISTING_ENV=true
  fi

  if [[ ! -t 0 ]]; then
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

    echo ""
    echo "${BOLD}3. Command Prefix${RESET} (optional, default: !)"
    echo "Any server can also override this later with !setprefix."
    read -rp "Default prefix [!]: " prefix_input
    DEFAULT_PREFIX_VALUE="${prefix_input:-!}"

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

  echo "[1/10] Installing system packages"
  sudo apt update
  sudo apt install -y python3 python3-venv ffmpeg logrotate libopus0 libsodium-dev curl

  echo "[2/10] Ensuring swap space"
  # e2-micro/E2.1.Micro-class instances have 1 GB RAM. MemoryMax=700M in the systemd
  # unit already caps the bot itself, but a burst during a big playlist import,
  # concurrent extraction, or an apt upgrade can still pressure the OS. A small swap
  # file is cheap insurance against an OOM-killed SSH session on a box this size —
  # skipped if swap already exists or RAM is generous enough that it's not needed.
  TOTAL_RAM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  if [[ "${TOTAL_RAM_MB}" -le 2048 ]] && [[ -z "$(swapon --show=NAME --noheadings 2>/dev/null)" ]]; then
    info "Low-memory instance detected (${TOTAL_RAM_MB} MB RAM), no swap configured — adding a 1 GB swap file."
    sudo fallocate -l 1G /swapfile 2>/dev/null || sudo dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    if ! grep -q '^/swapfile ' /etc/fstab; then
      echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    fi
    success "Swap enabled (1 GB)."
  else
    info "Swap already present or not needed — skipping."
  fi

  echo "[3/10] Validating Python runtime"
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    error "Python runtime '${PYTHON_BIN}' is not installed. Set PYTHON_BIN to an available interpreter."
    exit 1
  fi
  if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    error "Python 3.11 or newer is required. '${PYTHON_BIN}' does not meet that requirement."
    exit 1
  fi

  echo "[4/10] Preparing app directories"
  mkdir -p "${APP_DIR}"
  mkdir -p "${APP_DIR}/logs"
  mkdir -p "${APP_DIR}/data"

  echo "[5/10] Creating virtual environment"
  cd "${APP_DIR}" || exit 1
  if [[ ! -d .venv ]]; then
    "${PYTHON_BIN}" -m venv .venv
  fi

  echo "[6/10] Installing Python dependencies"
  source .venv/bin/activate
  python -m pip install --upgrade pip "setuptools>=83.0.0"
  python -m pip install -r requirements.txt
  deactivate

  echo "[7/10] Writing environment file"
  if [[ "$RUN_WIZARD" == true ]]; then
    {
      printf '# Generated by %s — see deploy/.env.example for every available option.\n\n' "$(basename "$0")"
      printf '# Required\n'
      printf 'DISCORD_TOKEN=%s\n' "${DISCORD_TOKEN_VALUE}"
      printf 'BOT_OWNERS=%s\n' "${BOT_OWNERS_VALUE}"
      printf '\n'
      printf '# Command prefix (default: !) — servers can override with !setprefix\n'
      printf 'DEFAULT_PREFIX=%s\n' "${DEFAULT_PREFIX_VALUE}"
      printf '\n'
      printf '# Logging\n'
      printf '# LOG_LEVEL=INFO\n'
      printf '# LOG_TO_FILE=true\n'
      printf '# LOG_DIR=logs\n'
      printf '\n'
      printf '# Queue limits\n'
      printf '# MAX_QUEUE_SIZE=100\n'
      printf '# MAX_QUEUE_SIZE_PER_USER=0\n'
      printf '# MAX_PLAYLIST_SIZE=25\n'
      printf '\n'
      printf '# Idle / empty-channel disconnect timeouts (seconds)\n'
      printf '# IDLE_TIMEOUT_SECONDS=180\n'
      printf '# EMPTY_CHANNEL_TIMEOUT_SECONDS=60\n'
      printf '\n'
      printf '# yt-dlp tuning\n'
      printf '# YTDLP_CONCURRENT_EXTRACTS=1\n'
      printf '# YTDLP_PREFETCH_COUNT=1\n'
      printf '# YTDLP_CURATION_CONCURRENCY=3\n'
      printf '# YTDLP_SEARCH_RESULTS=5\n'
      printf '# YTDLP_RESOLVE_CACHE_SIZE=128\n'
      printf '# YTDLP_RESOLVE_CACHE_TTL_SECONDS=1800\n'
      printf '# YTDLP_EXTRACT_TIMEOUT_SECONDS=45\n'
      printf '# YTDLP_SOCKET_TIMEOUT=15\n'
      printf '# NEAR_END_PREFETCH_SECONDS=30\n'
      printf '# YTDLP_COOKIES_FILE=cookies.txt\n'
      printf '# YTDLP_JS_RUNTIME_PATH=\n'
      printf '\n'
      printf '# Audio quality (kbps, 64-256)\n'
      printf '# OPUS_BITRATE_KBPS=64\n'
      printf '\n'
      printf '# Now-playing panel auto-refresh\n'
      printf '# NP_AUTO_REFRESH=false\n'
      printf '# NP_AUTO_REFRESH_INTERVAL=30\n'
      printf '\n'
      printf '# Error announcements in voice channels\n'
      printf '# ERROR_ANNOUNCE=true\n'
      printf '\n'
      printf '# Restore queue after bot restart\n'
      printf '# RESTORE_QUEUE_ON_RESTART=true\n'
      printf '\n'
      printf '# Last.fm API key — enables !vibe / !vibe-load and the per-server !autoplay toggle\n'
      printf 'LASTFM_API_KEY=%s\n' "${LASTFM_API_KEY_VALUE}"
    } > "${ENV_PATH}"
    success "Wrote ${ENV_PATH}"
  else
    info "Kept the existing .env unchanged."
  fi

  echo "[8/10] Installing logrotate config"
  # Template the path/user instead of installing the file verbatim — otherwise a custom
  # APP_DIR/SERVICE_USER silently doesn't take effect here even though the rest of the
  # install honors it.
  sed -e "s#/home/ubuntu/musicbot#${APP_DIR}#g" \
      -e "s#su ubuntu ubuntu#su ${SERVICE_USER} ${SERVICE_USER}#" \
      deploy/musicbot-logrotate | sudo tee "/etc/logrotate.d/${SERVICE_NAME}" >/dev/null

  echo "[9/10] Installing systemd unit"
  sed -e "s#/home/ubuntu/musicbot#${APP_DIR}#g" \
      -e "s#User=ubuntu#User=${SERVICE_USER}#" \
      deploy/musicbot.service | sudo tee "${SYSTEMD_UNIT_PATH}" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable "${SERVICE_NAME}"

  echo "[10/10] Starting the bot"
  sudo systemctl restart "${SERVICE_NAME}"
  sleep 3
  if sudo systemctl is-active --quiet "${SERVICE_NAME}"; then
    success "musicbot is running."
  else
    error "musicbot failed to start. Recent logs:"
    sudo journalctl -u "${SERVICE_NAME}" -n 30 --no-pager
    exit 1
  fi

  if declare -f provider_postflight >/dev/null 2>&1; then
    echo ""
    provider_postflight
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
}
