#!/data/data/com.termux/files/usr/bin/bash
# PyxeeBot — Termux (Android ARM) installer
#
# Incorporates the workarounds that were needed on real Termux:
#   - ANDROID_API_LEVEL + CARGO_BUILD_TARGET for maturin / davey
#   - SODIUM_INSTALL=system + --no-binary for PyNaCl
#   - Deno via Termux pkg
#   - No systemd (Termux has none) — gives tmux / termux-services guidance
#
# Usage (from repo root):
#   bash deploy/setup_termux.sh

set -euo pipefail

if [[ -t 1 ]]; then
  BOLD=\( '\033[1m'; GREEN= \)'\033[32m'; YELLOW=\( '\033[33m'; RED= \)'\033[31m'; CYAN=\( '\033[36m'; RESET= \)'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi

info()    { echo "\( {CYAN}==> \){RESET} $*"; }
success() { echo "\( {GREEN}✓ \){RESET} $*"; }
warn()    { echo "\( {YELLOW}! \){RESET} $*"; }
error()   { echo "\( {RED}✗ \){RESET} $*"; }

# ── Guard: Termux only ─────────────────────────────────────────────────
if [[ -z "${TERMUX_VERSION:-}" && ! -d /data/data/com.termux ]]; then
  error "This script is only for Termux on Android."
  error "For Ubuntu/Debian VPS use: bash deploy/setup.sh (or setup_oracle.sh / setup_gcp.sh)"
  exit 1
fi

SCRIPT_DIR="\( (cd " \)(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
ENV_PATH="${APP_DIR}/.env"
VENV_DIR="${APP_DIR}/.venv"

cd "${APP_DIR}"

echo ""
echo "\( {BOLD}PyxeeBot — Termux (Android ARM) setup \){RESET}"
echo "Installs system packages, venv, Discord voice stack (including davey),"
echo "and prepares .env. Uses the same flags that fixed builds on Termux."
echo ""

# ── 1. System packages ─────────────────────────────────────────────────
info "[1/8] Installing Termux packages"
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

# Deno — required by modern yt-dlp for YouTube
if ! command -v deno >/dev/null 2>&1; then
  info "Installing Deno"
  pkg install -y deno 2>/dev/null || warn "pkg install deno failed — install it later with: pkg install deno"
fi

success "System packages ready"

# ── 2. Python version ──────────────────────────────────────────────────
info "[2/8] Checking Python"
PYTHON_BIN=python
if ! $PYTHON_BIN -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  error "Python 3.10+ is required."
  exit 1
fi
success "Python $($PYTHON_BIN -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')"

# ── 3. Virtual environment ─────────────────────────────────────────────
info "[3/8] Creating virtual environment"
if [[ ! -d "${VENV_DIR}" ]]; then
  \( PYTHON_BIN -m venv " \){VENV_DIR}"
fi
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip setuptools wheel
success "venv ready"

# ── 4. Build environment for maturin / davey / PyNaCl ───────────────────
info "[4/8] Setting Android / Rust build environment"

# These two exports were the key that let maturin proceed past
# "Failed to determine Android API level"
export ANDROID_API_LEVEL="${ANDROID_API_LEVEL:-34}"
export CARGO_BUILD_TARGET="${CARGO_BUILD_TARGET:-aarch64-linux-android}"

# Help PyNaCl find the system libsodium we just installed via pkg
export SODIUM_INSTALL=system

info "ANDROID_API_LEVEL=${ANDROID_API_LEVEL}"
info "CARGO_BUILD_TARGET=${CARGO_BUILD_TARGET}"
info "SODIUM_INSTALL=${SODIUM_INSTALL}"

# maturin is required to build davey
info "Installing maturin (needed for davey)"
pip install maturin || warn "maturin install had issues — davey may fail later"

# ── 5. Core + voice packages ───────────────────────────────────────────
info "[5/8] Installing Python packages (voice stack included)"

# First the easy ones
pip install \
  "python-dotenv==1.2.2" \
  "aiosqlite==0.21.0" \
  "yt-dlp" \
  || true

# discord.py + voice extras (this pulls PyNaCl + davey)
info "Installing discord.py[voice] (this can take a while on first run)..."
if ! pip install "discord.py[voice]==2.7.1" --no-build-isolation; then
  warn "Full discord.py[voice] failed — trying stepwise approach"

  # Stepwise: discord.py core first
  pip install "discord.py==2.7.1" || true

  # PyNaCl with the flags that worked on Termux
  info "Installing PyNaCl with SODIUM_INSTALL=system + --no-binary..."
  if ! SODIUM_INSTALL=system pip install --no-binary=PyNaCl --force-reinstall --no-cache-dir "PyNaCl>=1.5.0,<1.6"; then
    warn "PyNaCl 1.5.x failed — trying 1.4.0 as last resort"
    SODIUM_INSTALL=system pip install --no-binary=PyNaCl --force-reinstall --no-cache-dir "PyNaCl==1.4.0" || true
  fi

  # davey (needs the ANDROID_API_LEVEL we set earlier)
  info "Installing davey..."
  pip install davey --no-build-isolation || warn "davey failed to build — basic voice may still work without full DAVE"
fi

success "Python package stage finished"

# ── 6. Verify ──────────────────────────────────────────────────────────
info "[6/8] Verifying critical imports"
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
    print("Some core packages failed. The bot may still start but voice could be limited.")
PY

# ── 7. .env ────────────────────────────────────────────────────────────
info "[7/8] Configuring .env"

DENO_PATH="$(command -v deno 2>/dev/null || echo /data/data/com.termux/files/usr/bin/deno)"

if [[ -f "\( {ENV_PATH}" ]] && grep -q "^DISCORD_TOKEN=" " \){ENV_PATH}" 2>/dev/null \
   && ! grep -qE "^DISCORD_TOKEN=(replace_me|your_discord_bot_token_here)?\( " " \){ENV_PATH}" 2>/dev/null; then
  info "Existing filled .env found — keeping it."
  if ! grep -q "^YTDLP_JS_RUNTIME_PATH=" "${ENV_PATH}"; then
    echo "YTDLP_JS_RUNTIME_PATH=\( {DENO_PATH}" >> " \){ENV_PATH}"
    success "Appended YTDLP_JS_RUNTIME_PATH"
  fi
else
  if [[ ! -f deploy/.env.example ]]; then
    error "deploy/.env.example missing — is the clone complete?"
    exit 1
  fi
  cp deploy/.env.example "${ENV_PATH}"

  echo ""
  echo "\( {BOLD}Discord Bot Token \){RESET} (required)"
  echo "https://discord.com/developers/applications  →  Bot tab  →  Reset/Copy Token"
  echo "Also enable MESSAGE CONTENT INTENT on that same tab."
  echo ""
  while true; do
    read -rsp "Paste Discord Bot Token (input hidden): " TOKEN
    echo ""
    if [[ -n "$TOKEN" && "$TOKEN" != "your_discord_bot_token_here" ]]; then
      break
    fi
    error "Token cannot be empty."
  done

  sed -i "s|^DISCORD_TOKEN=.*|DISCORD_TOKEN=\( {TOKEN}|" " \){ENV_PATH}"
  if grep -q "^YTDLP_JS_RUNTIME_PATH=" "${ENV_PATH}"; then
    sed -i "s|^YTDLP_JS_RUNTIME_PATH=.*|YTDLP_JS_RUNTIME_PATH=\( {DENO_PATH}|" " \){ENV_PATH}"
  else
    echo "YTDLP_JS_RUNTIME_PATH=\( {DENO_PATH}" >> " \){ENV_PATH}"
  fi
  success "Wrote ${ENV_PATH}"
fi

# ── 8. Done ────────────────────────────────────────────────────────────
info "[8/8] Setup finished"
echo ""
echo "\( {BOLD}Run the bot: \){RESET}"
echo "  cd ${APP_DIR}"
echo "  source .venv/bin/activate"
echo "  python bot.py"
echo ""
echo "\( {BOLD}Keep it alive after closing Termux: \){RESET}"
echo "  pkg install tmux"
echo "  tmux new -s musicbot"
echo "  # start the bot inside the session"
echo "  # Detach: Ctrl+B  then  D"
echo "  # Reattach later: tmux attach -t musicbot"
echo ""
echo "Optional longer-term: install Termux:Boot (F-Droid) + termux-services"
echo "if you want cleaner auto-start behaviour."
echo ""
echo "In Discord: join a voice channel → !join → !play <query or URL>"
echo ""