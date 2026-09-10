#!/data/data/com.termux/files/usr/bin/bash
# PyxeeBot — Termux (Android) installer
#
# Standalone counterpart to deploy/_common.sh (which drives setup.sh /
# setup_oracle.sh / setup_gcp.sh for VPS hosts). Termux has no systemd, no
# apt, no sudo, and needs Rust-toolchain workarounds the VPS path never
# touches, so it isn't a good fit for that shared engine — this script
# re-implements the same interactive wizard (Discord token + Last.fm key,
# both live-validated) on top of a Termux-specific install routine.
#
# Known Termux-specific fixes baked in below:
#   - ANDROID_API_LEVEL + CARGO_BUILD_TARGET exported before any pip install
#     that touches Rust — without these, maturin fails outright with
#     "Failed to determine Android API level" when building davey.
#   - CARGO_BUILD_TARGET is derived from `uname -m` instead of hardcoded, so
#     this also works on 32-bit ARM / x86_64 Termux (e.g. some emulators),
#     not just the common aarch64 case.
#   - SODIUM_INSTALL=system + --no-binary for PyNaCl, so it links against the
#     libsodium installed via `pkg` instead of trying (and failing) to build
#     its own libsodium from source.
#   - Deno installed via `pkg` — yt-dlp needs an external JS runtime for
#     YouTube; same reasoning as deploy/_common.sh's Deno step.
#   - No systemd here, so instead of a unit file this offers to set up a
#     termux-services (runit) service for real auto-supervision, falling
#     back to a detached tmux session so the bot is actually running by the
#     time the script exits either way.
#
# Usage (from repo root):
#   bash deploy/setup_termux.sh

set -euo pipefail

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi

info()    { echo "${CYAN}==>${RESET} $*"; }
success() { echo "${GREEN}✓${RESET} $*"; }
warn()    { echo "${YELLOW}!${RESET} $*"; }
error()   { echo "${RED}✗${RESET} $*"; }

# ── Guard: Termux only ─────────────────────────────────────────────────
if [[ -z "${TERMUX_VERSION:-}" && ! -d /data/data/com.termux ]]; then
  error "This script is only for Termux on Android."
  error "For a Ubuntu/Debian VPS use: bash deploy/setup.sh (or setup_oracle.sh / setup_gcp.sh)"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
ENV_PATH="${APP_DIR}/.env"
VENV_DIR="${APP_DIR}/.venv"
REQ_FILE="${APP_DIR}/requirements.txt"
PYTHON_BIN="python"

cd "${APP_DIR}"

if [[ ! -f "${REQ_FILE}" || ! -f "${APP_DIR}/bot.py" ]]; then
  error "Could not find requirements.txt or bot.py inside ${APP_DIR}."
  error "Make sure you cloned the repo and are running this from its root:"
  echo "  git clone https://github.com/Pylxyr/PyxeeBot.git ~/musicbot"
  echo "  cd ~/musicbot && bash deploy/setup_termux.sh"
  exit 1
fi

echo ""
echo "${BOLD}PyxeeBot — Termux (Android) setup${RESET}"
echo "Installs system packages, a venv, the Discord voice stack (PyNaCl + davey),"
echo "and walks you through .env. This runs unattended once you answer a few"
echo "questions up front."
echo ""

# ── curl (needed to verify the Discord token / Last.fm key later) ──────
if ! command -v curl >/dev/null 2>&1; then
  info "Installing curl (needed to verify your Discord token and Last.fm key)"
  pkg install -y curl
fi

# ── 1. System packages ──────────────────────────────────────────────────
info "[1/10] Installing Termux packages"
pkg update -y
pkg install -y \
  python \
  ffmpeg \
  git \
  curl \
  libopus \
  libsodium \
  clang \
  make \
  binutils \
  libffi \
  openssl \
  rust \
  2>/dev/null || true

if ! command -v deno >/dev/null 2>&1; then
  info "Installing Deno (yt-dlp needs a JS runtime for modern YouTube support)"
  pkg install -y deno 2>/dev/null || warn "pkg install deno failed — install it later with: pkg install deno"
fi
success "System packages ready"

# ── 2. Architecture + Android API level ─────────────────────────────────
info "[2/10] Detecting device architecture and Android API level"

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64)   CARGO_TARGET_DEFAULT="aarch64-linux-android" ;;
  armv7l|armv8l)   CARGO_TARGET_DEFAULT="armv7-linux-androideabi" ;;
  i686|i386)       CARGO_TARGET_DEFAULT="i686-linux-android" ;;
  x86_64)          CARGO_TARGET_DEFAULT="x86_64-linux-android" ;;
  *)
    CARGO_TARGET_DEFAULT="aarch64-linux-android"
    warn "Unrecognised architecture '${ARCH}' — defaulting to aarch64-linux-android."
    warn "Most phones are aarch64; if the build fails, set CARGO_BUILD_TARGET yourself and re-run."
    ;;
esac
export CARGO_BUILD_TARGET="${CARGO_BUILD_TARGET:-$CARGO_TARGET_DEFAULT}"

# maturin needs to be told which Android API level to build against, or it
# fails outright with "Failed to determine Android API level" before it ever
# gets to compiling anything. This is a build-toolchain setting, not a
# requirement that your device run that exact OS version — 34 is the floor
# known to work for davey/PyNaCl on Termux; if the device reports higher,
# that's used instead (also fine — these builds are forward-compatible).
DEVICE_API_LEVEL="$(getprop ro.build.version.sdk 2>/dev/null || echo '')"
if [[ "$DEVICE_API_LEVEL" =~ ^[0-9]+$ ]] && (( DEVICE_API_LEVEL > 34 )); then
  API_LEVEL_DEFAULT="$DEVICE_API_LEVEL"
else
  API_LEVEL_DEFAULT=34
fi
export ANDROID_API_LEVEL="${ANDROID_API_LEVEL:-$API_LEVEL_DEFAULT}"
export SODIUM_INSTALL=system

info "Architecture:        ${ARCH}"
info "CARGO_BUILD_TARGET:  ${CARGO_BUILD_TARGET}"
info "ANDROID_API_LEVEL:   ${ANDROID_API_LEVEL} (device reports API ${DEVICE_API_LEVEL:-unknown})"
info "SODIUM_INSTALL:      ${SODIUM_INSTALL}"

# ── 3. Python version ────────────────────────────────────────────────────
info "[3/10] Checking Python (3.11+ required, matches the project's own requirement)"
if ! $PYTHON_BIN -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  error "Python 3.11+ is required. Termux's 'python' package should already satisfy this —"
  error "try 'pkg upgrade python' if the check above failed."
  exit 1
fi
success "Python $($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"

# ── 4. Virtual environment ───────────────────────────────────────────────
info "[4/10] Creating virtual environment"
if [[ ! -d "${VENV_DIR}" ]]; then
  $PYTHON_BIN -m venv "${VENV_DIR}"
fi
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip setuptools wheel
success "venv ready at ${VENV_DIR}"

# ── 5. Rust build tooling (needed for davey / PyNaCl from source) ───────
info "[5/10] Installing maturin (needed to build davey)"
pip install maturin || warn "maturin install had issues — davey may fail to build later"

# ── 6. Python dependencies (voice stack included) ───────────────────────
info "[6/10] Installing Python dependencies"

get_spec() { grep -E "^${1}(\[|==)" "${REQ_FILE}" | head -n1; }
DISCORDPY_SPEC="$(get_spec 'discord\.py')"
YTDLP_SPEC="$(get_spec 'yt-dlp')"
DOTENV_SPEC="$(get_spec 'python-dotenv')"
AIOSQLITE_SPEC="$(get_spec 'aiosqlite')"
DISCORDPY_BARE="discord.py==${DISCORDPY_SPEC##*==}"
YTDLP_BARE="yt-dlp==${YTDLP_SPEC##*==}"

# Versions come from requirements.txt itself (not hardcoded here) so this
# script can't silently drift out of sync if those pins are ever bumped.

pip install "${DOTENV_SPEC}" "${AIOSQLITE_SPEC}" || true

info "Installing ${YTDLP_SPEC}..."
if ! pip install "${YTDLP_SPEC}" --no-build-isolation 2>/dev/null; then
  warn "yt-dlp with its full extras failed to build (likely curl-cffi) — retrying without extras"
  pip install "${YTDLP_BARE}" || warn "Even a bare yt-dlp install failed — check the output above"
fi

info "Installing ${DISCORDPY_SPEC} (this can take a while on first run)..."
if ! pip install "${DISCORDPY_SPEC}" --no-build-isolation; then
  warn "Full discord.py[voice] failed — trying stepwise approach"

  pip install "${DISCORDPY_BARE}" || true

  info "Installing PyNaCl with SODIUM_INSTALL=system + --no-binary..."
  if ! pip install --no-binary=PyNaCl --force-reinstall --no-cache-dir "PyNaCl>=1.5.0,<1.6"; then
    warn "PyNaCl 1.5.x failed — trying 1.4.0 as a last resort"
    pip install --no-binary=PyNaCl --force-reinstall --no-cache-dir "PyNaCl==1.4.0" || true
  fi

  info "Installing davey..."
  pip install davey --no-build-isolation || warn "davey failed to build — basic voice may still work without full DAVE"
fi
success "Python package stage finished"

# ── 7. Verify ─────────────────────────────────────────────────────────────
info "[7/10] Verifying critical imports"
python - <<'PY'
import sys
print(f"Python {sys.version.split()[0]}")

def check(name, import_stmt):
    try:
        exec(import_stmt)
        print(f"  ✓ {name}")
        return True
    except Exception as e:
        print(f"  ✗ {name}: {e}")
        return False

ok = True
ok &= check("discord.py", "import discord; print(f'    discord.py {discord.__version__}')")
ok &= check("yt-dlp", "import yt_dlp; print(f'    yt-dlp {yt_dlp.version.__version__}')")
ok &= check("PyNaCl (SecretBox)", "from nacl.secret import SecretBox")
try:
    from nacl.secret import Aead
    print("  ✓ PyNaCl Aead available")
except Exception:
    print("  ! PyNaCl Aead missing (older build — some DAVE features may be limited)")
check("davey (optional)", "import davey")
print()
if not ok:
    print("Some core packages failed to import. The bot may still start but voice could be broken.")
PY

# ── 8. Configure .env ────────────────────────────────────────────────────
info "[8/10] Configuring .env"

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

DENO_PATH="$(command -v deno 2>/dev/null || echo "${PREFIX:-/data/data/com.termux/files/usr}/bin/deno")"

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
    error "Discord token, etc. — but none is attached to stdin. Run this script"
    error "directly in a Termux session, not piped through another command."
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

  {
    printf '# Generated by deploy/setup_termux.sh — see deploy/.env.example for every option.\n\n'
    printf '# Required\n'
    printf 'DISCORD_TOKEN=%s\n' "${DISCORD_TOKEN_VALUE}"
    printf 'BOT_OWNERS=%s\n' "${BOT_OWNERS_VALUE}"
    printf '\n'
    printf '# Command prefix (default: !) — servers can override with !setprefix\n'
    printf 'DEFAULT_PREFIX=%s\n' "${DEFAULT_PREFIX_VALUE}"
    printf '\n'
    printf '# yt-dlp needs an external JS runtime for YouTube — points at the Deno\n'
    printf '# binary this script installed via pkg.\n'
    printf 'YTDLP_JS_RUNTIME_PATH=%s\n' "${DENO_PATH}"
    printf '\n'
    printf '# Last.fm API key — enables !vibe / !vibe-load and the per-server !autoplay toggle\n'
    printf 'LASTFM_API_KEY=%s\n' "${LASTFM_API_KEY_VALUE}"
  } > "${ENV_PATH}"
  success "Wrote ${ENV_PATH}"
else
  # Keeping an existing .env — still make sure YTDLP_JS_RUNTIME_PATH points at
  # a real Deno binary, since its path can change across Termux reinstalls.
  if grep -q "^YTDLP_JS_RUNTIME_PATH=" "${ENV_PATH}" 2>/dev/null; then
    sed -i "s|^YTDLP_JS_RUNTIME_PATH=.*|YTDLP_JS_RUNTIME_PATH=${DENO_PATH}|" "${ENV_PATH}"
  else
    echo "YTDLP_JS_RUNTIME_PATH=${DENO_PATH}" >> "${ENV_PATH}"
  fi
  # Recover the client ID for the invite link at the end, if we can.
  if EXISTING_TOKEN="$(grep '^DISCORD_TOKEN=' "${ENV_PATH}" | head -n1 | cut -d= -f2-)" && [[ -n "$EXISTING_TOKEN" ]]; then
    fetch_bot_identity "$EXISTING_TOKEN" || true
  fi
fi

# ── 9. Auto-start ─────────────────────────────────────────────────────────
info "[9/10] Setting up auto-start"
echo "Termux has no systemd. Two options:"
echo "  1) termux-services (runit) — supervises the bot, restarts it on crash,"
echo "     and can auto-start next time you open Termux."
echo "  2) A detached tmux session — simpler, but doesn't restart the bot on"
echo "     crash or survive a full Termux restart on its own."
echo ""
read -rp "Set up termux-services auto-supervision? [Y/n] " use_services
USE_SERVICES=true
[[ "$use_services" =~ ^[Nn]$ ]] && USE_SERVICES=false

BOT_STARTED=false

if [[ "$USE_SERVICES" == true ]]; then
  pkg install -y termux-services 2>/dev/null || warn "pkg install termux-services failed — falling back to tmux"
  SV_DIR="${PREFIX:-/data/data/com.termux/files/usr}/var/service/musicbot"
  if command -v sv-enable >/dev/null 2>&1; then
    mkdir -p "${SV_DIR}"
    cat > "${SV_DIR}/run" << RUNEOF
#!/data/data/com.termux/files/usr/bin/bash
cd "${APP_DIR}"
source "${VENV_DIR}/bin/activate"
exec python bot.py
RUNEOF
    chmod +x "${SV_DIR}/run"
    success "Service file written: ${SV_DIR}/run"

    # runsvdir (the runit supervisor daemon) is normally started by a hook
    # termux-services adds to your shell profile, which only takes effect in
    # a *new* shell — so on a first-ever install in this same session it may
    # not be running yet. Try to nudge it up now so we don't just tell the
    # user to "restart Termux" when it isn't necessary.
    if ! pgrep -f runsvdir >/dev/null 2>&1; then
      runsvdir "${PREFIX:-/data/data/com.termux/files/usr}/var/service" >/dev/null 2>&1 &
      disown 2>/dev/null || true
      sleep 1
    fi

    if sv-enable musicbot >/dev/null 2>&1 && sleep 2 && sv status musicbot 2>/dev/null | grep -q '^run:'; then
      success "musicbot service is running under termux-services."
      BOT_STARTED=true
    else
      warn "Service is configured but isn't confirmed running yet."
      warn "Close and reopen Termux once, then run: sv-enable musicbot"
      warn "Check status any time with: sv status musicbot"
      warn "Logs: sv-service musicbot   (or) cat ${APP_DIR}/logs/*.log"
    fi
  else
    warn "termux-services didn't install cleanly — falling back to tmux."
    USE_SERVICES=false
  fi
fi

if [[ "$BOT_STARTED" == false ]]; then
  info "Starting the bot in a detached tmux session instead"
  command -v tmux >/dev/null 2>&1 || pkg install -y tmux
  tmux kill-session -t musicbot 2>/dev/null || true
  tmux new-session -d -s musicbot \
    "cd '${APP_DIR}' && source '${VENV_DIR}/bin/activate' && exec python bot.py"
  sleep 2
  if tmux has-session -t musicbot 2>/dev/null; then
    success "Bot started in tmux session 'musicbot'."
    BOT_STARTED=true
  else
    warn "Couldn't confirm the tmux session started. Start it manually:"
    warn "  cd ${APP_DIR} && source .venv/bin/activate && python bot.py"
  fi
fi

# ── 10. Done ───────────────────────────────────────────────────────────────
info "[10/10] Setup finished"
echo ""
echo "${BOLD}Setup complete.${RESET}"

if [[ -n "$DISCORD_CLIENT_ID" ]]; then
  echo ""
  echo "Invite your bot to a server:"
  echo "  ${CYAN}https://discord.com/oauth2/authorize?client_id=${DISCORD_CLIENT_ID}&permissions=3230720&scope=bot%20applications.commands${RESET}"
  echo "  (View Channels, Send Messages, Embed Links, Read Message History, Connect, Speak)"
fi

echo ""
echo "${BOLD}Useful commands:${RESET}"
if [[ "$USE_SERVICES" == true ]]; then
  echo "  sv status musicbot         — check it's running"
  echo "  sv down musicbot           — stop it"
  echo "  sv up musicbot             — start it again"
  echo "  sv-disable musicbot        — stop auto-starting it"
else
  echo "  tmux attach -t musicbot    — view the running bot / logs"
  echo "  Detach again: Ctrl+B then D"
fi
echo "  tail -f ${APP_DIR}/logs/*.log   — follow the log file"
echo ""
echo "Optional: install Termux:Boot (F-Droid) so Termux — and termux-services"
echo "with it — starts automatically when your phone boots."
echo "Optional: install Termux:API (F-Droid) + 'pkg install termux-api', then"
echo "run 'termux-wake-lock' so Android doesn't kill Termux in the background."
echo ""
echo "In Discord: join a voice channel → !join → !play <query or URL>"
echo ""
