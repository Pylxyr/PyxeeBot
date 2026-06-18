"""_extraction.py — ExtractionMixin: yt-dlp extraction and audio-source helpers.

Mixed into MusicCog.  All methods access shared state through ``self``
(bot, logger, _ytdl_tlocal, _http_session, etc.) which MusicCog.__init__
initialises before any method can be called.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord
from discord.ext import commands
from yt_dlp import DownloadError, YoutubeDL

from musicbot.cogs.music._context import _CURRENT_GUILD_ID
from musicbot.cogs.music.constants import (
    FFMPEG_BEFORE_OPTIONS,
    FFMPEG_OPTIONS,
    SEARCH_SELECTION_LIMIT,
    _SEARCH_RESULT_COUNT_LONG,
    _SEARCH_RESULT_COUNT_MED,
    YTDL_OPTIONS,
)
from musicbot.cogs.music.models import Track
from musicbot.cogs.music.scoring import rank_entries, signal_tokens


class ExtractionMixin:
    """yt-dlp extraction, stream-URL validation, and FFmpeg audio-source construction."""

    # ── yt-dlp option builders ──────────────────────────────────────────────

    def _build_ytdl_options(
        self, *, flat_playlist: bool = False, flat_search: bool = False
    ) -> dict[str, Any]:
        if self._ytdl_base_options is None:  # type: ignore[attr-defined]
            base = dict(YTDL_OPTIONS)
            base["socket_timeout"] = self.bot.settings.ytdlp_socket_timeout  # type: ignore[attr-defined]
            base["playlistend"] = self.bot.settings.max_playlist_size  # type: ignore[attr-defined]
            if self.bot.settings.ytdlp_cookies_file:  # type: ignore[attr-defined]
                if self.bot.settings.ytdlp_cookies_file.exists():  # type: ignore[attr-defined]
                    base["cookiefile"] = str(self.bot.settings.ytdlp_cookies_file)  # type: ignore[attr-defined]
                elif not self._warned_missing_cookiefile:  # type: ignore[attr-defined]
                    self.logger.warning(  # type: ignore[attr-defined]
                        "YTDLP_COOKIES_FILE does not exist: %s",
                        self.bot.settings.ytdlp_cookies_file,  # type: ignore[attr-defined]
                    )
                    self._warned_missing_cookiefile = True  # type: ignore[attr-defined]
            if self.bot.settings.ytdlp_js_runtime_path:  # type: ignore[attr-defined]
                base["js_runtimes"] = {
                    "node": {"path": self.bot.settings.ytdlp_js_runtime_path}  # type: ignore[attr-defined]
                }
            self._ytdl_base_options = base  # type: ignore[attr-defined]
            fp = dict(base)
            fp["extract_flat"] = "in_playlist"
            fp["lazy_playlist"] = True
            fs = dict(base)
            fs["extract_flat"] = True
            fps = dict(fp)
            fps["extract_flat"] = True
            self._ytdl_variants = {  # type: ignore[attr-defined]
                (False, False): dict(base),
                (True, False): fp,
                (False, True): fs,
                (True, True): fps,
            }
        return self._ytdl_variants[(flat_playlist, flat_search)]  # type: ignore[attr-defined]

    # ── Stream-URL validation ───────────────────────────────────────────────

    async def _validate_stream_url(self, track: Track) -> bool:
        """HEAD-check a resolved stream URL against its CDN origin.

        Returns
        -------
        True
            The server confirmed the URL is still alive, **or** we could not
            reach the server (timeout / connection error).  Network
            unavailability does not mean the URL is stale — returning False
            here would cause spurious re-resolves and gaps between tracks.
        False
            The server explicitly rejected the URL (HTTP 4xx / 5xx).
        """
        url = track.stream_url
        if not url or not url.startswith("http"):
            return False

        session = self._http_session  # type: ignore[attr-defined]
        if session is None or session.closed:
            self._http_session = aiohttp.ClientSession()  # type: ignore[attr-defined]
            session = self._http_session  # type: ignore[attr-defined]

        try:
            async with session.head(
                url,
                timeout=aiohttp.ClientTimeout(total=5),
                allow_redirects=True,
            ) as resp:
                if resp.status < 400:
                    return True
                self.logger.debug(  # type: ignore[attr-defined]
                    "Stream URL validation: HTTP %d for %s",
                    resp.status,
                    track.webpage_url,
                )
                return False
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            # Can't check ≠ stale.  Keep the cached stream URL.
            self.logger.debug(  # type: ignore[attr-defined]
                "Stream URL HEAD check failed (network error — assuming still valid): %s | %s",
                exc.__class__.__name__,
                url[:80],
            )
            return True
        except Exception as exc:
            self.logger.debug(  # type: ignore[attr-defined]
                "Stream URL HEAD check unexpected error (assuming valid): %s", exc
            )
            return True

    # ── FFmpeg audio-source construction ───────────────────────────────────

    async def _build_audio_source(self, track: Track) -> discord.AudioSource:
        # yt-dlp reports acodec="opus" for WebM/Opus streams (the common YouTube
        # high-quality format).  Skip the ffprobe subprocess since we already know
        # the codec from yt-dlp metadata — but do NOT pass codec="copy". Copy mode
        # bypasses the libopus encoder entirely, so -frame_duration/-flush_packets
        # have no effect and ffmpeg dumps the container's buffered packets at
        # whatever cadence it read them, causing fast-forwarded playback for the
        # first few seconds of every track. Re-encoding (the default codec when
        # omitted) is what makes the steady 20ms pacing in FFMPEG_OPTIONS work.
        if track.acodec == "opus":
            return discord.FFmpegOpusAudio(
                track.stream_url,
                bitrate=self.bot.settings.opus_bitrate_kbps,  # type: ignore[attr-defined]
                before_options=FFMPEG_BEFORE_OPTIONS,
                options=FFMPEG_OPTIONS,
            )
        try:
            source: discord.AudioSource = await discord.FFmpegOpusAudio.from_probe(
                track.stream_url,
                method="fallback",
                before_options=FFMPEG_BEFORE_OPTIONS,
                options=FFMPEG_OPTIONS,
            )
        except (discord.ClientException, OSError, TypeError, ValueError) as exc:
            self.logger.warning(  # type: ignore[attr-defined]
                "Opus probe fallback for %s: %s", track.webpage_url, exc
            )
            try:
                source = discord.FFmpegOpusAudio(
                    track.stream_url,
                    bitrate=self.bot.settings.opus_bitrate_kbps,  # type: ignore[attr-defined]
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                )
            except (discord.ClientException, OSError, TypeError, ValueError) as exc2:
                self.logger.warning(  # type: ignore[attr-defined]
                    "FFmpeg source construction failed for %s [%s]: %s — skipping.",
                    track.title,
                    track.webpage_url,
                    exc2,
                )
                raise
        return source

    # ── Core yt-dlp wrapper ─────────────────────────────────────────────────

    async def _extract_info(
        self,
        query: str,
        *,
        flat_playlist: bool = False,
        flat_search: bool = False,
    ) -> dict[str, Any]:
        key = (flat_playlist, flat_search)
        options = self._build_ytdl_options(flat_playlist=flat_playlist, flat_search=flat_search)
        guild_id = _CURRENT_GUILD_ID.get()
        guild_sem = (
            self._guild_extract_semaphores.setdefault(guild_id, asyncio.Semaphore(1))  # type: ignore[attr-defined]
            if guild_id is not None
            else None
        )

        sem_ctx = guild_sem if guild_sem is not None else contextlib.nullcontext()
        async with sem_ctx:  # type: ignore[attr-defined]
            async with self.extract_semaphore:  # type: ignore[attr-defined]
                try:
                    loop = asyncio.get_running_loop()

                    def _run() -> dict[str, Any] | None:
                        tlocal = self._ytdl_tlocal  # type: ignore[attr-defined]
                        if not hasattr(tlocal, "instances"):
                            tlocal.instances: dict[tuple[bool, bool], YoutubeDL] = {}
                        ydl = tlocal.instances.get(key)
                        if ydl is None:
                            ydl = YoutubeDL(options)
                            tlocal.instances[key] = ydl
                        return ydl.extract_info(query, download=False)

                    result = await asyncio.wait_for(
                        loop.run_in_executor(self._ytdl_executor, _run),  # type: ignore[attr-defined]
                        timeout=self.bot.settings.ytdlp_extract_timeout_seconds,  # type: ignore[attr-defined]
                    )
                    if result is None:
                        raise commands.BadArgument(
                            "No information could be extracted for the provided source."
                        )
                    return result
                except asyncio.TimeoutError as exc:
                    self.logger.warning("yt-dlp timed out for query %r", query)  # type: ignore[attr-defined]
                    raise commands.BadArgument(
                        f"Source lookup timed out after "
                        f"{self.bot.settings.ytdlp_extract_timeout_seconds} seconds."  # type: ignore[attr-defined]
                    ) from exc

    # ── URL / metadata helpers ──────────────────────────────────────────────

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

    # ── Multi-track extractors ──────────────────────────────────────────────

    async def _extract_playlist_tracks(self, query: str, requester_id: int) -> tuple[list[Track], int]:
        try:
            info = await self._extract_info(query, flat_playlist=True)
        except commands.BadArgument:
            raise
        except DownloadError as exc:
            self.logger.warning("yt-dlp playlist scan failed for %s: %s", query, exc)  # type: ignore[attr-defined]
            raise commands.BadArgument(f"Failed to fetch media: {exc}") from exc

        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return [], 0

        tracks: list[Track] = []
        skipped = 0
        for item in entries:
            if len(tracks) >= self.bot.settings.max_playlist_size:  # type: ignore[attr-defined]
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
                self.logger.warning(  # type: ignore[attr-defined]
                    "Skipping unplayable item %s: %s", item.get("webpage_url"), exc
                )
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
        )

    async def _extract_full_tracks(self, query: str, requester_id: int) -> tuple[list[Track], int]:
        try:
            info = await self._extract_info(query)
        except commands.BadArgument:
            raise
        except DownloadError as exc:
            self.logger.warning("yt-dlp failed for query %r: %s", query, exc)  # type: ignore[attr-defined]
            raise commands.BadArgument(f"Failed to fetch media: {exc}") from exc

        entries = info.get("entries") if isinstance(info, dict) else None
        info_items: list[dict[str, Any]]
        if entries:
            info_items = [e for e in entries if e][: self.bot.settings.max_playlist_size]  # type: ignore[attr-defined]
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

    # ── Search helpers ──────────────────────────────────────────────────────

    def _search_result_count(self, query: str) -> int:
        base = max(self.bot.settings.ytdlp_search_results, SEARCH_SELECTION_LIMIT)  # type: ignore[attr-defined]
        tokens = signal_tokens(query)
        if len(tokens) >= 4:
            return max(base, _SEARCH_RESULT_COUNT_LONG)
        if len(tokens) >= 3:
            return max(base, _SEARCH_RESULT_COUNT_MED)
        return base

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
        return f"ytsearch{self._search_result_count(query)}:{query}"

    async def _extract_search_candidates(
        self,
        query: str,
        requester_id: int,
        *,
        limit: int = SEARCH_SELECTION_LIMIT,
        curation_mode: bool = False,
    ) -> tuple[list[Track], int]:
        try:
            info = await self._extract_info(query, flat_search=True)
        except commands.BadArgument:
            raise
        except DownloadError as exc:
            self.logger.warning("yt-dlp search failed for %r: %s", query, exc)  # type: ignore[attr-defined]
            raise commands.BadArgument(f"Failed to fetch media: {exc}") from exc

        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return [], 0

        guild_id = _CURRENT_GUILD_ID.get()
        search_text = self._search_text(query)
        ranked_items = rank_entries(
            search_text,
            list(entries),
            guild_id,
            self._last_search,  # type: ignore[attr-defined]
            self._last_search_max,  # type: ignore[attr-defined]
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
