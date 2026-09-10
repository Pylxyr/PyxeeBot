#!/usr/bin/env bash
# Installs the Twitch radio service on the same box as (or separate from) the
# Discord bot. Does NOT touch the Discord bot's files, venv, or service.
set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; CYAN=$'\033[0;36m'; RESET=$'\033[0m'
info()    { echo "${CYAN}==>${RESET} $*"; }
success() { echo "${GREEN}✓${RESET} $*"; }
warn()    { echo "${YELLOW}!${RESET} $*"; }
error()   { echo "${RED}✗${RESET} $*"; }

# ---- .env helpers -----------------------------------------------------
# Deliberately NOT sed-based: a pasted Client Secret or Stream Key can
# contain '/', '&', or '\', any of which corrupts (or silently
# mis-substitutes) a sed replacement. Plain string comparison + printf
# sidesteps that entirely — safe with any value that doesn't itself
# contain a literal newline, which a single `read` line never will.

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

missing_required_env() {  # prints each still-blank required key, one per line
  local key
  for key in "${REQUIRED_ENV_KEYS[@]}"; do
    if [[ -z "$(get_env_var "${key}" "${ENV_PATH}")" ]]; then
      echo "${key}"
    fi
  done
}

prompt_env_field() {
  # prompt_env_field KEY SECRET(0/1) NUMERIC(0/1) instruction-lines...
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
  # `|| true` on each read: under set -e, Ctrl+D/EOF mid-prompt would
  # otherwise abort the whole install partway through (packages and venv
  # already installed by this point) instead of just skipping this field.
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

prompt_optional_field() {
  # prompt_optional_field KEY DEFAULT SECRET(0/1) NUMERIC(0/1) description-line...
  # Unlike prompt_env_field, Enter here means "keep the default" (written
  # explicitly into .env), not "leave blank" — every one of these already
  # has a sane default the service runs fine with.
  local key="$1" default="$2" secret="$3" numeric="$4"
  shift 4
  if [[ -n "$(get_env_var "${key}" "${ENV_PATH}")" ]]; then
    info "${key} is already set — leaving it alone."
    return
  fi
  local shown_default="${default}"
  if [[ -z "${shown_default}" ]]; then
    shown_default="disabled"
  fi
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
  if [[ -z "${value}" ]]; then
    return  # blank default (e.g. no settings password) — nothing to write, absence IS the default
  fi
  set_env_var "${key}" "${value}" "${ENV_PATH}"
  success "${key} = ${value}"
}

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$(whoami)}"
SERVICE_NAME="twitch-radio"
ENV_PATH="${APP_DIR}/.env"

echo "Twitch Radio Bot — setup"
echo "App directory: ${APP_DIR}"
echo "Service user:  ${SERVICE_USER}"
echo ""

# SUDO_USER is only set when this was actually invoked via `sudo`. Run as a
# root shell directly (common on minimal VPS/container images) and whoami
# falls back to "root" silently — installing a systemd unit that runs
# ffmpeg and the yt-dlp JS runtime, both consuming untrusted URLs from chat,
# as User=root. Require an explicit opt-in for that instead of guessing.
if [[ "${SERVICE_USER}" == "root" && -z "${SUDO_USER:-}" ]]; then
  if [[ "${ALLOW_ROOT:-}" != "1" ]]; then
    error "Running as root with no SUDO_USER — this would install the service as User=root."
    warn  "Run this via 'sudo' as a normal user instead, so the service runs as that user."
    warn  "If running as root is actually intended (e.g. a container), re-run with:"
    warn  "    ALLOW_ROOT=1 ${BASH_SOURCE[0]}"
    exit 1
  fi
  warn "Proceeding as root (ALLOW_ROOT=1) — the service will run as User=root."
fi

echo "[1/7] Installing system packages"
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg logrotate curl unzip openssl

echo "[2/7] Installing a JS runtime for yt-dlp (Deno)"
# yt-dlp needs an external JS runtime to solve YouTube's JS challenges as of
# the version pinned in requirements.txt. Installed system-wide to
# /usr/local/bin so it's on PATH for the systemd unit too (that unit sets an
# explicit PATH that doesn't include a per-user ~/.deno/bin).
if command -v deno >/dev/null 2>&1; then
  info "Deno already installed ($(deno --version | head -n1)) — skipping."
else
  # install.sh takes zero flags — it's already fully non-interactive. Its
  # only optional argument is a specific version tag; passing anything else
  # gets treated as that tag and 404s.
  if curl -fsSL https://deno.land/install.sh | sudo DENO_INSTALL=/usr/local sh >/dev/null 2>&1; then
    success "Deno installed to /usr/local/bin."
  else
    warn "Deno install failed — yt-dlp will fall back to degraded YouTube support." \
         "Install manually later: https://docs.deno.com/runtime/getting_started/installation/"
  fi
fi

echo "[3/7] Preparing app directories"
mkdir -p "${APP_DIR}/data" "${APP_DIR}/logs" "${APP_DIR}/data/deno-cache"

echo "[4/7] Creating virtual environment"
if [[ ! -d "${APP_DIR}/.venv" ]]; then
  python3 -m venv "${APP_DIR}/.venv"
fi

echo "[5/7] Installing Python dependencies"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip -q
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -q

echo "[6/7] Environment file"
if [[ -f "${ENV_PATH}" ]]; then
  info "Found an existing ${ENV_PATH} — keeping it, only filling in anything still blank below."
  chmod 600 "${ENV_PATH}" 2>/dev/null || true
else
  cp "${APP_DIR}/deploy/.env.example" "${ENV_PATH}"
  chmod 600 "${ENV_PATH}"
  success "Wrote ${ENV_PATH} from the template."
fi

if [[ -z "$(missing_required_env)" ]]; then
  success "All required credentials are already set in ${ENV_PATH}."
elif [[ ! -t 0 || ! -t 1 ]]; then
  warn "Not running interactively (no TTY) — skipping the credential wizard."
  warn "Fill in ${ENV_PATH} by hand before starting the service; see the README for where each value comes from."
elif [[ "${SKIP_WIZARD:-}" == "1" ]]; then
  info "SKIP_WIZARD=1 — skipping the credential wizard."
else
  echo ""
  echo "─────────────────────────────────────────────────────────────"
  echo " Credential setup — Enter to skip any of these and fill it in"
  echo " by hand later (${ENV_PATH}). The service just won't start"
  echo " until all four are set."
  echo "─────────────────────────────────────────────────────────────"
  # Note: if the connection this is running over (e.g. an SSH session) dies
  # entirely mid-prompt — not a plain Ctrl+D, the whole pty going away — bash's
  # own terminal-attribute handling for `read -s` can abort the script outright
  # in a way that isn't a normal command failure `|| true` catches, and isn't
  # a signal a `trap` catches either. If that happens: nothing already
  # installed is harmed, and re-running this script picks up exactly where it
  # left off (already-set values are kept, only blanks get re-prompted).

  prompt_env_field TWITCH_CLIENT_ID 0 0 \
    "1. Go to https://dev.twitch.tv/console/apps and log in." \
    "2. Click 'Register Your Application'." \
    "3. Name: anything unique to your account. Category: 'Chat Bot'." \
    "4. OAuth Redirect URLs — add exactly:  http://localhost:4343/oauth/callback" \
    "5. Client Type: 'Confidential'. Click Create." \
    "6. The Client ID is shown right on the app's page."

  prompt_env_field TWITCH_CLIENT_SECRET 1 0 \
    "On that same app page, click 'New Secret'." \
    "It's shown once — copy it now. (Lost it? Generate a new one any time;" \
    "the old one just stops working.)"

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
  echo " Optional settings — every one below already has a working"
  echo " default. Enter accepts it; only type something if you want"
  echo " to change it. All of these can also be edited by hand later"
  echo " in ${ENV_PATH} (or live, for the four under /settings)."
  echo "─────────────────────────────────────────────────────────────"

  echo ""
  echo "${CYAN}-- Chat & audio --${RESET}"
  prompt_optional_field TWITCH_PREFIX "!" 0 0 \
    "— Command prefix in chat (!sr, !skip, ...)."
  prompt_optional_field AUDIO_BITRATE_KBPS "128" 0 1 \
    "— MP3 bitrate for /stream.mp3 (64-320). Raise it if it sounds thin."

  echo ""
  echo "${CYAN}-- Local HTTP surface (/stream.mp3, /overlay, /settings) --${RESET}"
  if [[ -n "$(get_env_var "TWITCH_NOWPLAYING_HOST" "${ENV_PATH}")" ]]; then
    info "TWITCH_NOWPLAYING_HOST is already set — leaving it alone."
    prompt_optional_field TWITCH_NOWPLAYING_PORT "8098" 0 1 \
      "— Port for the local HTTP surface (1024-65535)."
    prompt_optional_field TWITCH_SETTINGS_PASSWORD "" 1 0 \
      "— Basic Auth password for /settings. Strongly recommended if the host" \
      "    above isn't 127.0.0.1 — otherwise anyone who finds the port can" \
      "    change your queue/cooldown settings."
  else
    echo ""
    echo "${CYAN}TWITCH_NOWPLAYING_HOST${RESET}"
    echo "  This bot doesn't stream to Twitch itself — OBS pulls the audio"
    echo "  stream and overlay from here over plain HTTP instead."
    same_machine=""
    read -r -p "  Does OBS run on THIS machine? [Y/n]: " same_machine || true
    same_machine="$(trim "${same_machine}")"
    remote_obs=0
    if [[ "${same_machine}" =~ ^[Nn] ]]; then
      remote_obs=1
      set_env_var "TWITCH_NOWPLAYING_HOST" "0.0.0.0" "${ENV_PATH}"
      success "TWITCH_NOWPLAYING_HOST = 0.0.0.0 (reachable from other machines)"
    else
      set_env_var "TWITCH_NOWPLAYING_HOST" "127.0.0.1" "${ENV_PATH}"
      success "TWITCH_NOWPLAYING_HOST = 127.0.0.1"
    fi

    prompt_optional_field TWITCH_NOWPLAYING_PORT "8098" 0 1 \
      "— Port for the local HTTP surface (1024-65535)."

    if [[ "${remote_obs}" -eq 1 ]]; then
      echo ""
      echo "  A settings password is required when OBS is on another machine —"
      echo "  this surface is now reachable off this box, and /settings changes"
      echo "  your queue/cooldown limits."
      echo ""
      echo "${CYAN}TWITCH_SETTINGS_PASSWORD${RESET}"
      pw=""
      read -r -s -p "  Value (input hidden, Enter to auto-generate one): " pw || true
      echo ""
      pw="$(trim "${pw}")"
      if [[ -z "${pw}" ]]; then
        pw="$(openssl rand -hex 12 2>/dev/null || head -c16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c20)"
        echo "  Generated: ${pw}"
        echo "  (also saved in .env, so you don't need to remember it)"
      fi
      set_env_var "TWITCH_SETTINGS_PASSWORD" "${pw}" "${ENV_PATH}"
      success "TWITCH_SETTINGS_PASSWORD saved."

      port="$(get_env_var "TWITCH_NOWPLAYING_PORT" "${ENV_PATH}")"
      echo ""
      echo "  Detecting this machine's public IP..."
      public_ip=""
      for ip_svc in "https://ifconfig.me" "https://api.ipify.org" "https://icanhazip.com"; do
        public_ip="$(curl -fsS --max-time 4 "${ip_svc}" 2>/dev/null | tr -d '[:space:]' || true)"
        if [[ -n "${public_ip}" ]]; then
          break
        fi
      done
      if [[ -z "${public_ip}" ]]; then
        warn "Couldn't auto-detect it (no outbound network, or it timed out)."
        warn "Find it with: curl ifconfig.me — or your cloud provider's console."
        public_ip="<your-public-ip>"
      else
        success "Public IP: ${public_ip}"
      fi
      echo ""
      echo "─────────────────────────────────────────────────────────────"
      echo " Open port ${port} so OBS can reach it — in TWO places:"
      echo ""
      echo " 1. Your cloud provider's firewall/security rule (ingress, TCP,"
      echo "    port ${port}, source 0.0.0.0/0). On Oracle Cloud: the VCN's"
      echo "    Security List or NSG, in the OCI console."
      echo ""
      echo " 2. This VM's own OS firewall — commonly ALSO blocks it even"
      echo "    after step 1. On Oracle's Ubuntu images specifically:"
      echo "      sudo cp /etc/iptables/rules.v4 /etc/iptables/rules.v4.bak"
      echo "      sudo sed -i '/--dport 22 -j ACCEPT/a -A INPUT -p tcp -m state --state NEW -m tcp --dport ${port} -j ACCEPT' /etc/iptables/rules.v4"
      echo "      sudo iptables-restore < /etc/iptables/rules.v4"
      echo "      sudo netfilter-persistent save"
      echo "    Double-check the SSH (port 22) rule is still there afterward:"
      echo "      sudo iptables -L INPUT -n --line-numbers"
      echo ""
      echo " Then in OBS: http://${public_ip}:${port}/stream.mp3 and .../overlay"
      echo "─────────────────────────────────────────────────────────────"
    else
      prompt_optional_field TWITCH_SETTINGS_PASSWORD "" 1 0 \
        "— Basic Auth password for /settings. Only matters if you later" \
        "    change TWITCH_NOWPLAYING_HOST away from 127.0.0.1."
    fi
  fi

  echo ""
  echo "${CYAN}-- Advanced: state filenames (only for multiple instances) --${RESET}"
  prompt_optional_field TWITCH_TOKEN_FILE "twitch_tokens.json" 0 0 \
    "— Under data/. No reason to change unless running >1 instance from one data/."
  prompt_optional_field TWITCH_TUNABLES_FILE "tunables.json" 0 0 \
    "— Same as above, for the /settings tunables."

  echo ""
  echo "${CYAN}-- yt-dlp --${RESET}"
  prompt_optional_field YTDLP_COOKIES_FILE "" 0 0 \
    "— Leave blank on a residential connection. On a cloud VM, YouTube" \
    "    often blocks anonymous requests ('Sign in to confirm you're not a" \
    "    bot') and this is the simplest fix — a REAL cookies.txt from a" \
    "    logged-in browser session (empty/placeholder makes it worse). MUST" \
    "    be a path under data/ (e.g. data/cookies.txt) — anywhere else" \
    "    crashes every !sr (read-only fs under this service's sandbox)."
  prompt_optional_field YTDLP_POT_PROVIDER_URL "" 0 0 \
    "— Advanced, cloud-VM alternative to cookies: URL of a local bgutil-" \
    "    ytdlp-pot-provider instance (see README) if you've set one up." \
    "    Leave blank if you haven't — not required."
  prompt_optional_field YTDLP_JS_RUNTIME_PATH "" 0 0 \
    "— Advanced: pin a specific JS runtime binary. Leave blank to auto-detect" \
    "    the Deno install this script just did." \
    "    Switching YTDLP_JS_RUNTIME_NAME to node instead? It needs Node >=22 —" \
    "    Ubuntu's own apt nodejs package is almost always older than that." \
    "    Use https://github.com/nodesource/distributions or nvm, not apt."
  prompt_optional_field YTDLP_JS_RUNTIME_NAME "deno" 0 0 \
    "— Only matters if YTDLP_JS_RUNTIME_PATH above is set."
  prompt_optional_field YTDLP_PLAYER_CLIENT "" 0 0 \
    "— Advanced: comma-separated yt-dlp YouTube player_client override." \
    "    Leave blank for the built-in default (web_embedded,web when" \
    "    cookies are set, to dodge a currently-broken fallback client —" \
    "    see the README's yt-dlp note — otherwise yt-dlp's own default)." \
    "    YouTube changes what works here often; check" \
    "    https://github.com/yt-dlp/yt-dlp/wiki/EJS if requests start failing."
  prompt_optional_field YTDLP_CACHE_TTL_SECONDS "300" 0 1 \
    "— Seconds a resolved song is reused instead of re-running yt-dlp (0-3600)." \
    "    Cuts the usual double-extraction (once in chat, again right before" \
    "    it plays) for anything near the front of the queue. 0 disables it."
  prompt_optional_field YTDLP_CONCURRENCY "2" 0 1 \
    "— Concurrent yt-dlp extractions (1-4). Raise if !sr gets busy."
  prompt_optional_field YTDLP_EXTRACT_TIMEOUT_SECONDS "45" 0 1 \
    "— Seconds before giving up on a single resolve (10-120)."

  echo ""
  echo "${CYAN}-- Logging --${RESET}"
  prompt_optional_field LOG_LEVEL "INFO" 0 0 \
    "— DEBUG/INFO/WARNING/ERROR/CRITICAL."
  prompt_optional_field LOG_TO_FILE "true" 0 0 \
    "— Also write logs/twitch-radio.log (rotated weekly) alongside journalctl."
  echo ""
fi

echo "[7/7] Installing logrotate config and systemd unit"
# Template the path/user instead of installing verbatim — otherwise a custom
# APP_DIR/SERVICE_USER silently doesn't take effect here even though the
# rest of the install honors it.
sed -e "s#/home/ubuntu/twitch-radio-bot#${APP_DIR}#g" \
    -e "s#su ubuntu ubuntu#su ${SERVICE_USER} ${SERVICE_USER}#" \
    "${APP_DIR}/deploy/${SERVICE_NAME}-logrotate" | sudo tee "/etc/logrotate.d/${SERVICE_NAME}" >/dev/null
sed \
  -e "s#/home/ubuntu/twitch-radio-bot#${APP_DIR}#g" \
  -e "s#User=ubuntu#User=${SERVICE_USER}#" \
  "${APP_DIR}/deploy/${SERVICE_NAME}.service" | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null
sudo systemctl daemon-reload
success "Installed /etc/systemd/system/${SERVICE_NAME}.service (not started yet)."

echo ""
echo "─────────────────────────────────────────────────────────────"
echo "Next steps:"
echo ""
still_missing="$(missing_required_env)"
if [[ -n "${still_missing}" ]]; then
  echo "1. Still need to fill in, in ${ENV_PATH}:"
  while IFS= read -r key; do
    echo "     - ${key}"
  done <<<"${still_missing}"
  echo "   (see the comments in the file, or the README, for where each comes from —"
  echo "   or just re-run this script interactively to pick up where you left off.)"
else
  echo "1. All required credentials are set in ${ENV_PATH}."
  echo "   If OBS is on a different machine than this one, double-check"
  echo "   TWITCH_NOWPLAYING_HOST and TWITCH_SETTINGS_PASSWORD in the same file."
fi
echo ""
echo "2. Start the service:"
echo "     sudo systemctl enable --now ${SERVICE_NAME}"
echo ""
echo "3. Complete the one-time OAuth authorization — the service can't chat"
echo "   or read chat until this is done. Full walkthrough in README.md, or"
echo "   the module docstring in twitch_radio/chatbot.py. Short version:"
echo "     ssh -L 4343:localhost:4343 ${SERVICE_USER}@<this-host>"
echo "   then, in a browser:"
echo "     - as the BOT account:"
echo "       http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true"
echo "     - as the BROADCASTER account:"
echo "       http://localhost:4343/oauth?scopes=channel:bot&force_verify=true"
echo "   Use two SEPARATE browser sessions for these two — reusing one"
echo "   logged-in session for both silently authorizes the same account"
echo "   twice. Watch step 5's logs right after starting to catch that."
echo ""
echo "4. In OBS, add a Media Source pointed at /stream.mp3 and (optionally) a"
echo "   Browser Source pointed at /overlay — see README.md."
echo ""
echo "5. journalctl -u ${SERVICE_NAME} -f -o cat    — watch it come up"
echo "─────────────────────────────────────────────────────────────"
