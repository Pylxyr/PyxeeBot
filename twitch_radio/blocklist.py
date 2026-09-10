from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

_YOUTUBE_ID_RE = re.compile(r"^[\w-]{11}$")

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SOUNDCLOUD_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}


def normalize_track_key(url: str) -> str | None:
    """Returns a stable block-key for a YouTube or SoundCloud track URL, or
    None if the URL isn't recognized as either (matches
    extraction.py's own YouTube/SoundCloud-only restriction — nothing else
    is ever playable here anyway).

    YouTube keys are the bare 11-character video ID, so youtu.be/<id>,
    youtube.com/watch?v=<id>, and youtube.com/shorts/<id> all normalize to
    the same key regardless of which form a mod happened to paste or which
    form yt-dlp's webpage_url comes back as. SoundCloud has no separate
    short-link form to worry about, so its key is just the lowercased path
    with query/fragment stripped.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()

    if host == "youtu.be":
        vid = parts.path.strip("/").split("/")[0]
        return f"yt:{vid}" if _YOUTUBE_ID_RE.match(vid) else None

    if host in _YOUTUBE_HOSTS:
        if parts.path.startswith("/shorts/"):
            vid = parts.path.removeprefix("/shorts/").strip("/").split("/")[0]
            return f"yt:{vid}" if _YOUTUBE_ID_RE.match(vid) else None
        values = parse_qs(parts.query).get("v")
        v = values[0] if values else None
        return f"yt:{v}" if v and _YOUTUBE_ID_RE.match(v) else None

    if host in _SOUNDCLOUD_HOSTS:
        path = parts.path.rstrip("/").lower()
        return f"sc:{path}" if path else None

    return None


def blocklist_reason(webpage_url: str, uploader: str, data: dict[str, Any]) -> str | None:
    """Returns a short human-readable reason the given track is blocked, or
    None if it isn't. `data` is whatever's currently in the blocklist
    JsonStore — {"tracks": [...normalize_track_key() keys...], "uploaders":
    [...lowercased names...]}, tolerant of either key being absent (a fresh
    store) or the wrong type (hand-edited file)."""
    tracks = data.get("tracks")
    uploaders = data.get("uploaders")
    key = normalize_track_key(webpage_url)
    if key is not None and isinstance(tracks, list) and key in tracks:
        return "that track"
    if isinstance(uploaders, list) and uploader.strip().lower() in uploaders:
        return f"uploader {uploader!r}"
    return None


def add_track_block(data: dict[str, Any], key: str) -> dict[str, Any]:
    tracks = set(data.get("tracks") or [])
    tracks.add(key)
    return {**data, "tracks": sorted(tracks)}


def remove_track_block(data: dict[str, Any], key: str) -> dict[str, Any]:
    tracks = set(data.get("tracks") or [])
    tracks.discard(key)
    return {**data, "tracks": sorted(tracks)}


def add_uploader_block(data: dict[str, Any], name: str) -> dict[str, Any]:
    uploaders = set(data.get("uploaders") or [])
    uploaders.add(name.strip().lower())
    return {**data, "uploaders": sorted(uploaders)}


def remove_uploader_block(data: dict[str, Any], name: str) -> dict[str, Any]:
    uploaders = set(data.get("uploaders") or [])
    uploaders.discard(name.strip().lower())
    return {**data, "uploaders": sorted(uploaders)}


def counts(data: dict[str, Any]) -> tuple[int, int]:
    tracks = data.get("tracks")
    uploaders = data.get("uploaders")
    return (
        len(tracks) if isinstance(tracks, list) else 0,
        len(uploaders) if isinstance(uploaders, list) else 0,
    )
