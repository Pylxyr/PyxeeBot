from __future__ import annotations

import asyncio
import contextlib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord
from discord.ext import commands
from yt_dlp import DownloadError, YoutubeDL

from musicbot.cogs.music._base import MusicCogBase
from musicbot.cogs.music._context import _CURRENT_GUILD_ID
from musicbot.cogs.music.constants import (
    FFMPEG_BEFORE_OPTIONS,
    FFMPEG_OPTIONS,
    SEARCH_SELECTION_LIMIT,
    YTDL_OPTIONS,
)
from musicbot.cogs.music.models import Track


class ExtractionMixin(MusicCogBase):
    def _build_ytdl_options(
        self, *, flat_playlist: bool = False, flat_search: bool = False
    ) -> dict[str, Any]:
        if self._ytdl_base_options is None:
            base = dict(YTDL_OPTIONS)
            base["socket_timeout"] = self.bot.settings.ytdlp_socket_timeout
            base["playlistend"] = self.bot.settings.max_playlist_size
            if self.bot.settings.ytdlp_cookies_file:
                if self.bot.settings.ytdlp_cookies_file.exists():
                    base["cookiefile"] = str(self.bot.settings.ytdlp_cookies_file)
                elif not self._warned_missing_cookiefile:
                    self.logger.warning(
                        "YTDLP_COOKIES_FILE does not exist: %s",
                        self.bot.settings.ytdlp_cookies_file,
                    )
                    self._warned_missing_cookiefile = True
            if self.bot.settings.ytdlp_js_runtime_path:
                base["js_runtimes"] = {"node": {"path": self.bot.settings.ytdlp_js_runtime_path}}
            self._ytdl_base_options = base
            fp = dict(base)
            fp["extract_flat"] = "in_playlist"
            fp["lazy_playlist"] = True
            fs = dict(base)
            fs["extract_flat"] = True
            fps = dict(fp)
            fps["extract_flat"] = True
            self._ytdl_variants = {
                (False, False): dict(base),
                (True, False): fp,
                (False, True): fs,
                (True, True): fps,
            }
        return self._ytdl_variants[(flat_playlist, flat_search)]

    async def _validate_stream_url(self, track: Track) -> bool:
        url = track.stream_url
        if not url or not url.startswith("http"):
            return False

        session = self._http_session
        if session is None or session.closed:
            self._http_session = aiohttp.ClientSession()
            session = self._http_session

        try:
            async with session.head(
                url,
                timeout=aiohttp.ClientTimeout(total=5),
                allow_redirects=True,
            ) as resp:
                if resp.status < 400:
                    return True
                self.logger.debug(
                    "Stream URL validation: HTTP %d for %s",
                    resp.status,
                    track.webpage_url,
                )
                return False
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            self.logger.debug(
                "Stream URL HEAD check failed (network error — assuming still valid): %s | %s",
                exc.__class__.__name__,
                url[:80],
            )
            return True
        except Exception as exc:
            self.logger.debug("Stream URL HEAD check unexpected error (assuming valid): %s", exc)
            return True

    async def _build_audio_source(self, track: Track) -> discord.AudioSource:
        bitrate = int(track.abr) if track.abr > 0 else self.bot.settings.opus_bitrate_kbps
        return discord.FFmpegOpusAudio(
            track.stream_url,
            bitrate=bitrate,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_OPTIONS,
        )

    async def _extract_info(
        self,
        query: str,
        *,
        flat_playlist: bool = False,
        flat_search: bool = False,
        curation_mode: bool = False,
    ) -> dict[str, Any]:
        key = (flat_playlist, flat_search)
        options = self._build_ytdl_options(flat_playlist=flat_playlist, flat_search=flat_search)
        guild_id = _CURRENT_GUILD_ID.get()
        if guild_id is None:
            guild_sem = None
        elif curation_mode:
            guild_sem = self._curation_semaphores.setdefault(
                guild_id,
                asyncio.Semaphore(self.bot.settings.ytdlp_curation_concurrency),
            )
        else:
            guild_sem = self._guild_extract_semaphores.setdefault(guild_id, asyncio.Semaphore(1))

        sem_ctx = guild_sem if guild_sem is not None else contextlib.nullcontext()
        async with sem_ctx:
            async with self.extract_semaphore:
                try:
                    loop = asyncio.get_running_loop()

                    def _run() -> dict[str, Any] | None:
                        tlocal = self._ytdl_tlocal
                        if not hasattr(tlocal, "instances"):
                            tlocal.instances = {}
                        ydl = tlocal.instances.get(key)
                        if ydl is None:
                            ydl = YoutubeDL(options)
                            tlocal.instances[key] = ydl
                        return ydl.extract_info(query, download=False)

                    result = await asyncio.wait_for(
                        loop.run_in_executor(self._ytdl_executor, _run),
                        timeout=self.bot.settings.ytdlp_extract_timeout_seconds,
                    )
                    if result is None:
                        raise commands.BadArgument(
                            "No information could be extracted for the provided source."
                        )
                    self._ytdl_timeout_count = 0
                    return result
                except asyncio.TimeoutError as exc:
                    self.logger.warning("yt-dlp timed out for query %r", query)
                    self._ytdl_timeout_count += 1
                    if self._ytdl_timeout_count >= 3:
                        self.logger.warning(
                            "3 consecutive yt-dlp timeouts — recycling extraction thread pool."
                        )
                        old_executor = self._ytdl_executor
                        self._ytdl_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ytdlp")
                        self._ytdl_timeout_count = 0
                        old_executor.shutdown(wait=False)
                    raise commands.BadArgument(
                        f"Source lookup timed out after "
                        f"{self.bot.settings.ytdlp_extract_timeout_seconds} seconds."
                    ) from exc

    def _is_playlist_query(self, query: str) -> bool:
        if not query.startswith(("http://", "https://")):
            return False
        return "list" in parse_qs(urlparse(query).query)

    def _playlist_entry_url(self, item: dict[str, Any]) -> str | None:
        for candidate in (
            item.get("webpage_url"),
            item.get("original_url"),
            item.get("url"),
        ):
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

    async def _extract_playlist_tracks(self, query: str, requester_id: int) -> tuple[list[Track], int]:
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
            tracks.append(
                Track(
                    title=item.get("title", "Unknown title"),
                    webpage_url=webpage_url,
                    stream_url="",
                    uploader=item.get("channel") or item.get("uploader") or "Playlist item",
                    duration=int(item.get("duration") or 0),
                    requester_id=requester_id,
                    query=webpage_url,
                )
            )
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

        stream_url = item.get("url")
        webpage_url = item.get("webpage_url") or query
        if not stream_url:
            return None
        return Track(
            title=item.get("title", "Unknown title"),
            webpage_url=webpage_url,
            stream_url=stream_url,
            uploader=item.get("uploader", "Unknown uploader"),
            duration=int(item.get("duration") or 0),
            requester_id=requester_id,
            query=webpage_url,
            thumbnail_url=self._item_thumbnail_url(item),
            resolved_at=time.monotonic(),
            tags=list(item.get("tags") or []) + list(item.get("categories") or []),
            acodec=item.get("acodec") or "",
            abr=float(item.get("abr") or 0),
        )

    async def _extract_full_tracks(self, query: str, requester_id: int) -> tuple[list[Track], int]:
        try:
            info = await self._extract_info(query)
        except commands.BadArgument:
            raise
        except DownloadError as exc:
            self.logger.warning("yt-dlp failed for query %r: %s", query, exc)
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

    def _search_text(self, query: str) -> str:
        match = re.match(r"^ytsearch(?:all|\d+)?:", query, flags=re.IGNORECASE)
        if not match:
            return query.strip()
        return query[match.end() :].strip()

    def _preprocess_query(self, raw_query: str) -> str:
        if raw_query.startswith(("http://", "https://")) or raw_query.startswith("ytsearch"):
            return raw_query
        return re.sub(r"\s+", " ", raw_query).strip()

    def _normalize_query(self, query: str) -> str:
        query = self._preprocess_query(query)
        if query.startswith(("http://", "https://")) or query.startswith("ytsearch"):
            return query
        return f"ytsearch{self.bot.settings.ytdlp_search_results}:{query}"

    async def _extract_search_candidates(
        self,
        query: str,
        requester_id: int,
        *,
        limit: int = SEARCH_SELECTION_LIMIT,
        curation_mode: bool = False,
    ) -> tuple[list[Track], int]:
        try:
            info = await self._extract_info(query, flat_search=True, curation_mode=curation_mode)
        except commands.BadArgument:
            raise
        except DownloadError as exc:
            self.logger.warning("yt-dlp search failed for %r: %s", query, exc)
            raise commands.BadArgument(f"Failed to fetch media: {exc}") from exc

        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return [], 0

        tracks: list[Track] = []
        skipped = 0
        for item in entries:
            track = self._search_result_track(item, requester_id)
            if track is None:
                skipped += 1
                continue
            tracks.append(track)
            if len(tracks) >= limit:
                break
        return tracks, skipped

    async def _extract_search_tracks(self, query: str, requester_id: int) -> tuple[list[Track], int]:
        return await self._extract_search_candidates(query, requester_id, limit=1)

    async def _extract_tracks(
        self,
        query: str,
        requester_id: int,
        *,
        guild_id: int | None = None,
        curation_mode: bool = False,
    ) -> tuple[list[Track], int]:
        token = _CURRENT_GUILD_ID.set(guild_id)
        try:
            if query.startswith("ytsearch"):
                return await self._extract_search_candidates(
                    query, requester_id, limit=1, curation_mode=curation_mode
                )
            if self._is_playlist_query(query):
                return await self._extract_playlist_tracks(query, requester_id)
            return await self._extract_full_tracks(query, requester_id)
        finally:
            _CURRENT_GUILD_ID.reset(token)
