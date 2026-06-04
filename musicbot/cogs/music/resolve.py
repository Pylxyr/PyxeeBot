"""resolve.py — ResolveCache: track URL resolution with deduplication and TTL.

Encapsulates the OrderedDict cache, in-flight task deduplication, and the
apply/get/store methods that were previously scattered across MusicCog.

Fix #3: cache keys use the YouTube URL when available; bare search queries
        fall back to a prefixed key so they never collide with real URLs.
Fix #10: resolve task exceptions are surfaced via done_callback, never silently
         swallowed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from musicbot.cogs.music.models import ResolvedTrackData, Track

log = logging.getLogger(__name__)


class ResolveCache:
    """LRU cache for resolved track data with in-flight task deduplication.

    Parameters
    ----------
    max_size:
        Maximum number of entries.  Oldest is evicted when full.
    ttl_seconds:
        Seconds before a cached entry is considered stale.
    extract_full:
        Async callable ``(url_or_query, requester_id) -> list[Track]``.
        Provided by MusicCog so this class has no direct yt-dlp dependency.
    """

    def __init__(
        self,
        *,
        max_size: int,
        ttl_seconds: int,
        extract_full: Callable[[str, int], Awaitable[tuple[list[Track], int]]],
    ) -> None:
        self._max_size    = max_size
        self._ttl         = ttl_seconds
        self._extract     = extract_full
        self._cache:  OrderedDict[str, tuple[float, ResolvedTrackData]] = OrderedDict()
        self._tasks:  dict[str, asyncio.Task[ResolvedTrackData | None]] = {}

    # ------------------------------------------------------------------
    # Cache key (Fix #3)
    # ------------------------------------------------------------------

    @staticmethod
    def key_for(track: Track) -> str:
        """Stable cache key that avoids collisions between search strings and URLs."""
        url = track.webpage_url
        if url and url.startswith(("http://", "https://")):
            return url
        # Prefix search queries so they never shadow a real URL entry.
        return f"q:{track.query or track.title}"

    # ------------------------------------------------------------------
    # Internal read / write
    # ------------------------------------------------------------------

    def _get(self, key: str) -> ResolvedTrackData | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return data

    def _store(self, data: ResolvedTrackData) -> None:
        key = data.webpage_url if data.webpage_url.startswith("http") else f"q:{data.query}"
        expires_at = time.monotonic() + self._ttl
        self._cache[key] = (expires_at, data)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    # ------------------------------------------------------------------
    # Public resolution (with dedup + Fix #10 exception logging)
    # ------------------------------------------------------------------

    async def resolve(self, track: Track) -> Track | None:
        """Resolve *track* to a playable stream URL.  Returns the same Track
        object mutated in place, or ``None`` if resolution fails."""
        if track.stream_url:
            return track

        cache_key = self.key_for(track)
        cached = self._get(cache_key)
        if cached is not None:
            _apply(track, cached)
            return track

        pending = self._tasks.get(cache_key)
        if pending is None:
            pending = asyncio.create_task(
                self._run_resolve(track),
                name=f"resolve:{cache_key[:60]}",
            )
            self._tasks[cache_key] = pending

            # Fix #10: log exceptions unconditionally so they are never swallowed.
            def _on_done(t: asyncio.Task[Any]) -> None:
                self._tasks.pop(cache_key, None)
                if not t.cancelled() and t.exception() is not None:
                    log.warning("Resolve task failed for %s: %s", cache_key, t.exception())
            pending.add_done_callback(_on_done)

        try:
            data = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # shield() was cancelled externally — don't kill the inner task.
            return None
        except Exception:
            return None

        if data is None:
            return None
        _apply(track, data)
        return track

    async def _run_resolve(self, track: Track) -> ResolvedTrackData | None:
        tracks, _ = await self._extract(
            track.webpage_url or track.query, track.requester_id
        )
        if not tracks:
            return None
        resolved = tracks[0]
        data = ResolvedTrackData(
            title=resolved.title,
            webpage_url=resolved.webpage_url,
            stream_url=resolved.stream_url,
            uploader=resolved.uploader,
            duration=resolved.duration,
            query=resolved.query,
            resolved_at=resolved.resolved_at or time.monotonic(),
            thumbnail_url=resolved.thumbnail_url,
            tags=resolved.tags,
        )
        self._store(data)
        return data

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def invalidate(self, track: Track) -> None:
        """Remove a track from cache (e.g. after URL expiry)."""
        self._cache.pop(self.key_for(track), None)


def _apply(track: Track, data: ResolvedTrackData) -> None:
    """Mutate *track* in place from resolved data."""
    track.title       = data.title
    track.webpage_url = data.webpage_url
    track.stream_url  = data.stream_url
    track.uploader    = data.uploader
    track.duration    = data.duration
    track.query       = data.query
    track.resolved_at = data.resolved_at
    if data.thumbnail_url:
        track.thumbnail_url = data.thumbnail_url
    if data.tags:
        track.tags = data.tags
