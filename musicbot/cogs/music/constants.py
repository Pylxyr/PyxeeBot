"""constants.py — All static data for the music subsystem.

Zero bot/discord dependencies except for EMBED_COLOUR.
"""
from __future__ import annotations

import re
import discord

# ---------------------------------------------------------------------------
# FFmpeg
# ---------------------------------------------------------------------------

FFMPEG_BEFORE_OPTIONS = (
    "-nostdin "
    "-threads 1 "
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_delay_max 5"
)
FFMPEG_OPTIONS = "-vn -ar 48000 -ac 2"

# ---------------------------------------------------------------------------
# yt-dlp base options (settings-dependent values added at runtime)
# ---------------------------------------------------------------------------

YTDL_OPTIONS: dict[str, object] = {
    # Prefer Opus/WebM (already encoded, no transcode needed) then M4A, then
    # any audio-only stream, and only fall back to a muxed video stream as a
    # last resort capped at 480p to avoid pulling multi-megabit video data.
    "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best[height<=480]",
    "quiet": True,
    "noplaylist": False,
    "skip_download": True,
    "default_search": "ytsearch",
    "ignoreerrors": True,
    "extract_flat": False,
}

# ---------------------------------------------------------------------------
# Timing / sizing
# ---------------------------------------------------------------------------

NOW_PLAYING_PREVIEW_LIMIT         = 5
QUEUE_MESSAGE_LIMIT                = 20
QUEUE_PAGE_SIZE                    = 8
QUEUE_VIEW_TIMEOUT_SECONDS         = 300
NOW_PLAYING_TIMEOUT_SECONDS        = 1800
SNAPSHOT_DEBOUNCE_SECONDS          = 0.5
STREAM_URL_REFRESH_AGE_SECONDS     = 4 * 60 * 60
SEARCH_SELECTION_PAGE_SIZE         = 5
SEARCH_SELECTION_LIMIT             = 10
SEARCH_SELECTION_TIMEOUT_SECONDS   = 120
VOICE_RECONNECT_ATTEMPTS           = 2
NP_REFRESH_DEBOUNCE_SECONDS        = 0.8
PRESENCE_DEBOUNCE_SECONDS          = 5.0

# ---------------------------------------------------------------------------
# Loop mode
# ---------------------------------------------------------------------------

LOOP_CYCLE: dict[str, str]  = {"off": "one", "one": "all", "all": "off"}
LOOP_LABELS: dict[str, str] = {"off": "Off", "one": "Single track", "all": "Entire queue"}
LOOP_ICONS: dict[str, str]  = {"off": "→", "one": "↻¹", "all": "↻"}

# ---------------------------------------------------------------------------
# Brand
# ---------------------------------------------------------------------------

EMBED_COLOUR = discord.Colour.from_rgb(255, 170, 64)

# ---------------------------------------------------------------------------
# Search scoring tables
# ---------------------------------------------------------------------------

SEARCH_GENERIC_TOKENS: frozenset[str] = frozenset({
    "audio", "full", "hd", "hq", "lyrics", "lyric",
    "music", "official", "song", "ver", "version", "video",
})
SEARCH_ANIME_SIGNAL_TOKENS: frozenset[str] = frozenset({
    "op", "ed", "ost", "opening", "ending", "theme", "anime", "tv",
})
SEARCH_DISCOURAGED_TOKENS: dict[str, float] = {
    "amv": 0.60, "cast": 0.70, "cover": 0.60, "edit": 0.15,
    "instrumental": 0.60, "karaoke": 0.70, "nightcore": 0.70,
    "remix": 0.45, "reverb": 0.22, "seiyuu": 0.70, "slowed": 0.45,
    "live": 0.20,
    "guitar": 0.50, "piano": 0.50, "violin": 0.45,
    "acoustic": 0.35, "fingerstyle": 0.55, "ukulele": 0.55,
    "bass": 0.45, "drums": 0.45, "drum": 0.40,
    "flute": 0.45, "cello": 0.45, "harp": 0.45, "saxophone": 0.45,
    "lyrics": 0.80, "lyric": 0.50, "romaji": 0.70,
    "subtitles": 0.35, "kanji": 0.35, "translation": 0.45,
}
SEARCH_DISCOURAGED_PHRASES: dict[str, float] = {
    "cast version": 0.80, "cast ver": 0.75, "character song": 0.65,
    "female version": 0.40, "male version": 0.40,
    "lyric video": 0.45, "lyrics video": 0.50, "with lyrics": 0.50,
    "english cover": 0.80, "first take": 0.65,
    "short ver": 0.30, "short version": 0.30, "sped up": 0.45,
    "tv size": 0.22,
    "anime size": 0.40, "anime ver": 0.35, "anime version": 0.35,
    "op ver": 0.35, "ed ver": 0.35,
    "1 hour": 0.90, "one hour": 0.90, "10 hours": 0.90,
    "2 hours": 0.90, "3 hours": 0.90,
    "extended mix": 0.30, "full album": 0.60,
    "compilation": 0.50, "best of": 0.35,
}
SEARCH_PREFERRED_PHRASES: dict[str, float] = {
    "official audio": 0.30, "official music video": 0.22,
    "official mv": 0.20, "official ver": 0.18, "official version": 0.18,
    "official video": 0.16, "music video": 0.20,
}
SEARCH_PREFERRED_UPLOADER_TOKENS: dict[str, float] = {
    "topic": 0.35, "vevo": 0.28,
    "hybe": 0.22, "bighit": 0.22, "smtown": 0.22,
    "ygentertainment": 0.22, "jyp": 0.18, "starship": 0.16,
    "official": 0.22, "records": 0.10, "music": 0.06,
    "avex": 0.18, "ponycanyon": 0.18, "kingrecords": 0.18,
    "sonymusic": 0.18, "columbia": 0.15, "victor": 0.15,
    "tokyorecords": 0.15, "lantis": 0.15, "kicm": 0.12,
    "universal": 0.14, "warner": 0.14, "atlantic": 0.14,
    "capitol": 0.14, "interscope": 0.12, "republic": 0.12,
}

# ---------------------------------------------------------------------------
# Compiled regex (module-level so they are compiled once)
# ---------------------------------------------------------------------------

_ANIME_INTENT_RE = re.compile(
    r"\b(op|ed|ost|opening|ending|theme|insert\s*song|anime|season)\b",
    re.IGNORECASE,
)
_DASH_SEPARATED_RE   = re.compile(r"^.+\s*[-–]\s*.+$")
_JP_COVER_BRACKET_RE = re.compile(
    r"^[\s\[【\(]*"
    r"(ギター|ピアノ|バイオリン|チェロ|ベース|ドラム|弾いてみた|歌ってみた|叩いてみた|カバー|アレンジ|フル)"
    r"[\s\]】\)]*",
    re.IGNORECASE,
)
_BRACKET_STRIP_RE = re.compile(r'[\(\[（【][^\)\]）】]*[\)\]）】]')
_CJK_RE    = re.compile(r'[\u3040-\u30ff\u4e00-\u9fff]')
_HANGUL_RE = re.compile(r'[\uAC00-\uD7AF\u3130-\u318F]')
