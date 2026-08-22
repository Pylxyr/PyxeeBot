from __future__ import annotations

from typing import Literal

import discord

FFMPEG_BEFORE_OPTIONS = (
    "-nostdin "
    "-threads 1 "
    "-thread_queue_size 512 "
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_delay_max 5 "
    "-reconnect_on_network_error 1 "
    "-reconnect_on_http_error 429,500,502,503,504 "
    "-probesize 128k "
    "-analyzeduration 0"
)
FFMPEG_OPTIONS = "-vn -ar 48000 -ac 2 -application lowdelay -frame_duration 20 -flush_packets 1"

YTDL_OPTIONS: dict[str, object] = {
    "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best[height<=480]",
    "quiet": True,
    "noplaylist": False,
    "skip_download": True,
    "default_search": "ytsearch",
    "ignoreerrors": True,
    "extract_flat": False,
    "socket_timeout": 15,
}

NOW_PLAYING_PREVIEW_LIMIT = 5
QUEUE_MESSAGE_LIMIT = 20
QUEUE_PAGE_SIZE = 8
QUEUE_VIEW_TIMEOUT_SECONDS = 300
NOW_PLAYING_TIMEOUT_SECONDS = 1800
SNAPSHOT_DEBOUNCE_SECONDS = 0.5
STREAM_URL_REFRESH_AGE_SECONDS = 4 * 60 * 60
SEARCH_SELECTION_PAGE_SIZE = 5
SEARCH_SELECTION_LIMIT = 10
SEARCH_SELECTION_TIMEOUT_SECONDS = 120
VOICE_RECONNECT_ATTEMPTS = 2
NP_REFRESH_DEBOUNCE_SECONDS = 0.8

LoopMode = Literal["off", "one", "all"]

LOOP_CYCLE: dict[LoopMode, LoopMode] = {"off": "one", "one": "all", "all": "off"}
LOOP_LABELS: dict[str, str] = {"off": "Off", "one": "Single track", "all": "Entire queue"}
LOOP_ICONS: dict[str, str] = {"off": "→", "one": "↻¹", "all": "↻"}

EMBED_COLOUR = discord.Colour.from_rgb(255, 170, 64)
