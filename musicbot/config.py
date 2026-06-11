from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    token: str
    default_prefix: str
    bot_owners: tuple[int, ...]
    log_level: str
    db_path: Path
    max_queue_size: int
    max_queue_size_per_user: int
    max_playlist_size: int
    idle_timeout_seconds: int
    empty_channel_timeout_seconds: int
    log_to_file: bool
    log_dir: Path
    ytdlp_cookies_file: Path | None
    ytdlp_js_runtime_path: str | None
    ytdlp_socket_timeout: int
    ytdlp_prefetch_count: int
    ytdlp_concurrent_extracts: int
    ytdlp_curation_concurrency: int
    near_end_prefetch_seconds: int
    opus_bitrate_kbps: int
    ytdlp_search_results: int
    ytdlp_resolve_cache_size: int
    ytdlp_resolve_cache_ttl_seconds: int
    ytdlp_extract_timeout_seconds: int
    np_auto_refresh: bool
    np_auto_refresh_interval: int
    error_announce: bool
    lastfm_api_key: str | None
    restore_queue_on_restart: bool


def _parse_owner_ids(raw_value: str) -> tuple[int, ...]:
    return tuple(int(c.strip()) for c in raw_value.split(",") if c.strip())


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got: {raw!r}") from None


def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set. Add it to .env before starting the bot.")

    default_prefix = os.getenv("DEFAULT_PREFIX", "!").strip() or "!"
    if " " in default_prefix:
        raise RuntimeError("DEFAULT_PREFIX cannot contain spaces.")

    log_dir = BASE_DIR / os.getenv("LOG_DIR", "logs")
    log_dir.mkdir(exist_ok=True)

    cookies_file_raw = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    ytdlp_cookies_file = Path(cookies_file_raw) if cookies_file_raw else None
    if ytdlp_cookies_file and not ytdlp_cookies_file.is_absolute():
        ytdlp_cookies_file = BASE_DIR / ytdlp_cookies_file

    ytdlp_js_runtime_path = os.getenv("YTDLP_JS_RUNTIME_PATH", "").strip() or None

    return Settings(
        token=token,
        default_prefix=default_prefix,
        bot_owners=_parse_owner_ids(os.getenv("BOT_OWNERS", "")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        db_path=DATA_DIR / "musicbot.sqlite3",
        max_queue_size=max(1, _int_env("MAX_QUEUE_SIZE", 100)),
        max_queue_size_per_user=max(0, _int_env("MAX_QUEUE_SIZE_PER_USER", 0)),
        max_playlist_size=max(1, _int_env("MAX_PLAYLIST_SIZE", 25)),
        idle_timeout_seconds=max(30, _int_env("IDLE_TIMEOUT_SECONDS", 180)),
        empty_channel_timeout_seconds=max(15, _int_env("EMPTY_CHANNEL_TIMEOUT_SECONDS", 60)),
        log_to_file=os.getenv("LOG_TO_FILE", "true").strip().lower() in {"1", "true", "yes", "on"},
        log_dir=log_dir,
        ytdlp_cookies_file=ytdlp_cookies_file,
        ytdlp_js_runtime_path=ytdlp_js_runtime_path,
        ytdlp_socket_timeout=max(5, _int_env("YTDLP_SOCKET_TIMEOUT", 15)),
        ytdlp_prefetch_count=max(0, _int_env("YTDLP_PREFETCH_COUNT", 1)),
        ytdlp_concurrent_extracts=max(1, _int_env("YTDLP_CONCURRENT_EXTRACTS", 1)),
        ytdlp_curation_concurrency=max(1, min(6, _int_env("YTDLP_CURATION_CONCURRENCY", 3))),
        near_end_prefetch_seconds=max(0, _int_env("NEAR_END_PREFETCH_SECONDS", 30)),
        opus_bitrate_kbps=max(64, min(256, _int_env("OPUS_BITRATE_KBPS", 96))),
        ytdlp_search_results=max(1, min(10, _int_env("YTDLP_SEARCH_RESULTS", 5))),
        ytdlp_resolve_cache_size=max(16, _int_env("YTDLP_RESOLVE_CACHE_SIZE", 128)),
        ytdlp_resolve_cache_ttl_seconds=max(60, _int_env("YTDLP_RESOLVE_CACHE_TTL_SECONDS", 1800)),
        ytdlp_extract_timeout_seconds=max(5, _int_env("YTDLP_EXTRACT_TIMEOUT_SECONDS", 45)),
        np_auto_refresh=os.getenv("NP_AUTO_REFRESH", "false").strip().lower() in {"1", "true", "yes", "on"},
        np_auto_refresh_interval=max(15, _int_env("NP_AUTO_REFRESH_INTERVAL", 30)),
        error_announce=os.getenv("ERROR_ANNOUNCE", "true").strip().lower() in {"1", "true", "yes", "on"},
        lastfm_api_key=os.getenv("LASTFM_API_KEY", "").strip() or None,
        restore_queue_on_restart=os.getenv("RESTORE_QUEUE_ON_RESTART", "true").strip().lower() in {"1", "true", "yes", "on"},
    )
