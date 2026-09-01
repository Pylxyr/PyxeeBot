from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

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
    bot_activity_url: str
    twitch_client_id: str | None
    twitch_client_secret: str | None
    twitch_bot_id: str | None
    twitch_owner_id: str | None
    twitch_stream_key: str | None
    twitch_ingest_url: str
    twitch_prefix: str
    twitch_background_image: Path
    twitch_video_bitrate_kbps: int
    twitch_video_fps: int
    twitch_nowplaying_host: str
    twitch_nowplaying_port: int
    twitch_settings_password: str | None

    @property
    def twitch_enabled(self) -> bool:
        return bool(
            self.twitch_client_id
            and self.twitch_client_secret
            and self.twitch_bot_id
            and self.twitch_owner_id
            and self.twitch_stream_key
        )


def _parse_owner_ids(raw_value: str) -> tuple[int, ...]:
    ids: list[int] = []
    for chunk in raw_value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            raise RuntimeError(
                f"BOT_OWNERS must be a comma-separated list of user IDs, got: {chunk!r}"
            ) from None
    return tuple(ids)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got: {raw!r}") from None


def load_settings() -> Settings:
    DATA_DIR.mkdir(exist_ok=True)

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set. Add it to .env before starting the bot.")
    if token in {"your_discord_bot_token_here", "replace_me"}:
        raise RuntimeError(
            "DISCORD_TOKEN is still set to the placeholder value. Edit .env with your real bot token."
        )

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
        opus_bitrate_kbps=max(64, min(256, _int_env("OPUS_BITRATE_KBPS", 64))),
        ytdlp_search_results=max(1, min(10, _int_env("YTDLP_SEARCH_RESULTS", 5))),
        ytdlp_resolve_cache_size=max(16, _int_env("YTDLP_RESOLVE_CACHE_SIZE", 128)),
        ytdlp_resolve_cache_ttl_seconds=max(60, _int_env("YTDLP_RESOLVE_CACHE_TTL_SECONDS", 1800)),
        ytdlp_extract_timeout_seconds=max(5, _int_env("YTDLP_EXTRACT_TIMEOUT_SECONDS", 45)),
        np_auto_refresh=os.getenv("NP_AUTO_REFRESH", "false").strip().lower() in {"1", "true", "yes", "on"},
        np_auto_refresh_interval=max(15, _int_env("NP_AUTO_REFRESH_INTERVAL", 30)),
        error_announce=os.getenv("ERROR_ANNOUNCE", "true").strip().lower() in {"1", "true", "yes", "on"},
        lastfm_api_key=os.getenv("LASTFM_API_KEY", "").strip() or None,
        restore_queue_on_restart=os.getenv("RESTORE_QUEUE_ON_RESTART", "true").strip().lower()
        in {"1", "true", "yes", "on"},
        bot_activity_url=os.getenv("BOT_ACTIVITY_URL", "pylxyr.github.io/PyxeeBot-Page/").strip()
        or "pylxyr.github.io/PyxeeBot-Page/",
        # Twitch integration is entirely optional — leave TWITCH_STREAM_KEY unset and
        # none of this loads or runs (see Settings.twitch_enabled / bot.py). Every
        # field here has a get-it-from-here pointer in .env.example.
        twitch_client_id=os.getenv("TWITCH_CLIENT_ID", "").strip() or None,
        twitch_client_secret=os.getenv("TWITCH_CLIENT_SECRET", "").strip() or None,
        twitch_bot_id=os.getenv("TWITCH_BOT_ID", "").strip() or None,
        twitch_owner_id=os.getenv("TWITCH_OWNER_ID", "").strip() or None,
        twitch_stream_key=os.getenv("TWITCH_STREAM_KEY", "").strip() or None,
        twitch_ingest_url=os.getenv("TWITCH_INGEST_URL", "rtmp://live.twitch.tv/app").strip()
        or "rtmp://live.twitch.tv/app",
        twitch_prefix=os.getenv("TWITCH_PREFIX", "!").strip() or "!",
        twitch_background_image=BASE_DIR
        / os.getenv("TWITCH_BACKGROUND_IMAGE", "deploy/twitch_background.png").strip(),
        twitch_video_bitrate_kbps=max(300, min(3000, _int_env("TWITCH_VIDEO_BITRATE_KBPS", 800))),
        twitch_video_fps=max(1, min(10, _int_env("TWITCH_VIDEO_FPS", 2))),
        twitch_nowplaying_host=os.getenv("TWITCH_NOWPLAYING_HOST", "127.0.0.1").strip() or "127.0.0.1",
        twitch_nowplaying_port=max(1024, min(65535, _int_env("TWITCH_NOWPLAYING_PORT", 8098))),
        twitch_settings_password=os.getenv("TWITCH_SETTINGS_PASSWORD", "").strip() or None,
    )
