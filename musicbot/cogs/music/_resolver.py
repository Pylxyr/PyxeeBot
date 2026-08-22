from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any

from discord.ext import commands

from musicbot.cogs.music._base import MusicCogBase
from musicbot.cogs.music._context import _CURRENT_GUILD_ID
from musicbot.cogs.music.constants import STREAM_URL_REFRESH_AGE_SECONDS
from musicbot.cogs.music.models import ResolvedTrackData, Track


class ResolverMixin(MusicCogBase):
    def _cache_key(self, track: Track) -> str:
        url = track.webpage_url
        if url and url.startswith(("http://", "https://")):
            return url
        return f"q:{track.query or track.title}"

    def _cache_key_for_data(self, data: ResolvedTrackData) -> str:
        url = data.webpage_url
        if url and url.startswith(("http://", "https://")):
            return url
        return f"q:{data.query or data.title}"

    def _get_cached_track_data(self, key: str) -> ResolvedTrackData | None:
        cached = self.resolve_cache.get(key)
        if cached is None:
            return None
        expires_at, data = cached
        if expires_at <= time.monotonic():
            self.resolve_cache.pop(key, None)
            return None
        self.resolve_cache.move_to_end(key)
        return data

    def _store_cached_track_data(self, data: ResolvedTrackData) -> None:
        key = self._cache_key_for_data(data)
        expires_at = time.monotonic() + self.bot.settings.ytdlp_resolve_cache_ttl_seconds
        self.resolve_cache[key] = (expires_at, data)
        self.resolve_cache.move_to_end(key)
        while len(self.resolve_cache) > self.bot.settings.ytdlp_resolve_cache_size:
            self.resolve_cache.popitem(last=False)

    def _apply_resolved_track_data(self, track: Track, data: ResolvedTrackData) -> Track:
        track.title = data.title
        track.webpage_url = data.webpage_url
        track.stream_url = data.stream_url
        track.uploader = data.uploader
        track.duration = data.duration
        track.query = data.query
        track.resolved_at = data.resolved_at
        track.acodec = data.acodec
        track.abr = data.abr
        if data.thumbnail_url:
            track.thumbnail_url = data.thumbnail_url
        if data.tags:
            track.tags = data.tags
        return track

    async def _resolve_track_data(self, track: Track) -> ResolvedTrackData | None:
        cache_key = self._cache_key(track)
        cached = self._get_cached_track_data(cache_key)
        if cached is not None:
            return cached

        pending = self.resolve_tasks.get(cache_key)
        if pending is None:

            async def runner() -> ResolvedTrackData | None:
                tracks, _ = await self._extract_full_tracks(
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
                    acodec=resolved.acodec,
                    abr=resolved.abr,
                )
                self._store_cached_track_data(data)
                return data

            pending = asyncio.create_task(runner(), name=f"resolve:{cache_key[:60]}")
            self.resolve_tasks[cache_key] = pending

            def _on_done(t: asyncio.Task[Any]) -> None:
                self.resolve_tasks.pop(cache_key, None)
                if not t.cancelled() and t.exception() is not None:
                    self.logger.warning(
                        "Resolve task failed for %s (%s): %s",
                        cache_key,
                        track.title,
                        t.exception(),
                    )

            pending.add_done_callback(_on_done)

        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _resolve_track(self, track: Track) -> Track | None:
        if track.stream_url:
            return track
        data = await self._resolve_track_data(track)
        if data is None:
            return None
        return self._apply_resolved_track_data(track, data)

    async def _materialize_track(self, query: str, requester_id: int) -> Track | None:
        tracks, _ = await self._extract_tracks(query, requester_id=requester_id)
        if not tracks:
            return None
        return await self._resolve_track(tracks[0])

    def _kick_pipeline(self, guild_id: int) -> None:
        task = self._pipeline_tasks.get(guild_id)
        if task and not task.done():
            return
        self._pipeline_tasks[guild_id] = self._bg_task(
            self._url_pipeline(guild_id),
            name=f"url-pipeline-{guild_id}",
        )

    async def _url_pipeline(self, guild_id: int) -> None:
        try:
            player = self.players.get(guild_id)
            if player is None:
                return
            token = _CURRENT_GUILD_ID.set(guild_id)
            try:
                resolved_count = 0
                prefetch_depth = self.bot.settings.ytdlp_prefetch_count
                for track in list(itertools.islice(player.queue, prefetch_depth)):
                    if resolved_count >= prefetch_depth:
                        break
                    if track.stream_url:
                        age = time.monotonic() - track.resolved_at
                        if age < STREAM_URL_REFRESH_AGE_SECONDS:
                            resolved_count += 1
                            continue
                        track.stream_url = ""
                        track.resolved_at = 0.0
                    try:
                        await self._resolve_track(track)
                        resolved_count += 1
                        await asyncio.sleep(0)
                    except Exception as exc:
                        self.logger.debug(
                            "Pipeline resolve failed for %s (%s): %s",
                            track.webpage_url or track.title,
                            type(exc).__name__,
                            exc,
                        )
            finally:
                _CURRENT_GUILD_ID.reset(token)
            if resolved_count:
                self._persist_snapshot(guild_id)
        finally:
            self._pipeline_tasks.pop(guild_id, None)

    async def _safety_net_refresh(self, guild_id: int) -> None:
        player = self.players.get(guild_id)
        if player is None or not player.queue:
            return
        next_track = player.queue[0]
        if next_track.stream_url:
            age = time.monotonic() - next_track.resolved_at
            if age < STREAM_URL_REFRESH_AGE_SECONDS:
                return
            next_track.stream_url = ""
            next_track.resolved_at = 0.0
        try:
            token = _CURRENT_GUILD_ID.set(guild_id)
            try:
                resolved = await self._resolve_track(next_track)
            finally:
                _CURRENT_GUILD_ID.reset(token)
        except commands.BadArgument as exc:
            self.logger.warning("Safety-net refresh failed for guild %s: %s", guild_id, exc)
            return
        if resolved is not None:
            self._persist_snapshot(guild_id)
