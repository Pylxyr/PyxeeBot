from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Track:
    """A resolved, playable track. stream_url is a direct, time-limited media
    URL from yt-dlp — cached briefly by Resolver.resolve() (YTDLP_CACHE_TTL_SECONDS)
    but not indefinitely, since it expires; don't hold onto one across a long
    queue wait outside that cache."""

    title: str
    webpage_url: str
    stream_url: str
    uploader: str
    duration: int
    requester_id: int
    thumbnail_url: str | None = None
    query: str = ""
    is_live: bool = False
