#!/data/data/com.termux/files/usr/bin/bash
# Twitch Radio Bot — Termux (Android) installer.
#
# Standalone counterpart to deploy/setup.sh (which drives a systemd-based
# Ubuntu/Debian VPS install). Termux has no systemd, no apt/sudo, and —
# the one substantive difference from the VPS path below — Deno doesn't
# reliably run on it at all:
#
#   Deno on Termux/Android links against glibc, not Android's own Bionic
#   libc. Termux's package build for it has a long, still-unresolved history
#   of being added-then-disabled for aarch64 (see termux/termux-packages
#   issues #5322, #8689, #17398) — it's not just "not packaged", the
#   underlying libc mismatch is the actual blocker. This script uses Node
#   instead: Termux's `nodejs` package is mature and natively built for
#   Bionic (recommended directly by nodejs.org's own install docs for
#   Android/Termux), and this project's config.py already supports
#   YTDLP_JS_RUNTIME_NAME for exactly this — no code change needed, this
#   script just points it at Node instead of Deno.
#
# Also notably simpler than a discord.py bot's Termux install: nothing here
# (twitchio, yt-dlp, aiohttp, python-dotenv) needs Rust/maturin/PyNaCl-style
# native-extension workarounds — plain pip install, same as the VPS script.
#
# Usage (from repo root):
#   bash deploy/setup_termux.sh

set -euo pipefail

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; CYAN=$'\033[0;36m'; RESET=$'\033[0m'
else
  BOLD=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi
info()    { echo "${CYAN}==>${RESET} $*"; }
success() { echo "${GREEN}✓${RESET} $*"; }
warn()    { echo "${YELLOW}!${RESET} $*"; }
error()   { echo "${RED}✗${RESET} $*"; }

# ── Guard: Termux only ─────────────────────────────────────────────────
if [[ -z "${TERMUX_VERSION:-}" && ! -d /data/data/com.termux ]]; then
  error "This script is only for Termux on Android."
  error "For a Ubuntu/Debian VPS use: bash deploy/setup.sh"
  exit 1
fi

# ── .env helpers (verbatim from deploy/setup.sh — kept identical on purpose
#    so both installers behave the same way; see that file for the "why" on
#    each one) ──────────────────────────────────────────────────────────
get_env_var() {  # get_env_var KEY FILE — prints current value, "" if unset/missing
  local key="$1" file="$2" line
  [[ -f "${file}" ]] || return 0
  line="$(grep -m1 "^${key}=" "${file}" 2>/dev/null || true)"
  printf '%s' "${line#"${key}"=}"
}

set_env_var() {  # set_env_var KEY VALUE FILE — replaces KEY=... in place, appends if absent
  local key="$1" value="$2" file="$3" tmp line found=0
  tmp="$(mktemp)"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" == "${key}="* ]]; then
      printf '%s=%s\n' "${key}" "${value}" >>"${tmp}"
      found=1
    else
      printf '%s\n' "${line}" >>"${tmp}"
    fi
  done <"${file}"
  if [[ "${found}" -eq 0 ]]; then
    printf '%s=%s\n' "${key}" "${value}" >>"${tmp}"
  fi
  mv "${tmp}" "${file}"
}

trim() {  # pure-bash whitespace trim — no external command, safe with any content
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "${s}"
}

REQUIRED_ENV_KEYS=(TWITCH_CLIENT_ID TWITCH_CLIENT_SECRET TWITCH_BOT_ID TWITCH_OWNER_ID)

missing_required_env() {
  local key
  for key in "${REQUIRED_ENV_KEYS[@]}"; do
    if [[ -z "$(get_env_var "${key}" "${ENV_PATH}")" ]]; then
      echo "${key}"
    fi
  done
}

prompt_env_field() {  # prompt_env_field KEY SECRET(0/1) NUMERIC(0/1) instruction-lines...
  local key="$1" secret="$2" numeric="$3"
  shift 3
  if [[ -n "$(get_env_var "${key}" "${ENV_PATH}")" ]]; then
    info "${key} is already set — leaving it alone."
    return
  fi
  echo ""
  echo "${CYAN}${key}${RESET}"
  local line
  for line in "$@"; do
    echo "  ${line}"
  done
  local value=""
  while true; do
    if [[ "${secret}" == "1" ]]; then
      read -r -s -p "  Paste value (input hidden, Enter to skip): " value || true
      echo ""
    else
      read -r -p "  Paste value (Enter to skip): " value || true
    fi
    value="$(trim "${value}")"
    if [[ -z "${value}" ]]; then
      warn "${key} left blank — set it by hand later in ${ENV_PATH}."
      return
    fi
    if [[ "${numeric}" == "1" && ! "${value}" =~ ^[0-9]+$ ]]; then
      warn "Not a plain numeric ID (digits only — no username, no label like 'Twitch ID:')."
      warn "The service refuses to start with this. Try again, or leave blank to skip."
      continue
    fi
    break
  done
  set_env_var "${key}" "${value}" "${ENV_PATH}"
  success "${key} saved."
}

prompt_optional_field() {  # prompt_optional_field KEY DEFAULT SECRET(0/1) NUMERIC(0/1) description-line...
  local key="$1" default="$2" secret="$3" numeric="$4"
  shift 4
  if [[ -n "$(get_env_var "${key}" "${ENV_PATH}")" ]]; then
    info "${key} is already set — leaving it alone."
    return
  fi
  local shown_default="${default}"
  [[ -z "${shown_default}" ]] && shown_default="disabled"
  echo ""
  echo "${CYAN}${key}${RESET}"
  local line
  for line in "$@"; do
    echo "  ${line}"
  done
  local value=""
  if [[ "${secret}" == "1" ]]; then
    read -r -s -p "  Value (input hidden, Enter for ${shown_default}): " value || true
    echo ""
  else
    read -r -p "  Value [${shown_default}]: " value || true
  fi
  value="$(trim "${value}")"
  if [[ -z "${value}" ]]; then
    value="${default}"
  elif [[ "${numeric}" == "1" && ! "${value}" =~ ^[0-9]+$ ]]; then
    warn "That doesn't look like a number — using the default (${shown_default}) instead."
    value="${default}"
  fi
  [[ -z "${value}" ]] && return
  set_env_var "${key}" "${value}" "${ENV_PATH}"
  success "${key} = ${value}"
}

# ── Setup ────────────────────────────────────────────────────────────────
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="twitch-radio"
ENV_PATH="${APP_DIR}/.env"
VENV_DIR="${APP_DIR}/.venv"
REQ_FILE="${APP_DIR}/requirements.txt"

cd "${APP_DIR}"
if [[ ! -f "${REQ_FILE}" || ! -f "${APP_DIR}/bot.py" ]]; then
  error "Could not find requirements.txt or bot.py inside ${APP_DIR}."
  error "Run this from the repo root: git clone https://github.com/Pylxyr/pyxee-radio-bot.git && cd pyxee-radio-bot && bash deploy/setup_termux.sh"
  exit 1
fi

echo ""
echo "${BOLD}Twitch Radio Bot — Termux (Android) setup${RESET}"
echo "App directory: ${APP_DIR}"
echo ""

command -v curl >/dev/null 2>&1 || { info "Installing curl"; pkg install -y curl; }

# ── 1. System packages ──────────────────────────────────────────────────
info "[1/9] Installing Termux packages"
pkg update -y
pkg install -y python ffmpeg git curl clang make binutils libffi openssl nodejs 2>/dev/null || true
success "System packages ready"

# ── 2. JS runtime (Node, not Deno — see header comment) ────────────────
info "[2/9] Checking the JS runtime (yt-dlp needs one for full YouTube support)"
if ! command -v node >/dev/null 2>&1; then
  warn "node not found on PATH even after 'pkg install nodejs' — song requests will still work for"
  warn "plain URLs, but YouTube *searches* and some videos will be degraded or fail."
  warn "Try 'pkg install nodejs' manually and re-run this script."
  NODE_PATH=""
else
  NODE_PATH="$(command -v node)"
  node_major="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
  if [[ "${node_major}" =~ ^[0-9]+$ ]] && (( node_major < 22 )); then
    warn "Node $(node --version) is older than the 22+ yt-dlp-ejs needs — YouTube extraction may"
    warn "degrade or fail. Try 'pkg install nodejs' again (Termux tracks current upstream Node, so"
    warn "this is usually just a stale package cache) or 'pkg install nodejs-lts' as an alternative."
  else
    success "Node $(node --version) at ${NODE_PATH}"
  fi
fi

# ── 3. Python version ────────────────────────────────────────────────────
info "[3/9] Checking Python (3.11+ required)"
if ! python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  error "Python 3.11+ required. Try: pkg upgrade python"
  exit 1
fi
success "Python $(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"

# ── 4. Virtual environment ───────────────────────────────────────────────
info "[4/9] Creating virtual environment"
mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs"
[[ -d "${VENV_DIR}" ]] || python -m venv "${VENV_DIR}"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip -q
success "venv ready"

# ── 5. Python dependencies ──────────────────────────────────────────────
info "[5/9] Installing Python dependencies"
if ! pip install -r "${REQ_FILE}" -q 2>/tmp/pip_err.log; then
  if grep -qi "curl_cffi\|curl-cffi" /tmp/pip_err.log 2>/dev/null; then
    warn "Full requirements.txt failed, likely on yt-dlp's curl-cffi extra (needs a from-source build"
    warn "here) — retrying without it. Everything else (search, playback) works the same without it;"
    warn "it only helps yt-dlp better impersonate a browser on sites that fingerprint requests."
    ytdlp_bare="yt-dlp==$(grep -E '^yt-dlp' "${REQ_FILE}" | head -n1 | sed -E 's/.*==//')"
    grep -vE '^yt-dlp' "${REQ_FILE}" > /tmp/requirements_no_ytdlp.txt
    pip install -r /tmp/requirements_no_ytdlp.txt -q
    pip install "${ytdlp_bare}" -q
  else
    error "pip install failed — see above."
    cat /tmp/pip_err.log
    exit 1
  fi
fi
rm -f /tmp/pip_err.log /tmp/requirements_no_ytdlp.txt
success "Python packages installed"

# ── 6. Verify ─────────────────────────────────────────────────────────────
info "[6/9] Verifying the install"
if command -v ffmpeg >/dev/null 2>&1; then
  if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libmp3lame; then
    success "ffmpeg has libmp3lame (needed to encode the MP3 stream)"
  else
    warn "ffmpeg is installed but its libmp3lame encoder wasn't found — /stream.mp3 will fail to"
    warn "start. This would be unusual for Termux's own ffmpeg package; try 'pkg reinstall ffmpeg'."
  fi
else
  error "ffmpeg not found after install — required. Try: pkg install ffmpeg"
  exit 1
fi
python - <<'PY'
def check(name, stmt):
    try:
        exec(stmt)
        print(f"  ✓ {name}")
    except Exception as e:
        print(f"  ✗ {name}: {e}")
check("twitchio", "import twitchio; print(f'    twitchio {twitchio.__version__}')")
check("yt-dlp", "import yt_dlp; print(f'    yt-dlp {yt_dlp.version.__version__}')")
check("aiohttp", "import aiohttp; print(f'    aiohttp {aiohttp.__version__}')")
PY

# ── 7. .env wizard ────────────────────────────────────────────────────────
info "[7/9] Configuring .env"
if [[ -f "${ENV_PATH}" ]]; then
  info "Found an existing ${ENV_PATH} — keeping it, only filling in anything still blank below."
  chmod 600 "${ENV_PATH}" 2>/dev/null || true
else
  cp "${APP_DIR}/deploy/.env.example" "${ENV_PATH}"
  chmod 600 "${ENV_PATH}"
  success "Wrote ${ENV_PATH} from the template."
fi

# Point yt-dlp at Node instead of its own Deno auto-detect default — see
# this script's header comment for why. Safe to (re)write every run: it's
# not something a user is expected to hand-edit on Termux specifically.
if [[ -n "${NODE_PATH}" ]]; then
  set_env_var "YTDLP_JS_RUNTIME_PATH" "${NODE_PATH}" "${ENV_PATH}"
  set_env_var "YTDLP_JS_RUNTIME_NAME" "node" "${ENV_PATH}"
fi

if [[ -z "$(missing_required_env)" ]]; then
  success "All required credentials are already set in ${ENV_PATH}."
elif [[ ! -t 0 || ! -t 1 ]]; then
  warn "Not running interactively — skipping the credential wizard. Fill in ${ENV_PATH} by hand."
elif [[ "${SKIP_WIZARD:-}" == "1" ]]; then
  info "SKIP_WIZARD=1 — skipping the credential wizard."
else
  echo ""
  echo "─────────────────────────────────────────────────────────────"
  echo " Credential setup — Enter to skip any of these and fill it in"
  echo " by hand later (${ENV_PATH}). The service just won't start"
  echo " until all four are set."
  echo "─────────────────────────────────────────────────────────────"

  prompt_env_field TWITCH_CLIENT_ID 0 0 \
    "1. Go to https://dev.twitch.tv/console/apps and log in." \
    "2. Click 'Register Your Application'." \
    "3. Name: anything unique to your account. Category: 'Chat Bot'." \
    "4. OAuth Redirect URLs — add exactly:  http://localhost:4343/oauth/callback" \
    "5. Client Type: 'Confidential'. Click Create." \
    "6. The Client ID is shown right on the app's page."

  prompt_env_field TWITCH_CLIENT_SECRET 1 0 \
    "On that same app page, click 'New Secret'." \
    "It's shown once — copy it now."

  prompt_env_field TWITCH_BOT_ID 0 1 \
    "The numeric Twitch user ID of the account the BOT chats as — not a" \
    "username. Recommended: a separate account, made a moderator in your" \
    "channel. Look up a username's numeric ID at:" \
    "  https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/"

  prompt_env_field TWITCH_OWNER_ID 0 1 \
    "The numeric Twitch user ID of YOUR (broadcaster) account — not a" \
    "username. Same lookup tool as above, your own username this time."

  echo ""
  echo "─────────────────────────────────────────────────────────────"
  echo " Optional settings — Enter accepts the default shown."
  echo "─────────────────────────────────────────────────────────────"

  echo ""
  echo "${CYAN}-- Chat & audio --${RESET}"
  prompt_optional_field TWITCH_PREFIX "!" 0 0 "— Command prefix in chat (!sr, !skip, ...)."
  prompt_optional_field AUDIO_BITRATE_KBPS "128" 0 1 \
    "— MP3 bitrate for /stream.mp3 (64-320). Raise it if it sounds thin."

  echo ""
  echo "${CYAN}-- Local HTTP surface (/stream.mp3, /overlay, /settings) --${RESET}"
  # Unlike the VPS wizard, this doesn't ask "does OBS run on this machine?"
  # — OBS Studio doesn't run on Android, so on Termux the answer is always
  # "no", every time. This always configures for a separate OBS machine.
  echo "OBS can't run on this phone, so this always sets up for a separate"
  echo "OBS machine reaching this phone over the network — same Wi-Fi is the"
  echo "realistic case (see this script's final summary for the exact URL)."
  if [[ -n "$(get_env_var "TWITCH_NOWPLAYING_HOST" "${ENV_PATH}")" ]]; then
    info "TWITCH_NOWPLAYING_HOST is already set — leaving it alone."
  else
    set_env_var "TWITCH_NOWPLAYING_HOST" "0.0.0.0" "${ENV_PATH}"
    success "TWITCH_NOWPLAYING_HOST = 0.0.0.0"
  fi
  prompt_optional_field TWITCH_NOWPLAYING_PORT "8098" 0 1 \
    "— Port for the local HTTP surface (1024-65535)."

  echo ""
  echo "  A settings password is required here — this surface is reachable"
  echo "  off this phone, and /settings changes your queue/cooldown limits."
  echo ""
  echo "${CYAN}TWITCH_SETTINGS_PASSWORD${RESET}"
  if [[ -n "$(get_env_var "TWITCH_SETTINGS_PASSWORD" "${ENV_PATH}")" ]]; then
    info "TWITCH_SETTINGS_PASSWORD is already set — leaving it alone."
  else
    pw=""
    read -r -s -p "  Value (input hidden, Enter to auto-generate one): " pw || true
    echo ""
    pw="$(trim "${pw}")"
    if [[ -z "${pw}" ]]; then
      pw="$(openssl rand -hex 12 2>/dev/null || head -c16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c20)"
      echo "  Generated: ${pw}  (also saved in .env, so you don't need to remember it)"
    fi
    set_env_var "TWITCH_SETTINGS_PASSWORD" "${pw}" "${ENV_PATH}"
    success "TWITCH_SETTINGS_PASSWORD saved."
  fi

  echo ""
  echo "${CYAN}-- yt-dlp --${RESET}"
  prompt_optional_field YTDLP_COOKIES_FILE "" 0 0 \
    "— Leave blank on home Wi-Fi. Only likely needed if requests get blocked" \
    "    ('Sign in to confirm you're not a bot') — see README. Must be a" \
    "    path under data/, e.g. data/cookies.txt."
  prompt_optional_field YTDLP_PLAYER_CLIENT "" 0 0 \
    "— Advanced: comma-separated YouTube player_client override. Leave" \
    "    blank for the built-in default."
  prompt_optional_field YTDLP_CACHE_TTL_SECONDS "300" 0 1 \
    "— Seconds a resolved song is reused instead of re-running yt-dlp (0-3600)."
  prompt_optional_field YTDLP_CONCURRENCY "2" 0 1 \
    "— Concurrent yt-dlp extractions (1-4). Lower this on an older/slower phone."
  prompt_optional_field YTDLP_EXTRACT_TIMEOUT_SECONDS "45" 0 1 \
    "— Seconds before giving up on a single resolve (10-120)."

  echo ""
  echo "${CYAN}-- Logging --${RESET}"
  prompt_optional_field LOG_LEVEL "INFO" 0 0 "— DEBUG/INFO/WARNING/ERROR/CRITICAL."
  prompt_optional_field LOG_TO_FILE "true" 0 0 \
    "— Also write logs/twitch-radio.log. No automatic rotation on Termux" \
    "    (that's setup.sh's logrotate cron job, which Termux has no cron for" \
    "    by default) — the file just grows; clear it by hand occasionally, or" \
    "    set this to false and rely on the tmux/termux-services log instead."
fi

# ── 8. Auto-start ─────────────────────────────────────────────────────────
info "[8/9] Setting up auto-start"
echo "Termux has no systemd. Two options:"
echo "  1) termux-services (runit) — supervises the bot, restarts it on crash."
echo "  2) A detached tmux session — simpler, no auto-restart."
echo ""
read -rp "Set up termux-services auto-supervision? [Y/n] " use_services
USE_SERVICES=true
[[ "${use_services}" =~ ^[Nn]$ ]] && USE_SERVICES=false
BOT_STARTED=false

if [[ "${USE_SERVICES}" == true ]]; then
  pkg install -y termux-services 2>/dev/null || warn "pkg install termux-services failed — falling back to tmux"
  SV_DIR="${PREFIX:-/data/data/com.termux/files/usr}/var/service/${SERVICE_NAME}"
  if command -v sv-enable >/dev/null 2>&1; then
    mkdir -p "${SV_DIR}"
    cat > "${SV_DIR}/run" << RUNEOF
#!/data/data/com.termux/files/usr/bin/bash
cd "${APP_DIR}"
source "${VENV_DIR}/bin/activate"
set -a; source "${ENV_PATH}"; set +a
exec python bot.py
RUNEOF
    chmod +x "${SV_DIR}/run"
    success "Service file written: ${SV_DIR}/run"
    if ! pgrep -f runsvdir >/dev/null 2>&1; then
      runsvdir "${PREFIX:-/data/data/com.termux/files/usr}/var/service" >/dev/null 2>&1 &
      disown 2>/dev/null || true
      sleep 1
    fi
    if sv-enable "${SERVICE_NAME}" >/dev/null 2>&1 && sleep 2 && sv status "${SERVICE_NAME}" 2>/dev/null | grep -q '^run:'; then
      success "${SERVICE_NAME} service is running under termux-services."
      BOT_STARTED=true
    else
      warn "Service is configured but isn't confirmed running yet."
      warn "Close and reopen Termux once, then run: sv-enable ${SERVICE_NAME}"
      warn "Check status any time with: sv status ${SERVICE_NAME}"
    fi
  else
    warn "termux-services didn't install cleanly — falling back to tmux."
    USE_SERVICES=false
  fi
fi

if [[ "${BOT_STARTED}" == false ]]; then
  info "Starting the bot in a detached tmux session instead"
  command -v tmux >/dev/null 2>&1 || pkg install -y tmux
  tmux kill-session -t "${SERVICE_NAME}" 2>/dev/null || true
  tmux new-session -d -s "${SERVICE_NAME}" \
    "cd '${APP_DIR}' && source '${VENV_DIR}/bin/activate' && exec python bot.py"
  sleep 2
  if tmux has-session -t "${SERVICE_NAME}" 2>/dev/null; then
    success "Bot started in tmux session '${SERVICE_NAME}'."
    BOT_STARTED=true
  else
    warn "Couldn't confirm the tmux session started. Start it manually:"
    warn "  cd ${APP_DIR} && source .venv/bin/activate && python bot.py"
  fi
fi

# ── 9. Done ────────────────────────────────────────────────────────────
info "[9/9] Setup finished"
LOCAL_IP="$(python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
except Exception:
    print('')
finally:
    s.close()
" 2>/dev/null || echo "")"
PORT="$(get_env_var "TWITCH_NOWPLAYING_PORT" "${ENV_PATH}")"
PORT="${PORT:-8098}"

echo ""
echo "${BOLD}Setup complete.${RESET}"
still_missing="$(missing_required_env)"
if [[ -n "${still_missing}" ]]; then
  echo ""
  echo "Still need to fill in, in ${ENV_PATH}:"
  while IFS= read -r key; do echo "  - ${key}"; done <<<"${still_missing}"
  echo "(or re-run this script interactively to pick up where you left off)"
fi

echo ""
echo "─────────────────────────────────────────────────────────────"
echo "Next: one-time Twitch OAuth authorization — the bot can't chat or read"
echo "chat until this is done."
echo ""
echo "Since this runs ON the phone, no SSH tunnel is needed (unlike the VPS"
echo "instructions) — just open these two URLs directly in a browser app on"
echo "THIS phone, in two SEPARATE browser sessions (e.g. Chrome + a private/"
echo "incognito tab) so each authorizes the right account:"
echo ""
echo "  As the BOT account:"
echo "    http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true"
echo "  As the BROADCASTER account:"
echo "    http://localhost:4343/oauth?scopes=channel:bot&force_verify=true"
echo "─────────────────────────────────────────────────────────────"
echo ""
if [[ -n "${LOCAL_IP}" ]]; then
  echo "In OBS, on a computer on the SAME Wi-Fi network as this phone:"
  echo "  Media Source   -> http://${LOCAL_IP}:${PORT}/stream.mp3"
  echo "  Browser Source -> http://${LOCAL_IP}:${PORT}/overlay"
  echo ""
  echo "This IP can change if the phone reconnects to Wi-Fi — re-run this"
  echo "script (or check 'ip addr' / Termux:API's termux-wifi-connectioninfo)"
  echo "if OBS stops connecting."
else
  echo "Couldn't detect this phone's local IP automatically. Find it in"
  echo "Android's Wi-Fi settings, then point OBS at http://<that-ip>:${PORT}/stream.mp3"
fi
echo ""
echo "Reaching it from OUTSIDE that Wi-Fi network (e.g. a cloud OBS box, or"
echo "cellular data instead of Wi-Fi) needs its own solution — most mobile"
echo "carriers block inbound connections outright (CGNAT), so port-forwarding"
echo "the way the VPS README describes usually isn't possible here. A"
echo "tunneling tool (e.g. Tailscale, or Cloudflare Tunnel) is the realistic"
echo "option if that's what you need."
echo ""
echo "${BOLD}Useful commands:${RESET}"
if [[ "${USE_SERVICES}" == true ]]; then
  echo "  sv status ${SERVICE_NAME}   — check it's running"
  echo "  sv down/up ${SERVICE_NAME}  — stop / start it"
else
  echo "  tmux attach -t ${SERVICE_NAME}   — view the running bot / logs (Ctrl+B, D to detach)"
fi
echo "  tail -f ${APP_DIR}/logs/*.log   — follow the log file (if LOG_TO_FILE=true)"
echo ""
echo "Optional: install Termux:Boot (F-Droid) to start Termux automatically on"
echo "phone reboot, and Termux:API (F-Droid) + 'pkg install termux-api', then"
echo "'termux-wake-lock', so Android doesn't kill the session in the background."
echo ""
