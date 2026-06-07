"""cog.py — MusicCog: commands and event handlers."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
import logging
import math
import random
import re
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord
from discord.ext import commands
from yt_dlp import DownloadError, YoutubeDL

from musicbot.cogs.music.constants import (
    EMBED_COLOUR,
    FFMPEG_BEFORE_OPTIONS,
    FFMPEG_OPTIONS,
    LOOP_CYCLE,
    LOOP_ICONS,
    LOOP_LABELS,
    NOW_PLAYING_PREVIEW_LIMIT,
    NOW_PLAYING_TIMEOUT_SECONDS,
    NP_REFRESH_DEBOUNCE_SECONDS,
    PRESENCE_DEBOUNCE_SECONDS,
    QUEUE_PAGE_SIZE,
    SEARCH_SELECTION_LIMIT,
    NEAR_END_SAFETY_SECONDS,
    SNAPSHOT_DEBOUNCE_SECONDS,
    STREAM_URL_REFRESH_AGE_SECONDS,
    URL_PIPELINE_DEPTH,
    YTDL_OPTIONS,
)
from musicbot.cogs.music.models import (
    NowPlayingController,
    ResolvedTrackData,
    SearchDebugRecord,
    Track,
)
from musicbot.cogs.music.player import GuildPlayer
from musicbot.cogs.music.scoring import rank_entries
from musicbot.cogs.music.views import (
    NowPlayingView,
    QueueView,
    ScoreDebugView,
    SearchSelectionView,
)

if TYPE_CHECKING:
    from musicbot.bot import MusicBot

_CURRENT_GUILD_ID: ContextVar[int | None] = ContextVar("_CURRENT_GUILD_ID", default=None)

class MusicCog(commands.Cog):
    def __init__(self, bot: "MusicBot") -> None:
        self.bot    = bot
        self.logger = logging.getLogger(__name__)

        self.players: dict[int, GuildPlayer] = {}
        self.now_playing_messages: dict[int, NowPlayingController] = {}

        self._warned_missing_cookiefile = False
        self._ytdl_base_options: dict[str, Any] | None = None
        self._ytdl_variants: dict[tuple[bool, bool], dict[str, Any]] | None = None

        self._ytdl_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ytdlp")

        self._http_session: aiohttp.ClientSession | None = None

        self.resolve_tasks: dict[str, asyncio.Task[ResolvedTrackData | None]] = {}
        self.resolve_cache: OrderedDict[str, tuple[float, ResolvedTrackData]] = OrderedDict()

        self._pipeline_tasks: dict[int, asyncio.Task[None]] = {}

        self._snapshot_deadlines: dict[int, float] = {}
        self._snapshot_tasks:     dict[int, asyncio.Task[None]] = {}

        self._np_refresh_deadlines: dict[int, float] = {}
        self._np_refresh_tasks:     dict[int, asyncio.Task[None]] = {}

        self._presence_task: asyncio.Task[None] | None = None
        self._presence_deadline: float = 0.0

        self._guild_extract_semaphores: dict[int, asyncio.Semaphore] = {}
        self.extract_semaphore = asyncio.Semaphore(self.bot.settings.ytdlp_concurrent_extracts)

        self._last_search: OrderedDict[int, SearchDebugRecord] = OrderedDict()
        self._last_search_max = 50   # was 100; halved to reduce memory footprint

        self._ytdl_instances: dict[tuple[bool, bool], YoutubeDL] = {}

    async def cog_load(self) -> None:
        """Called by discord.py after the cog is added to the bot."""
        self._http_session = aiohttp.ClientSession()

    def cog_unload(self) -> None:
        self.now_playing_messages.clear()
        for player in self.players.values():
            self._bg_task(player.destroy(), name="cog-unload-destroy")
        for task_dict in (self._pipeline_tasks, self._snapshot_tasks, self._np_refresh_tasks):
            for task in task_dict.values():
                task.cancel()
        if self._presence_task and not self._presence_task.done():
            self._presence_task.cancel()
        if self._http_session and not self._http_session.closed:
            asyncio.create_task(self._http_session.close())
        self._ytdl_instances.clear()
        self._ytdl_executor.shutdown(wait=False)

    async def shutdown(self) -> None:
        for task in list(self._np_refresh_tasks.values()):
            task.cancel()
        for task in list(self._snapshot_tasks.values()):
            task.cancel()
        for guild_id in list(self.players):
            with contextlib.suppress(Exception):
                await self._flush_snapshot(guild_id)
        for player in list(self.players.values()):
            with contextlib.suppress(Exception):
                await player.destroy()

    def _build_ytdl_options(
        self, *, flat_playlist: bool = False, flat_search: bool = False
    ) -> dict[str, Any]:
        if self._ytdl_base_options is None:
            base = dict(YTDL_OPTIONS)
            base["socket_timeout"] = self.bot.settings.ytdlp_socket_timeout
            base["playlistend"]    = self.bot.settings.max_playlist_size
            if self.bot.settings.ytdlp_cookies_file:
                if self.bot.settings.ytdlp_cookies_file.exists():
                    base["cookiefile"] = str(self.bot.settings.ytdlp_cookies_file)
                elif not self._warned_missing_cookiefile:
                    self.logger.warning("YTDLP_COOKIES_FILE does not exist: %s",
                                        self.bot.settings.ytdlp_cookies_file)
                    self._warned_missing_cookiefile = True
            if self.bot.settings.ytdlp_js_runtime_path:
                base["js_runtimes"] = {"node": {"path": self.bot.settings.ytdlp_js_runtime_path}}
            self._ytdl_base_options = base
            fp  = dict(base); fp["extract_flat"] = "in_playlist"; fp["lazy_playlist"] = True
            fs  = dict(base); fs["extract_flat"] = True
            fps = dict(fp);   fps["extract_flat"] = True
            self._ytdl_variants = {
                (False, False): dict(base),
                (True,  False): fp,
                (False, True):  fs,
                (True,  True):  fps,
            }
        return self._ytdl_variants[(flat_playlist, flat_search)]

    async def _validate_stream_url(self, track: Track) -> bool:
        """Fix #2: uses shared session from cog_load — no per-call session creation."""
        url = track.stream_url
        if not url or not url.startswith("http"):
            return False
        session = self._http_session
        if session is None or session.closed:
            self._http_session = aiohttp.ClientSession()
            session = self._http_session
        try:
            async with session.head(
                url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True
            ) as resp:
                return resp.status < 400
        except Exception:
            return False


    async def _build_audio_source(self, track: Track) -> discord.AudioSource:
        try:
            source: discord.AudioSource = await discord.FFmpegOpusAudio.from_probe(
                track.stream_url, method="fallback",
                before_options=FFMPEG_BEFORE_OPTIONS, options=FFMPEG_OPTIONS,
            )
        except (discord.ClientException, OSError, TypeError, ValueError) as exc:
            self.logger.warning("Opus probe fallback for %s: %s", track.webpage_url, exc)
            try:
                source = discord.FFmpegOpusAudio(
                    track.stream_url,
                    bitrate=self.bot.settings.opus_bitrate_kbps,
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                )
            except (discord.ClientException, OSError, TypeError, ValueError) as exc2:
                self.logger.warning("Opus source fallback failed for %s: %s — skipping.",
                                    track.webpage_url, exc2)
                raise
        return source

    async def _extract_info(self, query: str, *, flat_playlist: bool = False, flat_search: bool = False) -> dict[str, Any]:
        key     = (flat_playlist, flat_search)
        options = self._build_ytdl_options(flat_playlist=flat_playlist, flat_search=flat_search)
        guild_id = _CURRENT_GUILD_ID.get()
        guild_sem = (
            self._guild_extract_semaphores.setdefault(guild_id, asyncio.Semaphore(1))
            if guild_id is not None else None
        )

        async def _do() -> dict[str, Any]:
            if guild_sem is not None:
                await guild_sem.acquire()
            try:
                async with self.extract_semaphore:
                    try:
                        loop = asyncio.get_running_loop()
                        ydl  = self._ytdl_instances.get(key)
                        if ydl is None:
                            ydl = YoutubeDL(options)
                            self._ytdl_instances[key] = ydl
                        qry  = query
                        result = await asyncio.wait_for(
                            loop.run_in_executor(
                                self._ytdl_executor,
                                lambda: ydl.extract_info(qry, download=False),
                            ),
                            timeout=self.bot.settings.ytdlp_extract_timeout_seconds,
                        )
                        if result is None:
                            raise commands.BadArgument(
                                "No information could be extracted for the provided source."
                            )
                        return result
                    except asyncio.TimeoutError as exc:
                        self.logger.warning("yt-dlp timed out for query %s", query)
                        raise commands.BadArgument(
                            f"Source lookup timed out after "
                            f"{self.bot.settings.ytdlp_extract_timeout_seconds} seconds."
                        ) from exc
            finally:
                if guild_sem is not None:
                    guild_sem.release()

        return await _do()

    def _is_playlist_query(self, query: str) -> bool:
        if not query.startswith(("http://", "https://")):
            return False
        return "list" in parse_qs(urlparse(query).query)

    def _playlist_entry_url(self, item: dict[str, Any]) -> str | None:
        for candidate in (item.get("webpage_url"), item.get("original_url"), item.get("url")):
            if not candidate:
                continue
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
            if item.get("ie_key") == "Youtube" or item.get("extractor_key") == "Youtube":
                return f"https://www.youtube.com/watch?v={candidate}"
        video_id = item.get("id")
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        return None

    def _item_thumbnail_url(self, item: dict[str, Any]) -> str:
        thumbnail = item.get("thumbnail")
        if isinstance(thumbnail, str) and thumbnail.startswith(("http://", "https://")):
            return thumbnail
        thumbnails = item.get("thumbnails")
        if isinstance(thumbnails, list):
            for candidate in reversed(thumbnails):
                if not isinstance(candidate, dict):
                    continue
                url = candidate.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url
        return ""

    def _search_result_track(self, item: dict[str, Any], requester_id: int) -> Track | None:
        webpage_url = self._playlist_entry_url(item)
        if not webpage_url:
            return None
        return Track(
            title=item.get("title", "Unknown title"),
            webpage_url=webpage_url,
            stream_url="",
            uploader=item.get("channel") or item.get("uploader") or "Search result",
            duration=int(item.get("duration") or 0),
            requester_id=requester_id,
            query=webpage_url,
            thumbnail_url=self._item_thumbnail_url(item),
        )

    async def _extract_playlist_tracks(
        self, query: str, requester_id: int
    ) -> tuple[list[Track], int]:
        try:
            info = await self._extract_info(query, flat_playlist=True)
        except commands.BadArgument:
            raise
        except DownloadError as exc:
            self.logger.warning("yt-dlp playlist scan failed for %s: %s", query, exc)
            raise commands.BadArgument(f"Failed to fetch media: {exc}") from exc
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return [], 0
        tracks: list[Track] = []
        skipped = 0
        for item in entries:
            if len(tracks) >= self.bot.settings.max_playlist_size:
                break
            if not item:
                skipped += 1
                continue
            webpage_url = self._playlist_entry_url(item)
            if not webpage_url:
                skipped += 1
                continue
            tracks.append(Track(
                title=item.get("title", "Unknown title"),
                webpage_url=webpage_url, stream_url="",
                uploader=item.get("channel") or item.get("uploader") or "Playlist item",
                duration=int(item.get("duration") or 0),
                requester_id=requester_id, query=webpage_url,
            ))
        return tracks, skipped

    async def _extract_single_track(
        self, item: dict[str, Any], query: str, requester_id: int
    ) -> Track | None:
        if "url" not in item and item.get("webpage_url"):
            try:
                item = await self._extract_info(item["webpage_url"])
            except DownloadError as exc:
                self.logger.warning("Skipping unplayable item %s: %s", item.get("webpage_url"), exc)
                return None
        stream_url  = item.get("url")
        webpage_url = item.get("webpage_url") or query
        if not stream_url:
            return None
        return Track(
            title=item.get("title", "Unknown title"),
            webpage_url=webpage_url, stream_url=stream_url,
            uploader=item.get("uploader", "Unknown uploader"),
            duration=int(item.get("duration") or 0),
            requester_id=requester_id, query=webpage_url,
            thumbnail_url=self._item_thumbnail_url(item),
            resolved_at=time.monotonic(),
            tags=list(item.get("tags") or []) + list(item.get("categories") or []),
        )

    async def _extract_full_tracks(
        self, query: str, requester_id: int
    ) -> tuple[list[Track], int]:
        try:
            info = await self._extract_info(query)
        except commands.BadArgument:
            raise
        except DownloadError as exc:
            self.logger.warning("yt-dlp failed for query %s: %s", query, exc)
            raise commands.BadArgument(f"Failed to fetch media: {exc}") from exc
        entries = info.get("entries") if isinstance(info, dict) else None
        info_items: list[dict[str, Any]]
        if entries:
            info_items = [e for e in entries if e][: self.bot.settings.max_playlist_size]
        elif isinstance(info, dict):
            info_items = [info]
        else:
            return [], 0
        tracks, skipped = [], 0
        for item in info_items:
            track = await self._extract_single_track(item, query, requester_id)
            if track is None:
                skipped += 1
                continue
            tracks.append(track)
        return tracks, skipped

    def _search_result_count(self, query: str) -> int:
        from musicbot.cogs.music.scoring import signal_tokens
        base = max(self.bot.settings.ytdlp_search_results, SEARCH_SELECTION_LIMIT)
        tokens = signal_tokens(query)
        if len(tokens) >= 4:
            return max(base, 8)
        if len(tokens) >= 3:
            return max(base, 6)
        return base

    def _search_text(self, query: str) -> str:
        match = re.match(r"^ytsearch(?:all|\d+)?:", query, flags=re.IGNORECASE)
        if not match:
            return query.strip()
        return query[match.end():].strip()

    def _preprocess_query(self, raw_query: str) -> str:
        if raw_query.startswith(("http://", "https://")) or raw_query.startswith("ytsearch"):
            return raw_query
        return re.sub(r"\s+", " ", raw_query).strip()

    def _normalize_query(self, query: str) -> str:
        query = self._preprocess_query(query)
        if query.startswith(("http://", "https://")) or query.startswith("ytsearch"):
            return query
        return f"ytsearch{self._search_result_count(query)}:{query}"

    async def _extract_search_candidates(
        self, query: str, requester_id: int, *, limit: int = SEARCH_SELECTION_LIMIT,
        curation_mode: bool = False,
    ) -> tuple[list[Track], int]:
        try:
            info = await self._extract_info(query, flat_search=True)
        except commands.BadArgument:
            raise
        except DownloadError as exc:
            self.logger.warning("yt-dlp search failed for %s: %s", query, exc)
            raise commands.BadArgument(f"Failed to fetch media: {exc}") from exc
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return [], 0
        guild_id    = _CURRENT_GUILD_ID.get()
        search_text = self._search_text(query)
        ranked_items = rank_entries(
            search_text, list(entries), guild_id,
            self._last_search, self._last_search_max,
            self._playlist_entry_url,
            curation_mode=curation_mode,
        )
        tracks: list[Track] = []
        skipped = 0
        for item in ranked_items:
            track = self._search_result_track(item, requester_id)
            if track is None:
                skipped += 1
                continue
            tracks.append(track)
            if len(tracks) >= limit:
                break
        return tracks, skipped

    async def _extract_search_tracks(
        self, query: str, requester_id: int
    ) -> tuple[list[Track], int]:
        return await self._extract_search_candidates(query, requester_id, limit=1)

    async def _extract_tracks(
        self, query: str, requester_id: int, *, guild_id: int | None = None,
        curation_mode: bool = False,
    ) -> tuple[list[Track], int]:
        token = _CURRENT_GUILD_ID.set(guild_id)
        try:
            if query.startswith("ytsearch"):
                return await self._extract_search_candidates(
                    query, requester_id, limit=1, curation_mode=curation_mode,
                )
            if self._is_playlist_query(query):
                return await self._extract_playlist_tracks(query, requester_id)
            return await self._extract_full_tracks(query, requester_id)
        finally:
            _CURRENT_GUILD_ID.reset(token)

    def _cache_key(self, track: Track) -> str:
        url = track.webpage_url
        if url and url.startswith(("http://", "https://")):
            return url
        return f"q:{track.query or track.title}"

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
        key        = self._cache_key_for_data(data)
        expires_at = time.monotonic() + self.bot.settings.ytdlp_resolve_cache_ttl_seconds
        self.resolve_cache[key] = (expires_at, data)
        self.resolve_cache.move_to_end(key)
        while len(self.resolve_cache) > self.bot.settings.ytdlp_resolve_cache_size:
            self.resolve_cache.popitem(last=False)

    def _cache_key_for_data(self, data: ResolvedTrackData) -> str:
        url = data.webpage_url
        if url and url.startswith(("http://", "https://")):
            return url
        return f"q:{data.query or data.title}"

    def _apply_resolved_track_data(self, track: Track, data: ResolvedTrackData) -> Track:
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
                    title=resolved.title, webpage_url=resolved.webpage_url,
                    stream_url=resolved.stream_url, uploader=resolved.uploader,
                    duration=resolved.duration, query=resolved.query,
                    resolved_at=resolved.resolved_at or time.monotonic(),
                    thumbnail_url=resolved.thumbnail_url, tags=resolved.tags,
                )
                self._store_cached_track_data(data)
                return data

            pending = asyncio.create_task(runner(), name=f"resolve:{cache_key[:60]}")
            self.resolve_tasks[cache_key] = pending

            def _on_done(t: asyncio.Task[Any]) -> None:
                self.resolve_tasks.pop(cache_key, None)
                if not t.cancelled() and t.exception() is not None:
                    self.logger.warning(
                        "Resolve task failed for %s: %s", cache_key, t.exception()
                    )
            pending.add_done_callback(_on_done)

        try:
            return await asyncio.shield(pending)
        except (asyncio.CancelledError, Exception):
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
        """Wake or restart the URL pipeline task for this guild."""
        task = self._pipeline_tasks.get(guild_id)
        if task and not task.done():
            return
        self._pipeline_tasks[guild_id] = self._bg_task(
            self._url_pipeline(guild_id), name=f"url-pipeline-{guild_id}"
        )

    async def _url_pipeline(self, guild_id: int) -> None:
        """Sequentially resolve the top URL_PIPELINE_DEPTH unresolved tracks.

        Runs to completion then exits — _kick_pipeline reschedules on demand.
        Skips tracks whose URL is still fresh (age < STREAM_URL_REFRESH_AGE_SECONDS).
        """
        try:
            player = self.players.get(guild_id)
            if player is None:
                return
            token = _CURRENT_GUILD_ID.set(guild_id)
            try:
                resolved_count = 0
                # Snapshot the queue to a list before iterating — the player
                # loop can popleft() the deque concurrently, causing
                # "RuntimeError: deque mutated during iteration".
                for track in list(itertools.islice(player.queue, URL_PIPELINE_DEPTH)):
                    if resolved_count >= URL_PIPELINE_DEPTH:
                        break
                    if track.stream_url:
                        age = time.monotonic() - track.resolved_at
                        if age < STREAM_URL_REFRESH_AGE_SECONDS:
                            resolved_count += 1
                            continue
                        track.stream_url  = ""
                        track.resolved_at = 0.0
                    try:
                        await self._resolve_track(track)
                        resolved_count += 1
                        await asyncio.sleep(0)   # yield to audio thread between resolves
                    except (commands.BadArgument, Exception) as exc:
                        self.logger.debug(
                            "Pipeline resolve failed for %s: %s", track.webpage_url, exc
                        )
            finally:
                _CURRENT_GUILD_ID.reset(token)
            if resolved_count:
                self._persist_snapshot(guild_id)
        finally:
            self._pipeline_tasks.pop(guild_id, None)

    async def _safety_net_refresh(self, guild_id: int) -> None:
        """Force-refresh position 0 URL if stale. Called by the near-end event."""
        player = self.players.get(guild_id)
        if player is None or not player.queue:
            return
        next_track = player.queue[0]
        if next_track.stream_url:
            age = time.monotonic() - next_track.resolved_at
            if age < STREAM_URL_REFRESH_AGE_SECONDS:
                return
            next_track.stream_url  = ""
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

    def _persist_snapshot(self, guild_id: int) -> None:
        """Schedule a debounced snapshot write via deadline timestamp."""
        self._snapshot_deadlines[guild_id] = time.monotonic() + SNAPSHOT_DEBOUNCE_SECONDS
        task = self._snapshot_tasks.get(guild_id)
        if task and not task.done():
            return  # long-lived task already running — it will pick up the new deadline
        self._snapshot_tasks[guild_id] = self._bg_task(
            self._snapshot_loop(guild_id), name=f"snapshot-{guild_id}"
        )

    async def _snapshot_loop(self, guild_id: int) -> None:
        """Run until there are no more pending writes for this guild."""
        try:
            while True:
                deadline = self._snapshot_deadlines.get(guild_id)
                if deadline is None:
                    break
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                # Deadline passed — write and clear.
                self._snapshot_deadlines.pop(guild_id, None)
                await self._write_snapshot(guild_id)
        finally:
            self._snapshot_tasks.pop(guild_id, None)

    async def _flush_snapshot(
        self, guild_id: int, *, entries: list[dict[str, Any]] | None = None
    ) -> None:
        task = self._snapshot_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._snapshot_deadlines.pop(guild_id, None)
        await self._write_snapshot(guild_id, entries=entries)

    def _snapshot_entries(self, guild_id: int) -> list[dict[str, Any]]:
        player = self.players.get(guild_id)
        if player is None:
            return []
        tracks: list[Track] = []
        if player.current:
            tracks.append(player.current)
        tracks.extend(player.queue)
        return [
            {
                "query":        t.query or t.webpage_url or t.title,
                "title":        t.title,
                "webpage_url":  t.webpage_url or "",
                "requester_id": str(t.requester_id),
            }
            for t in tracks
        ]

    async def _write_snapshot(
        self, guild_id: int, *, entries: list[dict[str, Any]] | None = None
    ) -> None:
        if not self.bot.database.is_open or getattr(self.bot, "_shutting_down", False):
            return
        snapshot = self._snapshot_entries(guild_id) if entries is None else entries
        await self.bot.database.save_queue_snapshot(guild_id, snapshot)

    def _schedule_np_refresh(
        self, guild_id: int, *, delay: float = NP_REFRESH_DEBOUNCE_SECONDS
    ) -> None:
        self._np_refresh_deadlines[guild_id] = time.monotonic() + delay
        task = self._np_refresh_tasks.get(guild_id)
        if task and not task.done():
            return
        self._np_refresh_tasks[guild_id] = self._bg_task(
            self._np_refresh_loop(guild_id), name=f"np-refresh-{guild_id}"
        )

    async def _np_refresh_loop(self, guild_id: int) -> None:
        try:
            while True:
                deadline = self._np_refresh_deadlines.get(guild_id)
                if deadline is None:
                    break
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                self._np_refresh_deadlines.pop(guild_id, None)
                await self._refresh_now_playing_message(guild_id)
        finally:
            self._np_refresh_tasks.pop(guild_id, None)

    def _controller(
        self, guild_id: int, *, message_id: int | None = None
    ) -> NowPlayingController | None:
        controller = self.now_playing_messages.get(guild_id)
        if controller is None:
            return None
        if controller.expires_at <= time.monotonic():
            self.now_playing_messages.pop(guild_id, None)
            return None
        if message_id is not None and controller.message_id != message_id:
            return None
        return controller

    @staticmethod
    def _format_duration(seconds: float) -> str:
        s = max(0, int(seconds))
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _format_progress_bar(self, elapsed: float, duration: float, *, width: int = 16) -> str:
        if duration <= 0:
            return f"`{'─' * width}`  Live"
        ratio  = min(1.0, max(0.0, elapsed / duration))
        filled = round(ratio * width)
        bar    = "▓" * filled + "░" * (width - filled)
        return f"`{bar}` {self._format_duration(elapsed)} / {self._format_duration(duration)}"

    def _queue_lines(
        self, player: GuildPlayer, *, limit: int, include_current: bool = True
    ) -> list[str]:
        lines: list[str] = []
        if include_current and player.current:
            lines.append(
                f"Now: `{player.current.escaped_title}` "
                f"[{player.current.duration_label}]"
            )
        for index, track in enumerate(itertools.islice(player.queue, limit), start=1):
            duration = track.duration_label if track.duration else "pending"
            lines.append(
                f"{index}. `{track.escaped_title}` [{duration}]"
            )
        if len(player.queue) > limit:
            lines.append(f"...and {len(player.queue) - limit} more.")
        return lines

    def _render_now_playing_embed(
        self,
        guild: discord.Guild,
        player: GuildPlayer | None,
        controller: NowPlayingController,
    ) -> discord.Embed:
        embed       = discord.Embed(colour=EMBED_COLOUR)
        footer_parts = ["⏮ prev", "⏭ skip", "⏯ pause", "↻ loop", "≡ queue"]
        footer = "  ·  ".join(footer_parts)
        if controller.status_text:
            footer = f"{footer}  ·  {controller.status_text}"

        if not player or not player.current:
            embed.title       = "Now Playing"
            embed.description = "Nothing is playing right now."
            embed.set_footer(text=footer)
            return embed

        track      = player.current
        is_paused  = bool(player.voice_client and player.voice_client.is_paused())
        loop_label = LOOP_LABELS.get(player.loop_mode, "Off")
        loop_icon  = LOOP_ICONS.get(player.loop_mode, "→")
        requester  = guild.get_member(track.requester_id)
        requester_label = requester.mention if requester else f"<@{track.requester_id}>"

        embed.title = "Now Playing" if not is_paused else "Paused"
        embed.add_field(
            name="Track",
            value=f"[{track.escaped_title}]({track.webpage_url})",
            inline=False,
        )
        embed.add_field(
            name=f"Progress — {'Paused' if is_paused else 'Playing'}",
            value=self._format_progress_bar(player.elapsed_seconds, track.duration),
            inline=False,
        )
        embed.add_field(name="Uploader",
                        value=track.escaped_uploader, inline=True)
        embed.add_field(name="Duration",
                        value=f"`{track.duration_label}`", inline=True)
        embed.add_field(name="Requested by", value=requester_label, inline=True)
        embed.add_field(name=f"Loop — {loop_icon}", value=f"`{loop_label}`", inline=True)

        preview_lines = self._queue_lines(
            player, limit=NOW_PLAYING_PREVIEW_LIMIT, include_current=False
        )
        embed.add_field(
            name="Up Next",
            value="\n".join(preview_lines) if preview_lines else "Nothing queued.",
            inline=False,
        )
        if track.thumbnail_url:
            embed.set_thumbnail(url=track.thumbnail_url)
        embed.set_footer(text=footer)
        return embed

    async def _fetch_announce_channel(
        self, guild: discord.Guild, player: GuildPlayer
    ) -> discord.abc.Messageable | None:
        if player.announce_channel_id is None:
            return None
        channel = self.bot.get_channel(player.announce_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(player.announce_channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    async def _send_now_playing_panel(
        self,
        guild: discord.Guild,
        player: GuildPlayer,
        *,
        channel: discord.abc.Messageable | None = None,
        replace_existing: bool = False,
        status_text: str = "",
    ) -> discord.Message | None:
        target_channel = channel or await self._fetch_announce_channel(guild, player)
        if target_channel is None:
            return None

        if replace_existing:
            existing = self._controller(guild.id)
            if existing and existing.message_id:
                ch = self.bot.get_channel(existing.channel_id)
                if ch and hasattr(ch, "get_partial_message"):
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await ch.get_partial_message(existing.message_id).delete()

        channel_id = getattr(target_channel, "id", None)
        if not isinstance(channel_id, int):
            return None

        view       = NowPlayingView(self, guild.id)
        controller = NowPlayingController(
            channel_id=channel_id, message_id=0,
            expires_at=time.monotonic() + NOW_PLAYING_TIMEOUT_SECONDS,
            status_text=status_text,
        )
        message = await target_channel.send(
            embed=self._render_now_playing_embed(guild, player, controller),
            view=view,
        )
        controller.message_id = message.id
        self.now_playing_messages[guild.id] = controller
        return message

    async def _refresh_now_playing_message(self, guild_id: int) -> None:
        controller = self._controller(guild_id)
        guild      = self.bot.get_guild(guild_id)
        if controller is None or guild is None:
            return
        channel = self.bot.get_channel(controller.channel_id)
        if channel is None or not hasattr(channel, "get_partial_message"):
            return
        player = self.players.get(guild_id)

        elapsed_bucket = int(player.elapsed_seconds // 4) if player else 0
        queue_preview  = tuple(t.title for t in itertools.islice(player.queue, NOW_PLAYING_PREVIEW_LIMIT)) if player else ()
        current_title  = player.current.title if player and player.current else ""
        loop_mode      = player.loop_mode if player else "off"
        is_paused      = bool(player and player.voice_client and player.voice_client.is_paused())
        state_key      = (current_title, elapsed_bucket, queue_preview, loop_mode, is_paused, controller.status_text)
        if getattr(controller, "_last_render_key", None) == state_key:
            return
        controller._last_render_key = state_key  # type: ignore[attr-defined]

        embed = self._render_now_playing_embed(guild, player, controller)
        partial = channel.get_partial_message(controller.message_id)
        with contextlib.suppress(discord.HTTPException, discord.NotFound):
            await partial.edit(embed=embed)

    async def _update_bot_presence(self) -> None:
        self._presence_deadline = time.monotonic() + PRESENCE_DEBOUNCE_SECONDS
        if self._presence_task and not self._presence_task.done():
            return
        self._presence_task = self._bg_task(
            self._presence_loop(), name="presence-update"
        )

    async def _presence_loop(self) -> None:
        try:
            while True:
                remaining = self._presence_deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                break
        except asyncio.CancelledError:
            return
        active = [
            p for p in self.players.values()
            if p.current and p.voice_client and p.voice_client.is_playing()
        ]
        with contextlib.suppress(Exception):
            if not active:
                await self.bot.change_presence(activity=discord.Activity(
                    type=discord.ActivityType.watching, name="pylxyr.github.io/PyxeeBot",
                ))
            elif len(active) == 1:
                await self.bot.change_presence(activity=discord.Activity(
                    type=discord.ActivityType.listening, name=active[0].current.title[:128],
                ))
            else:
                await self.bot.change_presence(activity=discord.Activity(
                    type=discord.ActivityType.listening, name=f"music in {len(active)} servers",
                ))

    def _bg_task(self, coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        def _on_done(t: asyncio.Task[Any]) -> None:
            if not t.cancelled() and t.exception() is not None:
                self.logger.exception("Background task %r raised", t.get_name(),
                                      exc_info=t.exception())
        task.add_done_callback(_on_done)
        return task

    async def _get_player(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if not player:
            player = await GuildPlayer.create(
                self.bot, guild,
                self._resolve_track,
                self._build_audio_source,
                self._validate_stream_url,
            )
            self.players[guild.id] = player
            await self._restore_snapshot(player)
        return player

    async def _restore_snapshot(self, player: GuildPlayer) -> None:
        rows = await self.bot.database.load_queue_snapshot(player.guild.id)
        if player.queue:
            return
        restored = [
            Track(
                title=row["title"], webpage_url=row["webpage_url"],
                stream_url="", uploader="Restored queue",
                duration=0, requester_id=int(row["requester_id"]),
                query=row["query"],
            )
            for row in rows
        ]
        player.replace_queue(restored)
        if restored:
            self._bg_task(
                self._warmup_restore(list(restored[:2]), guild_id=player.guild.id),
                name="warmup-restore",
            )

    async def _warmup_restore(self, tracks: list[Track], *, guild_id: int) -> None:
        sem = self._guild_extract_semaphores.setdefault(guild_id, asyncio.Semaphore(1))
        for track in tracks:
            async with sem:
                with contextlib.suppress(Exception):
                    await self._resolve_track(track)

    async def _ensure_author_voice(
        self, context: commands.Context[Any]
    ) -> discord.VoiceChannel | discord.StageChannel:
        voice_state = context.author.voice
        if not voice_state or not voice_state.channel:
            raise commands.BadArgument("Join a voice channel first.")
        return voice_state.channel

    def _voice_humans(self, channel: discord.abc.GuildChannel) -> list[discord.Member]:
        return [m for m in getattr(channel, "members", []) if not m.bot]

    def _is_bot_owner(self, user: discord.User | discord.Member) -> bool:
        return user.id in self.bot.settings.bot_owners or (
            self.bot.owner_id is not None and user.id == self.bot.owner_id
        )

    async def _is_dj(self, member: discord.Member) -> bool:
        if self._is_bot_owner(member):
            return True
        if member.guild_permissions.manage_guild:
            return True
        role_id = await self.bot.database.get_dj_role_id(member.guild.id)
        return bool(role_id and any(r.id == role_id for r in member.roles))

    async def _require_dj(self, context: commands.Context[Any]) -> None:
        if not await self._is_dj(context.author):
            raise commands.CheckFailure("DJ role or Manage Server permission required.")

    async def _join_for_context(self, context: commands.Context[Any]) -> GuildPlayer:
        channel = await self._ensure_author_voice(context)
        player  = await self._get_player(context.guild)
        self._remember_channel(player, context.channel)
        await player.connect(channel)
        return player

    def _remember_channel(self, player: GuildPlayer, channel: discord.abc.Messageable) -> None:
        channel_id = getattr(channel, "id", None)
        if isinstance(channel_id, int):
            player.set_announce_channel(channel_id)

    def _required_skip_votes(self, player: GuildPlayer) -> int:
        if not player.voice_client or not player.voice_client.channel:
            return 1
        return max(1, math.ceil(len(self._voice_humans(player.voice_client.channel)) / 2))

    def _is_in_player_voice(self, player: GuildPlayer, member: discord.Member) -> bool:
        return bool(
            player.voice_client
            and player.voice_client.channel
            and member in player.voice_client.channel.members
        )

    def _build_queue_view(
        self, guild_id: int, player: GuildPlayer, *, author_id: int, page: int = 0
    ) -> QueueView:
        return QueueView(self, guild_id, player, author_id=author_id, page_index=page)

    async def _prompt_for_search_selection(
        self,
        context: commands.Context[Any],
        query: str,
        candidates: list[Track],
        *,
        mode: str,
    ) -> Track | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        view = SearchSelectionView(
            author_id=context.author.id, candidates=candidates, mode=mode,
            query_text=self._search_text(query), prefix=context.clean_prefix,
            bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
            guild_icon_url=context.guild.icon.url if context.guild and context.guild.icon else None,
        )
        prompt = await context.send(embed=view.build_embed(), view=view)
        view.message = prompt
        return await view.wait_for_selection()

    async def _skip_for_member(self, player: GuildPlayer, member: discord.Member) -> str:
        if not player.current or not player.voice_client or not player.voice_client.channel:
            return "Nothing is playing."
        if not self._is_in_player_voice(player, member):
            return "Join my voice channel to vote skip."
        if player.current.requester_id == member.id or await self._is_dj(member):
            player.skip_votes.clear()
            player.skip()
            return "Skipped the current track."
        player.skip_votes.add(member.id)
        needed        = self._required_skip_votes(player)
        current_votes = len(player.skip_votes)
        if current_votes >= needed:
            player.skip_votes.clear()
            player.skip()
            return f"Skip vote passed with `{current_votes}` votes."
        return f"Skip vote added. `{current_votes}/{needed}` votes."

    async def _previous_for_member(self, player: GuildPlayer, member: discord.Member) -> str:
        if not self._is_in_player_voice(player, member):
            return "Join my voice channel first."
        if not await self._is_dj(member) and (
            not player.current or player.current.requester_id != member.id
        ):
            return "Only the current requester or a DJ can go to the previous track."
        if not player.play_previous():
            return "There is no previous track to return to."
        return "Returned to the previous track."

    async def _toggle_pause_for_member(self, player: GuildPlayer, member: discord.Member) -> str:
        if not self._is_in_player_voice(player, member):
            return "Join my voice channel first."
        if not player.voice_client:
            return "Nothing is connected."
        if player.voice_client.is_paused():
            player.resume()
            return "Resumed playback."
        if player.voice_client.is_playing():
            player.pause()
            return "Paused playback."
        return "Nothing is playing."

    async def _toggle_loop_for_member(self, player: GuildPlayer, member: discord.Member) -> str:
        if not self._is_in_player_voice(player, member):
            return "Join my voice channel first."
        if not await self._is_dj(member):
            return "DJ role or Manage Server permission required."
        if not player.current and not player.queue:
            return "Nothing is loaded."
        prev_label      = LOOP_LABELS.get(player.loop_mode, "Off")
        player.loop_mode = LOOP_CYCLE.get(player.loop_mode, "off")
        self._persist_snapshot(member.guild.id)
        label = LOOP_LABELS.get(player.loop_mode, "Off")
        icon  = LOOP_ICONS.get(player.loop_mode, "→")
        return f"Loop changed: **{prev_label}** → {icon} **{label}**"

    @commands.Cog.listener()
    async def on_musicbot_np_auto_refresh(self, guild: discord.Guild) -> None:
        await self._refresh_now_playing_message(guild.id)

    @commands.Cog.listener()
    async def on_musicbot_track_skipped_error(
        self, guild: discord.Guild, track: Track, reason: str
    ) -> None:
        if not self.bot.settings.error_announce:
            return
        player  = self.players.get(guild.id)
        channel = await self._fetch_announce_channel(guild, player) if player else None
        if channel is None:
            channel = guild.system_channel
        if channel:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(
                    f"Skipped **{track.escaped_title}** — {reason}"
                )

    @commands.Cog.listener()
    async def on_musicbot_playback_error(
        self, guild: discord.Guild, error: Exception
    ) -> None:
        player  = self.players.get(guild.id)
        channel = await self._fetch_announce_channel(guild, player) if player else None
        if channel is None:
            channel = guild.system_channel
        if channel and self.bot.settings.error_announce:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(f"Playback error: `{error}`")

    @commands.Cog.listener()
    async def on_musicbot_track_started(self, guild: discord.Guild, track: Track) -> None:
        player = self.players.get(guild.id)
        if player is None or player.current is None:
            return
        await self._update_bot_presence()
        await self._send_now_playing_panel(
            guild, player, replace_existing=True, status_text="Track changed."
        )

    @commands.Cog.listener()
    async def on_musicbot_queue_updated(self, guild: discord.Guild) -> None:
        player = self.players.get(guild.id)
        if player and not player.current and not player.queue:
            await self._update_bot_presence()
        self._persist_snapshot(guild.id)
        self._kick_pipeline(guild.id)
        self._schedule_np_refresh(guild.id)

    @commands.Cog.listener()
    async def on_musicbot_track_near_end(self, guild: discord.Guild) -> None:
        player = self.players.get(guild.id)
        if player is None or player.loop_mode == "one":
            return
        await self._safety_net_refresh(guild.id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        player = self.players.pop(guild.id, None)
        if player:
            await player.destroy()
        self._guild_extract_semaphores.pop(guild.id, None)
        await self._flush_snapshot(guild.id, entries=[])
        for task_dict in (self._snapshot_tasks, self._np_refresh_tasks, self._pipeline_tasks):
            task = task_dict.pop(guild.id, None)
            if task and not task.done():
                task.cancel()
        self.now_playing_messages.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        player = self.players.get(member.guild.id)
        if not player or not player.voice_client or not player.voice_client.channel:
            return
        tracked_channel = player.voice_client.channel
        if self.bot.user is not None and member.id == self.bot.user.id:
            if before.channel is not None and after.channel is None:
                await player.destroy()
                self.players.pop(member.guild.id, None)
                self._guild_extract_semaphores.pop(member.guild.id, None)
                await self._flush_snapshot(member.guild.id, entries=[])
                await self._refresh_now_playing_message(member.guild.id)
                await self._update_bot_presence()
            elif after.channel is not None and after.channel != before.channel:
                player.voice_client = member.guild.voice_client  # type: ignore[assignment]
                await player.refresh_empty_channel_state()
            return
        if before.channel == tracked_channel or after.channel == tracked_channel:
            await player.refresh_empty_channel_state()

    @commands.hybrid_command(name="join", aliases=["summon"])
    @commands.guild_only()
    async def join(self, context: commands.Context[Any]) -> None:
        """Dock into your current voice channel."""
        player = await self._join_for_context(context)
        if player.queue:
            self._kick_pipeline(context.guild.id)
        await context.send("Connected to your voice channel.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="leave", aliases=["disconnect"])
    @commands.guild_only()
    async def leave(self, context: commands.Context[Any]) -> None:
        """Disconnect and wipe the active session."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player:
            await context.send("I am not connected.")
            return
        gid = context.guild.id
        await player.destroy()
        self.players.pop(gid, None)
        await self._flush_snapshot(gid, entries=[])
        for task_dict in (self._snapshot_tasks, self._np_refresh_tasks, self._pipeline_tasks):
            task = task_dict.pop(gid, None)
            if task and not task.done():
                task.cancel()
        self._guild_extract_semaphores.pop(gid, None)
        self.now_playing_messages.pop(gid, None)
        await context.send("Disconnected and cleared the queue.")
        await self._refresh_now_playing_message(gid)

    @commands.hybrid_command(name="history")
    @commands.guild_only()
    async def history(self, context: commands.Context[Any]) -> None:
        """Show the last tracks that were played this session."""
        player = self.players.get(context.guild.id)
        hist   = list(player.history) if player else []
        if not hist:
            await context.send("No tracks have been played this session.")
            return
        lines = [
            f"`{i}.` [{discord.utils.escape_markdown(t.title)}]({t.webpage_url})"
            f" — <@{t.requester_id}>"
            for i, t in enumerate(reversed(hist), start=1)
        ]
        embed = discord.Embed(
            title="Recent History",
            description="\n".join(lines[:20]),
            colour=EMBED_COLOUR,
        )
        embed.set_footer(text=f"{len(hist)} track(s) in session history.")
        await context.send(embed=embed)

    @commands.hybrid_command(name="why", aliases=["searchdebug", "scorewhy"])
    @commands.guild_only()
    async def why(self, context: commands.Context[Any]) -> None:
        """Show how the last search's results were scored."""
        record = self._last_search.get(context.guild.id)
        if record is None:
            await context.send("No search has been run this session. Use `!play <query>` first.")
            return
        stale_suffix = ""
        age_seconds = time.monotonic() - record.timestamp
        if age_seconds > 300:
            mins = int(age_seconds // 60)
            stale_suffix = f"\n> ⚠️ This breakdown is {mins}m old — run a new search for fresh data."
        embed = discord.Embed(
            title=f"Score breakdown — `{discord.utils.escape_markdown(record.query_text)}`",
            colour=EMBED_COLOUR,
        )
        lines: list[str] = []
        for c in record.candidates:
            sel       = "  ✓" if c.selected else ""
            dur_m, dur_s = divmod(c.duration, 60)
            dur_label = f"{dur_m}:{dur_s:02d}" if c.duration else "?"
            title_short = discord.utils.escape_markdown(c.title[:52])
            detail = (
                f"title={c.title_overlap:.2f} artist={c.uploader_overlap:.2f} "
                f"anchor={c.anchor_score:+.2f} jp={c.jp_original_bonus:+.2f} "
                f"views={c.view_bonus:+.2f} penalty={-c.discouraged_penalty:+.2f}"
            )
            lines.append(
                f"`#{c.rank}` **{c.final_score:+.3f}**{sel} "
                f"[{title_short}]({c.webpage_url})\n└ `{dur_label}` · {detail}"
            )
        embed.description = "\n\n".join(lines) + stale_suffix if lines else "No data."
        embed.set_footer(text="Press the button for a full per-component DM breakdown.")
        view = ScoreDebugView(author_id=context.author.id, record=record)
        await context.send(embed=embed, view=view)

    @commands.hybrid_command(name="skipto")
    @commands.guild_only()
    async def skipto(self, context: commands.Context[Any], position: int) -> None:
        """Skip ahead to a specific queue position, dropping everything before it."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is empty.")
            return
        self._remember_channel(player, context.channel)
        size = len(player.queue)
        if position < 1 or position > size:
            await context.send(f"Position `{position}` is out of range (queue has {size} tracks).")
            return
        if position == 1:
            await context.send("That is already the next track. Use `!skip` to skip the current one.")
            return
        queue_list    = list(player.queue)
        dropped       = position - 1
        player.replace_queue(queue_list[position - 1:])
        self._persist_snapshot(context.guild.id)
        target = player.queue[0]
        embed  = discord.Embed(title="Jumped to Position", colour=EMBED_COLOUR)
        embed.add_field(
            name="Now Up Next",
            value=f"[{discord.utils.escape_markdown(target.title)}]({target.webpage_url})",
            inline=False,
        )
        embed.add_field(name="Position", value=f"`{position}`", inline=True)
        embed.add_field(name="Dropped",  value=f"`{dropped}` track{'s' if dropped != 1 else ''}", inline=True)
        if target.thumbnail_url:
            embed.set_thumbnail(url=target.thumbnail_url)
        await context.send(embed=embed)

    @commands.hybrid_command(name="replay")
    @commands.guild_only()
    async def replay(self, context: commands.Context[Any]) -> None:
        """Re-queue the current track so it plays again after the queue ends."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or not player.current:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        if len(player.queue) >= self.bot.settings.max_queue_size:
            await context.send("Queue is full.")
            return
        clone = dataclasses.replace(player.current, requester_id=context.author.id)
        clone.stream_url  = ""
        clone.resolved_at = 0.0
        await player.enqueue(clone, front=True)
        self._persist_snapshot(context.guild.id)
        await self._refresh_now_playing_message(context.guild.id)
        await context.send(
            f"Re-queued **{player.current.escaped_title}** to play next."
        )

    @commands.hybrid_command(name="qsearch", aliases=["qs"])
    @commands.guild_only()
    async def qsearch(self, context: commands.Context[Any], *, keyword: str) -> None:
        """Search within the current queue."""
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is empty.")
            return
        kw = keyword.strip().lower()
        matches = [
            (i + 1, track)
            for i, track in enumerate(player.queue)
            if kw in track.title.lower() or kw in (track.uploader or "").lower()
        ]
        if not matches:
            await context.send(f"No tracks in the queue matching `{discord.utils.escape_markdown(keyword)}`.")
            return
        lines = [
            f"`{pos}.` [{discord.utils.escape_markdown(t.title)}]({t.webpage_url})"
            for pos, t in matches[:20]
        ]
        embed = discord.Embed(
            title=f"Queue Search: {discord.utils.escape_markdown(keyword)}",
            description="\n".join(lines), colour=EMBED_COLOUR,
        )
        footer = f"Showing first 20 of {len(matches)} matches." if len(matches) > 20 else f"{len(matches)} match(es) found."
        embed.set_footer(text=footer)
        await context.send(embed=embed)

    @commands.hybrid_command(name="play", aliases=["p"])
    @commands.guild_only()
    async def play(self, context: commands.Context[Any], *, query: str) -> None:
        """Queue a URL, playlist, or search query. Searches go direct via YouTube Music."""
        player = await self._join_for_context(context)
        self._kick_pipeline(context.guild.id)
        if len(player.queue) >= self.bot.settings.max_queue_size:
            await context.send("Queue is full. Clear or play tracks before adding more.")
            return
        query  = self._normalize_query(query)
        is_url = query.startswith(("http://", "https://"))

        is_playlist = self._is_playlist_query(query)
        if is_playlist:
            fetch_msg: discord.Message | None = await context.send("⏳ Loading playlist…")
        elif is_url:
            fetch_msg = await context.send("🔍 Fetching…")
        else:
            fetch_msg = await context.send("🔍 Searching…")

        async with context.typing():
            tracks, skipped = await self._extract_tracks(
                query, requester_id=context.author.id, guild_id=context.guild.id,
            )
        if not tracks:
            msg = (
                f"No playable results found. Skipped `{skipped}` unavailable items."
                if skipped
                else "No playable results found. Try `!search <query>` to browse manually."
            )
            if fetch_msg:
                await fetch_msg.edit(content=msg)
            else:
                await context.send(msg)
            return
        added = 0
        for track in tracks:
            if len(player.queue) >= self.bot.settings.max_queue_size:
                break
            await player.enqueue(track)
            added += 1
        self._persist_snapshot(context.guild.id)
        self._kick_pipeline(context.guild.id)
        await self._refresh_now_playing_message(context.guild.id)
        suffix = f" Skipped `{skipped}` unavailable items." if skipped else ""
        result = (
            f"Queued [{tracks[0].escaped_title}]({tracks[0].webpage_url}).{suffix}"
            if added == 1
            else f"Queued `{added}` tracks.{suffix}"
        )
        if fetch_msg:
            await fetch_msg.edit(content=result)
        else:
            await context.send(result)

    @commands.hybrid_command(name="playnext", aliases=["pn"])
    @commands.guild_only()
    async def playnext(self, context: commands.Context[Any], *, query: str) -> None:
        """Insert a track next in queue."""
        await self._require_dj(context)
        player = await self._join_for_context(context)
        query  = self._normalize_query(query)
        fetch_msg = await context.send("🔍 Searching…")
        async with context.typing():
            tracks, _ = await self._extract_tracks(
                query, requester_id=context.author.id, guild_id=context.guild.id,
            )
        track = tracks[0] if tracks else None
        if track is None:
            await fetch_msg.edit(content="No playable result found.")
            return
        await player.enqueue(track, front=True)
        self._persist_snapshot(context.guild.id)
        self._kick_pipeline(context.guild.id)
        await self._refresh_now_playing_message(context.guild.id)
        await fetch_msg.edit(
            content=f"Queued next: [{track.escaped_title}]({track.webpage_url})."
        )

    @commands.hybrid_command(name="repeat", aliases=["rp"])
    @commands.guild_only()
    async def repeat(self, context: commands.Context[Any]) -> None:
        """Toggle repeat for the current track. Shortcut for !loop one / !loop off."""
        player = self.players.get(context.guild.id)
        if not player or not player.current:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        prev_label       = LOOP_LABELS.get(player.loop_mode, "Off")
        player.loop_mode = "off" if player.loop_mode == "one" else "one"
        self._persist_snapshot(context.guild.id)
        label = LOOP_LABELS.get(player.loop_mode, "Off")
        icon  = LOOP_ICONS.get(player.loop_mode, "→")
        await context.send(f"Loop changed: **{prev_label}** → {icon} **{label}**")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="search", aliases=["find", "s"])
    @commands.guild_only()
    async def search(self, context: commands.Context[Any], *, query: str) -> None:
        """Browse search results and pick one to queue."""
        player = await self._join_for_context(context)
        if len(player.queue) >= self.bot.settings.max_queue_size:
            await context.send("Queue is full.")
            return
        self._remember_channel(player, context.channel)
        search_query = f"ytsearch{self._search_result_count(query)}:{self._preprocess_query(query)}"
        async with context.typing():
            tracks, _ = await self._extract_search_candidates(
                search_query, requester_id=context.author.id
            )
        selected = await self._prompt_for_search_selection(context, search_query, tracks, mode="play")
        if selected is None:
            if not tracks:
                await context.send("No results found.")
            return
        await player.enqueue(selected)
        self._persist_snapshot(context.guild.id)
        self._kick_pipeline(context.guild.id)
        await self._refresh_now_playing_message(context.guild.id)
        await context.send(
            f"Queued [{discord.utils.escape_markdown(selected.title)}]({selected.webpage_url})."
        )

    @commands.hybrid_command(name="skip", aliases=["next"])
    @commands.guild_only()
    async def skip(self, context: commands.Context[Any]) -> None:
        """Vote-skip or instantly skip if you have control."""
        player = self.players.get(context.guild.id)
        if not player:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        message = await self._skip_for_member(player, context.author)
        await context.send(message)
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="prev", aliases=["previous", "back"])
    @commands.guild_only()
    async def previous(self, context: commands.Context[Any]) -> None:
        """Jump back to the last completed track."""
        player = self.players.get(context.guild.id)
        if not player:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        message = await self._previous_for_member(player, context.author)
        await context.send(message)
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="forceskip", aliases=["fs"])
    @commands.guild_only()
    async def forceskip(self, context: commands.Context[Any]) -> None:
        """DJ-only immediate skip."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or not player.current:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        player.skip()
        await context.send("Force skipped the current track.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="stop")
    @commands.guild_only()
    async def stop(self, context: commands.Context[Any]) -> None:
        """Stop playback and drop loop mode."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player:
            await context.send("Nothing to stop.")
            return
        self._remember_channel(player, context.channel)
        await player.stop()
        self._persist_snapshot(context.guild.id)
        await context.send("Stopped playback and cleared loop mode.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="pause")
    @commands.guild_only()
    async def pause(self, context: commands.Context[Any]) -> None:
        """Freeze playback in place."""
        player = self.players.get(context.guild.id)
        if not player or not player.voice_client or not player.voice_client.is_playing():
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        if not self._is_in_player_voice(player, context.author):
            await context.send("Join my voice channel first.")
            return
        player.pause()
        await context.send("Paused playback.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="resume")
    @commands.guild_only()
    async def resume(self, context: commands.Context[Any]) -> None:
        """Resume the paused track."""
        player = self.players.get(context.guild.id)
        if not player or not player.voice_client or not player.voice_client.is_paused():
            await context.send("Nothing is paused.")
            return
        self._remember_channel(player, context.channel)
        if not self._is_in_player_voice(player, context.author):
            await context.send("Join my voice channel first.")
            return
        player.resume()
        await context.send("Resumed playback.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="nowplaying", aliases=["np"])
    @commands.guild_only()
    async def nowplaying(self, context: commands.Context[Any]) -> None:
        """Open the live control panel with buttons."""
        player = self.players.get(context.guild.id)
        if player:
            self._remember_channel(player, context.channel)
            await self._send_now_playing_panel(
                context.guild, player, channel=context.channel, replace_existing=True
            )
            return
        controller = NowPlayingController(
            channel_id=context.channel.id, message_id=0,
            expires_at=time.monotonic() + NOW_PLAYING_TIMEOUT_SECONDS,
        )
        await context.send(embed=self._render_now_playing_embed(context.guild, None, controller))

    @commands.hybrid_command(name="queue", aliases=["q"])
    @commands.guild_only()
    async def queue(self, context: commands.Context[Any]) -> None:
        """Inspect the current track stack."""
        player = self.players.get(context.guild.id)
        if not player or (not player.current and not player.queue):
            await context.send("Queue is empty.")
            return
        self._remember_channel(player, context.channel)
        view    = self._build_queue_view(context.guild.id, player, author_id=context.author.id)
        message = await context.send(embed=view.build_embed(), view=view)
        if isinstance(message, discord.Message):
            view.message = message

    @commands.hybrid_command(name="remove")
    @commands.guild_only()
    async def remove(self, context: commands.Context[Any], index: int) -> None:
        """Pull one queued track by index."""
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is empty.")
            return
        self._remember_channel(player, context.channel)
        if index < 1 or index > len(player.queue):
            raise commands.BadArgument("Queue index is out of range.")
        queue_list = list(player.queue)
        removed    = queue_list[index - 1]
        if removed.requester_id != context.author.id and not await self._is_dj(context.author):
            raise commands.CheckFailure("Only the requester or a DJ can remove this track.")
        queue_list.pop(index - 1)
        player.replace_queue(queue_list)
        self._persist_snapshot(context.guild.id)
        await context.send(f"Removed **{removed.escaped_title}** from the queue.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="clear")
    @commands.guild_only()
    async def clear(self, context: commands.Context[Any]) -> None:
        """Flush the queued tracks."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is already empty.")
            return
        self._remember_channel(player, context.channel)
        player.queue.clear()
        self._persist_snapshot(context.guild.id)
        await context.send("Cleared the queue.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="shuffle")
    @commands.guild_only()
    async def shuffle(self, context: commands.Context[Any]) -> None:
        """Randomize the upcoming queue."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or len(player.queue) < 2:
            await context.send("Need at least two queued tracks to shuffle.")
            return
        self._remember_channel(player, context.channel)
        shuffled = list(player.queue)
        random.shuffle(shuffled)
        player.replace_queue(shuffled)
        self._persist_snapshot(context.guild.id)
        await context.send("Shuffled the queue.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="move")
    @commands.guild_only()
    async def move(self, context: commands.Context[Any], from_index: int, to_index: int) -> None:
        """Move a track from one queue position to another. e.g. !move 10 2"""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is empty.")
            return
        self._remember_channel(player, context.channel)
        size = len(player.queue)
        if from_index < 1 or from_index > size:
            await context.send(f"Position `{from_index}` is out of range (queue has {size} tracks).")
            return
        if to_index < 1 or to_index > size:
            await context.send(f"Position `{to_index}` is out of range (queue has {size} tracks).")
            return
        if from_index == to_index:
            await context.send("Source and destination are the same position.")
            return
        queue_list = list(player.queue)
        track      = queue_list.pop(from_index - 1)
        queue_list.insert(to_index - 1, track)
        player.replace_queue(queue_list)
        self._persist_snapshot(context.guild.id)
        await context.send(
            f"Moved **{track.escaped_title}** "
            f"from position `{from_index}` to `{to_index}`."
        )
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="loop")
    @commands.guild_only()
    async def loop(self, context: commands.Context[Any]) -> None:
        """Cycle loop mode: off → single track → full queue → off."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or (not player.current and not player.queue):
            await context.send("Nothing is loaded.")
            return
        self._remember_channel(player, context.channel)
        prev_label       = LOOP_LABELS.get(player.loop_mode, "Off")
        player.loop_mode = LOOP_CYCLE.get(player.loop_mode, "off")
        self._persist_snapshot(context.guild.id)
        label = LOOP_LABELS.get(player.loop_mode, "Off")
        icon  = LOOP_ICONS.get(player.loop_mode, "→")
        await context.send(f"Loop changed: **{prev_label}** → {icon} **{label}**")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_group(name="playlist", invoke_without_command=True)
    @commands.guild_only()
    async def playlist(self, context: commands.Context[Any]) -> None:
        """Work with saved server playlists."""
        await context.send(
            "Use `playlist save`, `playlist load`, `playlist list`, "
            "`playlist show`, or `playlist delete`."
        )

    @playlist.command(name="save")
    @commands.guild_only()
    async def playlist_save(self, context: commands.Context[Any], name: str) -> None:
        """Save the current queue as a named playlist."""
        player = self.players.get(context.guild.id)
        if not player or (not player.current and not player.queue):
            await context.send("Nothing is loaded to save.")
            return
        entries = player.snapshot()
        await self.bot.database.save_playlist(
            context.guild.id, name.lower(), context.author.id, entries
        )
        await context.send(f"Saved `{len(entries)}` tracks to playlist `{name.lower()}`.")

    @playlist.command(name="list")
    @commands.guild_only()
    async def playlist_list(self, context: commands.Context[Any]) -> None:
        """List all saved playlists for this server."""
        rows = await self.bot.database.list_playlists(context.guild.id)
        if not rows:
            await context.send("No saved playlists for this server.")
            return
        PAGE       = 25
        page_count = math.ceil(len(rows) / PAGE)
        for page in range(page_count):
            chunk = rows[page * PAGE: (page + 1) * PAGE]
            lines = [
                f"`{row['name']}` — {row['track_count']} tracks — <@{row['created_by']}>"
                for row in chunk
            ]
            title = "Saved Playlists" if page_count == 1 else f"Saved Playlists (page {page+1}/{page_count})"
            embed = discord.Embed(title=title, description="\n".join(lines), colour=EMBED_COLOUR)
            embed.set_footer(text=f"{len(rows)} playlist(s) total")
            await context.send(embed=embed)

    @playlist.command(name="show")
    @commands.guild_only()
    async def playlist_show(self, context: commands.Context[Any], name: str) -> None:
        """Show the tracks in a saved playlist."""
        rows = await self.bot.database.get_playlist_entries(context.guild.id, name.lower())
        if not rows:
            await context.send("Playlist not found.")
            return
        PAGE       = 15
        page_count = math.ceil(len(rows) / PAGE)
        for page in range(page_count):
            chunk = rows[page * PAGE: (page + 1) * PAGE]
            lines = [
                f"`{index}.` {discord.utils.escape_markdown(row['title'])}"
                for index, row in enumerate(chunk, start=page * PAGE + 1)
            ]
            title = (
                f"Playlist: {name.lower()}" if page_count == 1
                else f"Playlist: {name.lower()} (page {page+1}/{page_count})"
            )
            embed = discord.Embed(title=title, description="\n".join(lines), colour=EMBED_COLOUR)
            embed.set_footer(text=f"{len(rows)} track(s) total")
            await context.send(embed=embed)

    @playlist.command(name="load")
    @commands.guild_only()
    async def playlist_load(self, context: commands.Context[Any], name: str) -> None:
        """Load a saved playlist into the queue."""
        player = await self._join_for_context(context)
        rows   = await self.bot.database.get_playlist_entries(context.guild.id, name.lower())
        if not rows:
            await context.send("Playlist not found.")
            return
        cap_rows = list(rows[: self.bot.settings.max_playlist_size])
        added = 0
        async with context.typing():
            for row in cap_rows:
                if len(player.queue) >= self.bot.settings.max_queue_size:
                    break
                query       = row["query"]
                webpage_url = row["webpage_url"] or query
                if not query or not webpage_url:
                    continue
                await player.enqueue(Track(
                    title=row["title"], webpage_url=webpage_url,
                    stream_url="", uploader="Saved playlist",
                    duration=0, requester_id=context.author.id, query=query,
                ))
                added += 1
        skipped = len(cap_rows) - added
        self._persist_snapshot(context.guild.id)
        self._kick_pipeline(context.guild.id)
        suffix = f" Skipped `{skipped}` unavailable items." if skipped else ""
        await context.send(f"Loaded `{added}` tracks from playlist `{name.lower()}`.{suffix}")
        await self._refresh_now_playing_message(context.guild.id)

    @playlist.command(name="delete")
    @commands.guild_only()
    async def playlist_delete(self, context: commands.Context[Any], name: str) -> None:
        """Delete a saved playlist."""
        await self._require_dj(context)
        if not await self.bot.database.delete_playlist(context.guild.id, name.lower()):
            await context.send("Playlist not found.")
            return
        await context.send(f"Deleted playlist `{name.lower()}`.")
