from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

load_dotenv(BASE_DIR / ".env")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _clamped_int_env(name: str, default: int, lo: int, hi: int) -> int:
    # _int_env alone silently clamps out-of-range values with no trace of
    # it anywhere — e.g. AUDIO_BITRATE_KBPS=999999 would just quietly
    # become 320 with nothing in the logs explaining the mismatch between
    # what's in .env and what the service actually runs with. Same
    # print()-not-log rationale as _log_level_env above: this runs before
    # configure_logging() exists.
    value = _int_env(name, default)
    clamped = max(lo, min(hi, value))
    if clamped != value:
        print(f"WARNING: {name}={value} is outside the allowed range {lo}-{hi} — using {clamped}.")
    return clamped


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _log_level_env(name: str, default: str) -> str:
    # print() is deliberate — this runs before configure_logging() exists.
    raw = os.getenv(name, "").strip().upper()
    if not raw:
        return default
    if raw not in _VALID_LOG_LEVELS:
        print(f"WARNING: {name}={raw!r} is not a valid log level — using {default}.")
        return default
    return raw


def _check_cookies_path_writable(raw: str, path: Path) -> None:
    # yt-dlp saves this file back on every single extraction once configured
    # at all, and only data/ and logs/ are writable under the systemd unit's
    # hardening — checked here so a bad path fails loudly at startup instead
    # of on every !sr.
    cookies_dir = path.parent
    try:
        cookies_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"YTDLP_COOKIES_FILE={raw!r} resolves to {path}, but its directory ({cookies_dir}) "
            f"couldn't be created: {exc}. Use a path under data/ instead, e.g. "
            f"YTDLP_COOKIES_FILE=data/cookies.txt."
        ) from exc
    if not os.access(cookies_dir, os.W_OK):
        raise RuntimeError(
            f"YTDLP_COOKIES_FILE={raw!r} resolves to {path}, but {cookies_dir} isn't writable. "
            f"Use a path under data/ instead, e.g. YTDLP_COOKIES_FILE=data/cookies.txt."
        )


# YouTube player_client names and whether each accepts cookie auth, per
# yt_dlp.extractor.youtube._base.INNERTUBE_CLIENTS[*]["SUPPORTS_COOKIES"] —
# hardcoded rather than imported since that's a private yt-dlp module that
# can change shape across versions; re-verify against the pinned yt-dlp
# version in requirements.txt if this ever needs updating.
# Checked against yt-dlp==2026.08.19.
_VALID_PLAYER_CLIENTS = {
    "web": True, "web_safari": True, "web_embedded": True, "web_music": True,
    "web_creator": True, "android": False, "android_vr": False, "ios": False,
    "visionos": False, "mweb": True, "tv": True, "tv_downgraded": True, "tv_simply": False,
}


def _check_player_clients(raw_clients: tuple[str, ...], cookies_configured: bool) -> None:
    unknown = [c for c in raw_clients if c not in _VALID_PLAYER_CLIENTS]
    if unknown:
        print(
            f"WARNING: YTDLP_PLAYER_CLIENT has unrecognized client name(s) {unknown} — yt-dlp "
            f"will just skip them with a warning. Valid names: {sorted(_VALID_PLAYER_CLIENTS)}"
        )
    if cookies_configured and raw_clients:
        cookie_ok = [c for c in raw_clients if _VALID_PLAYER_CLIENTS.get(c)]
        if not cookie_ok:
            raise RuntimeError(
                f"YTDLP_PLAYER_CLIENT={','.join(raw_clients)!r} has no client that supports "
                f"cookie auth, but YTDLP_COOKIES_FILE is set — every client gets skipped and "
                f"every request fails. android/android_vr/ios/visionos/tv_simply all reject "
                f"cookies outright; mix in at least one of web/web_safari/web_embedded/"
                f"web_music/web_creator/mweb/tv/tv_downgraded, or unset YTDLP_COOKIES_FILE."
            )


@dataclass(frozen=True, slots=True)
class Settings:
    # Twitch app credentials — from https://dev.twitch.tv/console/apps
    client_id: str
    client_secret: str
    bot_id: str
    owner_id: str
    prefix: str

    # Audio
    audio_bitrate_kbps: int
    # When True, the player won't start a new track while nobody's
    # subscribed to /stream.mp3 (0 active listeners) — it just holds at the
    # current silence/track boundary and resumes normally once someone
    # (re)connects. A track already playing when the last listener
    # disconnects still finishes normally; this only holds off *starting*
    # the next one. Off by default to match existing behavior (the queue
    # has always run on a continuous real-time clock regardless of
    # listeners) — opt in via PAUSE_QUEUE_WHEN_NO_LISTENERS=true.
    pause_when_no_listeners: bool

    # Local HTTP surface — serves /stream.mp3, /overlay, /nowplaying.json, /settings
    nowplaying_host: str
    nowplaying_port: int
    settings_password: str | None

    # Persistence — both under DATA_DIR so a single ReadWritePaths entry in
    # the systemd unit covers everything this process needs to write.
    token_path: Path
    tunables_path: Path
    blocklist_path: Path

    # yt-dlp
    ytdlp_cookies_file: Path | None
    ytdlp_js_runtime_path: str | None
    ytdlp_js_runtime_name: str
    ytdlp_concurrency: int
    ytdlp_extract_timeout_seconds: int
    ytdlp_player_client: tuple[str, ...]
    ytdlp_cache_ttl_seconds: int
    ytdlp_pot_provider_url: str | None

    # Logging
    log_level: str
    log_to_file: bool
    log_dir: Path


def load_settings() -> Settings:
    DATA_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    def _required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise RuntimeError(f"{name} is not set — add it to .env before starting. See .env.example.")
        return value

    def _required_numeric_id(name: str) -> str:
        # Real production failure this guards against: TWITCH_BOT_ID pasted
        # as "Twitch ID:1536026185" instead of just the digits — Helix
        # rejects that with a bare "Bad Identifiers" error.
        value = _required(name)
        if not value.isdigit():
            raise RuntimeError(
                f"{name}={value!r} isn't a plain numeric Twitch user ID — digits only, no "
                f"username, no label. Look one up at "
                f"https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/"
            )
        return value

    client_id = _required("TWITCH_CLIENT_ID")
    client_secret = _required("TWITCH_CLIENT_SECRET")
    bot_id = _required_numeric_id("TWITCH_BOT_ID")
    owner_id = _required_numeric_id("TWITCH_OWNER_ID")

    cookies_raw = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    cookies_path = (BASE_DIR / cookies_raw) if cookies_raw else None
    if cookies_path is not None:
        _check_cookies_path_writable(cookies_raw, cookies_path)

    player_client_raw = os.getenv("YTDLP_PLAYER_CLIENT", "").strip()
    if not player_client_raw and cookies_path is not None:
        # yt-dlp's own default client list when cookies are set (verified
        # against yt-dlp==2026.08.19) is
        # ('web_embedded', 'tv_downgraded', 'web') — tv_downgraded has a
        # known open bug (yt-dlp#17389, "The page needs to be reloaded").
        # Pinning to the other two already-default clients avoids it.
        player_client_raw = "web_embedded,web"
    ytdlp_player_client = tuple(c.strip() for c in player_client_raw.split(",") if c.strip())
    _check_player_clients(ytdlp_player_client, cookies_configured=cookies_path is not None)

    nowplaying_host = os.getenv("TWITCH_NOWPLAYING_HOST", "127.0.0.1").strip() or "127.0.0.1"
    settings_password = os.getenv("TWITCH_SETTINGS_PASSWORD", "").strip() or None
    if nowplaying_host not in ("127.0.0.1", "localhost") and settings_password is None:
        print(
            f"WARNING: TWITCH_NOWPLAYING_HOST={nowplaying_host!r} is reachable off this machine, "
            f"but TWITCH_SETTINGS_PASSWORD is unset — anyone who finds the port can change your "
            f"queue/cooldown settings via /settings. Set TWITCH_SETTINGS_PASSWORD."
        )

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        bot_id=bot_id,
        owner_id=owner_id,
        prefix=os.getenv("TWITCH_PREFIX", "!").strip() or "!",
        audio_bitrate_kbps=_clamped_int_env("AUDIO_BITRATE_KBPS", 128, 64, 320),
        pause_when_no_listeners=_bool_env("PAUSE_QUEUE_WHEN_NO_LISTENERS", False),
        nowplaying_host=nowplaying_host,
        nowplaying_port=_clamped_int_env("TWITCH_NOWPLAYING_PORT", 8098, 1024, 65535),
        settings_password=settings_password,
        token_path=DATA_DIR / os.getenv("TWITCH_TOKEN_FILE", "twitch_tokens.json").strip(),
        tunables_path=DATA_DIR / os.getenv("TWITCH_TUNABLES_FILE", "tunables.json").strip(),
        blocklist_path=DATA_DIR / os.getenv("TWITCH_BLOCKLIST_FILE", "blocklist.json").strip(),
        ytdlp_cookies_file=cookies_path,
        ytdlp_js_runtime_path=os.getenv("YTDLP_JS_RUNTIME_PATH", "").strip() or None,
        ytdlp_js_runtime_name=os.getenv("YTDLP_JS_RUNTIME_NAME", "deno").strip() or "deno",
        ytdlp_concurrency=_clamped_int_env("YTDLP_CONCURRENCY", 2, 1, 4),
        ytdlp_extract_timeout_seconds=_clamped_int_env("YTDLP_EXTRACT_TIMEOUT_SECONDS", 45, 10, 120),
        ytdlp_player_client=ytdlp_player_client,
        # Skips the player's second extraction (chat resolves once to queue,
        # then it re-resolves right before playing) for anything near the
        # front of the queue. 0 disables caching.
        ytdlp_cache_ttl_seconds=_clamped_int_env("YTDLP_CACHE_TTL_SECONDS", 300, 0, 3600),
        # Only used if you've separately set up a bgutil-ytdlp-pot-provider
        # instance (see README) — points yt-dlp's PO-token plugin at it.
        # None means "no PO token provider configured", not an error.
        ytdlp_pot_provider_url=os.getenv("YTDLP_POT_PROVIDER_URL", "").strip() or None,
        log_level=_log_level_env("LOG_LEVEL", "INFO"),
        log_to_file=_bool_env("LOG_TO_FILE", True),
        log_dir=LOG_DIR,
    )
