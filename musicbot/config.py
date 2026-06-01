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
    near_end_prefetch_seconds: int
    opus_bitrate_kbps: int
    ytdlp_search_results: int
    ytdlp_resolve_cache_size: int
    ytdlp_resolve_cache_ttl_seconds: int
    ytdlp_extract_timeout_seconds: int
    np_auto_refresh: bool        # background NP progress-bar refresh
    np_auto_refresh_interval: int  # seconds between auto-refresh edits
    error_announce: bool         # post skip-error msgs to announce channel
    lastfm_api_key: str | None   # Last.fm API key for CurationCog


def _parse_owner_ids(raw_value: str) -> tuple[int, ...]:
    if not raw_value.strip():
        return ()
    owner_ids: list[int] = []
    for chunk in raw_value.split(","):
        chunk = chunk.strip()
        if chunk:
            owner_ids.append(int(chunk))
    return tuple(owner_ids)


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
        max_queue_size=max(1, int(os.getenv("MAX_QUEUE_SIZE", "100"))),
        max_playlist_size=max(1, int(os.getenv("MAX_PLAYLIST_SIZE", "25"))),
        idle_timeout_seconds=max(30, int(os.getenv("IDLE_TIMEOUT_SECONDS", "180"))),
        empty_channel_timeout_seconds=max(15, int(os.getenv("EMPTY_CHANNEL_TIMEOUT_SECONDS", "60"))),
        log_to_file=os.getenv("LOG_TO_FILE", "true").strip().lower() in {"1", "true", "yes", "on"},
        log_dir=log_dir,
        ytdlp_cookies_file=ytdlp_cookies_file,
        ytdlp_js_runtime_path=ytdlp_js_runtime_path,
        ytdlp_socket_timeout=max(5, int(os.getenv("YTDLP_SOCKET_TIMEOUT", "15"))),
        ytdlp_prefetch_count=max(0, int(os.getenv("YTDLP_PREFETCH_COUNT", "1"))),
        ytdlp_concurrent_extracts=max(1, int(os.getenv("YTDLP_CONCURRENT_EXTRACTS", "1"))),
        near_end_prefetch_seconds=max(0, int(os.getenv("NEAR_END_PREFETCH_SECONDS", "30"))),
        opus_bitrate_kbps=max(64, min(256, int(os.getenv("OPUS_BITRATE_KBPS", "96")))),
        ytdlp_search_results=max(1, min(10, int(os.getenv("YTDLP_SEARCH_RESULTS", "5")))),
        ytdlp_resolve_cache_size=max(16, int(os.getenv("YTDLP_RESOLVE_CACHE_SIZE", "128"))),
        ytdlp_resolve_cache_ttl_seconds=max(60, int(os.getenv("YTDLP_RESOLVE_CACHE_TTL_SECONDS", "1800"))),
        ytdlp_extract_timeout_seconds=max(5, int(os.getenv("YTDLP_EXTRACT_TIMEOUT_SECONDS", "45"))),
        np_auto_refresh=os.getenv("NP_AUTO_REFRESH", "false").strip().lower() in {"1", "true", "yes", "on"},
        np_auto_refresh_interval=max(15, int(os.getenv("NP_AUTO_REFRESH_INTERVAL", "30"))),
        error_announce=os.getenv("ERROR_ANNOUNCE", "true").strip().lower() in {"1", "true", "yes", "on"},
        lastfm_api_key=os.getenv("LASTFM_API_KEY", "").strip() or None,
    )
