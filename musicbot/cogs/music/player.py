"""player.py — GuildPlayer: per-guild audio state machine."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable, Literal, TYPE_CHECKING

import discord

from musicbot.cogs.music.constants import (
    LOOP_CYCLE,
    STREAM_URL_REFRESH_AGE_SECONDS,
    VOICE_RECONNECT_ATTEMPTS,
)
from musicbot.cogs.music.models import Track

if TYPE_CHECKING:
    from musicbot.bot import MusicBot

class GuildPlayer:
    """Per-guild voice + queue state machine."""

    def __init__(
        self,
        bot: "MusicBot",
        guild: discord.Guild,
        track_resolver: Callable[[Track], Awaitable[Track | None]],
        audio_source_factory: Callable[[Track], Awaitable[discord.AudioSource]],
        validate_stream_url: Callable[[Track], Awaitable[bool]],
    ) -> None:
        self.bot                  = bot
        self.guild                = guild
        self.track_resolver       = track_resolver
        self.audio_source_factory = audio_source_factory
        self.validate_stream_url  = validate_stream_url
        self.logger               = logging.getLogger(f"musicbot.player.{guild.id}")

        self.voice_client: discord.VoiceClient | None = None
        self.queue:   deque[Track] = deque(maxlen=bot.settings.max_queue_size)
        self.history: deque[Track] = deque(maxlen=20)
        self.current: Track | None = None
        self._total_duration: int = 0   # sum of durations of queued tracks (not current)

        self.announce_channel_id: int | None = None
        self.loop_mode: Literal["off", "one", "all"] = "off"
        self.rewind_requested = False
        self._connected_at: float = 0.0

        self.next_event = asyncio.Event()
        self.idle_task:          asyncio.Task[None] | None = None
        self.empty_channel_task: asyncio.Task[None] | None = None
        self.near_end_task:      asyncio.Task[None] | None = None
        self.np_refresh_task:    asyncio.Task[None] | None = None

        self.skip_votes: set[int] = set()
        self.started_at:      float = 0.0
        self._pause_started:  float = 0.0
        self._total_paused:   float = 0.0
        self._resolve_fail_counts: dict[str, int] = {}

        self.player_task: asyncio.Task[None] | None = None

    @classmethod
    async def create(
        cls,
        bot: "MusicBot",
        guild: discord.Guild,
        track_resolver: Callable[[Track], Awaitable[Track | None]],
        audio_source_factory: Callable[[Track], Awaitable[discord.AudioSource]],
        validate_stream_url: Callable[[Track], Awaitable[bool]],
    ) -> "GuildPlayer":
        """Preferred constructor — creates the player and starts its loop task."""
        player = cls(bot, guild, track_resolver, audio_source_factory, validate_stream_url)
        player.player_task = asyncio.create_task(
            player._player_loop(), name=f"player-{guild.id}"
        )
        return player

    async def connect(
        self, channel: discord.VoiceChannel | discord.StageChannel
    ) -> discord.VoiceClient:
        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.channel != channel:
                await self.voice_client.move_to(channel)
                await self.refresh_empty_channel_state()
            return self.voice_client
        self.voice_client = await channel.connect(self_deaf=True)
        self._connected_at = time.monotonic()
        await self.refresh_empty_channel_state()
        return self.voice_client

    def replace_queue(self, tracks: list[Track]) -> None:
        """Replace the entire queue and recalculate the running duration total.

        All direct assignments to self.queue from outside the player should
        use this method to keep _total_duration accurate.
        """
        cap = self.queue.maxlen
        trimmed = tracks[:cap] if cap is not None else tracks
        self.queue = deque(trimmed, maxlen=cap)
        self._total_duration = sum(t.duration for t in self.queue)

    async def enqueue(self, track: Track, *, front: bool = False) -> None:
        if len(self.queue) == self.queue.maxlen:
            evicted = self.queue[-1] if front else self.queue[0]
            self._total_duration = max(0, self._total_duration - evicted.duration)
        self._total_duration += track.duration
        if front:
            self.queue.appendleft(track)
        else:
            self.queue.append(track)
        self.next_event.set()

    def pause(self) -> bool:
        if not self.voice_client or not self.voice_client.is_playing():
            return False
        self.voice_client.pause()
        self._pause_started = time.monotonic()
        return True

    def resume(self) -> bool:
        if not self.voice_client or not self.voice_client.is_paused():
            return False
        if self._pause_started > 0:
            self._total_paused += time.monotonic() - self._pause_started
            self._pause_started = 0.0
        self.voice_client.resume()
        return True

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at <= 0:
            return 0.0
        now     = time.monotonic()
        elapsed = now - self.started_at - self._total_paused
        if self._pause_started > 0:
            elapsed -= now - self._pause_started
        return max(0.0, elapsed)

    def set_announce_channel(self, channel_id: int) -> None:
        self.announce_channel_id = channel_id

    async def stop(self) -> None:
        self.queue.clear()
        self.history.clear()
        self._total_duration = 0
        self.loop_mode = "off"
        self.rewind_requested = False
        self.skip_votes.clear()
        self.started_at = self._pause_started = self._total_paused = 0.0
        self._resolve_fail_counts.clear()
        await self._cancel_near_end_task()
        await self._cancel_np_refresh_task()
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        self.current = None

    def play_previous(self) -> bool:
        if not self.history:
            return False
        previous_track = self.history.pop()
        if self.current:
            self._total_duration += self.current.duration
            self.queue.appendleft(self.current)
        self._total_duration += previous_track.duration
        self.queue.appendleft(previous_track)
        self.rewind_requested = True
        self.next_event.set()
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        return True

    async def disconnect(self) -> None:
        for task in (self.idle_task, self.empty_channel_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.idle_task = self.empty_channel_task = None
        await self._cancel_near_end_task()
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect(force=False)
        self.voice_client = None
        self.current = None
        self._total_duration = 0
        self.started_at = self._pause_started = self._total_paused = 0.0
        self._resolve_fail_counts.clear()
        self.history.clear()
        self.rewind_requested = False
        self.skip_votes.clear()
        await self._cancel_np_refresh_task()

    def skip(self) -> bool:
        if self.voice_client and (
            self.voice_client.is_playing() or self.voice_client.is_paused()
        ):
            self.voice_client.stop()
            return True
        return False

    async def destroy(self) -> None:
        await self.stop()
        await self.disconnect()
        if self.player_task:
            self.player_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.player_task

    def snapshot(self) -> list[dict[str, Any]]:
        entries: list[Track] = []
        if self.current:
            entries.append(self.current)
        entries.extend(self.queue)
        return [
            {
                # Prefer the original search query; fall back to URL then title so
                # restored tracks are re-fetchable even when query is empty.
                "query": track.query or track.webpage_url or track.title,
                "title": track.title,
                "webpage_url": track.webpage_url or "",
                "requester_id": track.requester_id,
            }
            for track in entries
        ]

    async def _cancel_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _cancel_near_end_task(self) -> None:
        await self._cancel_task(self.near_end_task)
        self.near_end_task = None

    async def _cancel_np_refresh_task(self) -> None:
        await self._cancel_task(self.np_refresh_task)
        self.np_refresh_task = None

    async def _trigger_near_end_preload(self, delay_seconds: float) -> None:
        await asyncio.sleep(delay_seconds)
        self.bot.dispatch("musicbot_track_near_end", self.guild)

    async def _auto_refresh_np_loop(self) -> None:
        interval = self.bot.settings.np_auto_refresh_interval
        try:
            while True:
                await asyncio.sleep(interval)
                if not self.current:
                    break
                self.bot.dispatch("musicbot_np_auto_refresh", self.guild)
        except asyncio.CancelledError:
            pass

    def _has_human_listeners(self) -> bool:
        if not self.voice_client or not self.voice_client.channel:
            return False
        return any(not member.bot for member in self.voice_client.channel.members)

    async def refresh_empty_channel_state(self) -> None:
        if not self.voice_client or not self.voice_client.is_connected():
            if self.empty_channel_task:
                self.empty_channel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.empty_channel_task
                self.empty_channel_task = None
            return
        if self._has_human_listeners():
            if self.empty_channel_task:
                self.empty_channel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.empty_channel_task
                self.empty_channel_task = None
            return
        if self.empty_channel_task is None or self.empty_channel_task.done():
            self.empty_channel_task = asyncio.create_task(self._disconnect_when_empty())

    async def _disconnect_when_empty(self) -> None:
        await asyncio.sleep(self.bot.settings.empty_channel_timeout_seconds)
        if (
            self.voice_client
            and self.voice_client.is_connected()
            and not self._has_human_listeners()
        ):
            await self.stop()
            await self.disconnect()
            self.bot.dispatch("musicbot_queue_updated", self.guild)

    async def _player_loop(self) -> None:
        try:
            while True:
                try:
                    await self._wait_for_track()
                    if not self.current:
                        continue

                    resolved_track = await self.track_resolver(self.current)
                    if resolved_track is None:
                        key   = self.current.query or self.current.webpage_url
                        fails = self._resolve_fail_counts.get(key, 0) + 1
                        if len(self._resolve_fail_counts) > 200:
                            self._resolve_fail_counts.clear()
                        self._resolve_fail_counts[key] = fails
                        backoff = min(30.0, 1.0 * (2 ** (fails - 1)))
                        self.bot.dispatch(
                            "musicbot_track_skipped_error", self.guild, self.current,
                            f"Could not resolve stream (attempt {fails}). Retrying in {backoff:.0f}s."
                            if fails < 4 else "Track is unavailable, skipping.",
                        )
                        self.current = None
                        if fails < 4:
                            await asyncio.sleep(backoff)
                        self.bot.dispatch("musicbot_queue_updated", self.guild)
                        continue

                    key = resolved_track.query or resolved_track.webpage_url
                    self._resolve_fail_counts.pop(key, None)
                    self.current = resolved_track

                    _url_age = time.monotonic() - resolved_track.resolved_at
                    if (
                        resolved_track.stream_url
                        and _url_age >= STREAM_URL_REFRESH_AGE_SECONDS
                        and not await self.validate_stream_url(resolved_track)
                    ):
                        self.logger.info(
                            "Stream URL pre-validation failed for %s, forcing re-resolve.",
                            resolved_track.webpage_url,
                        )
                        resolved_track.stream_url = ""
                        resolved_track.resolved_at = 0.0
                        re_resolved = await self.track_resolver(self.current)
                        if re_resolved is None:
                            self.bot.dispatch(
                                "musicbot_track_skipped_error", self.guild, self.current,
                                "Stream URL expired and could not be refreshed, skipping.",
                            )
                            self.current = None
                            self.bot.dispatch("musicbot_queue_updated", self.guild)
                            continue
                        self.current = re_resolved

                    source = await self.audio_source_factory(self.current)
                    finished = asyncio.Event()
                    _loop = asyncio.get_running_loop()

                    def after_playback(error: Exception | None) -> None:
                        if error:
                            _loop.call_soon_threadsafe(
                                self.bot.dispatch, "musicbot_playback_error", self.guild, error,
                            )
                        _loop.call_soon_threadsafe(finished.set)

                    if not self.voice_client or not self.voice_client.is_connected():
                        reconnected = await self._try_reconnect()
                        if not reconnected:
                            self.current = None
                            continue

                    self.skip_votes.clear()
                    await self._cancel_near_end_task()

                    if self._connected_at > 0:
                        wait = 0.75 - (time.monotonic() - self._connected_at)
                        if wait > 0:
                            await asyncio.sleep(wait)
                        self._connected_at = 0.0

                    self.started_at = time.monotonic()
                    self._pause_started = self._total_paused = 0.0
                    self.voice_client.play(source, after=after_playback)

                    # Safety-net only: the URL pipeline (in cog.py) keeps the
                    # top-3 queue positions warm eagerly. This task is a last
                    # resort — it fires near_end_prefetch_seconds before the end
                    # and force-refreshes position 0 only if its URL is stale.
                    if self.current.duration > self.bot.settings.near_end_prefetch_seconds and (self.queue or self.loop_mode != "off"):
                        self.near_end_task = asyncio.create_task(
                            self._trigger_near_end_preload(
                                max(self.current.duration - self.bot.settings.near_end_prefetch_seconds, 0)
                            )
                        )
                    if self.bot.settings.np_auto_refresh:
                        await self._cancel_np_refresh_task()
                        self.np_refresh_task = asyncio.create_task(self._auto_refresh_np_loop())

                    self.bot.dispatch("musicbot_track_started", self.guild, self.current)
                    await finished.wait()

                    await self._cancel_near_end_task()
                    await self._cancel_np_refresh_task()
                    played_track = self.current
                    self.current = None
                    self.started_at = self._pause_started = self._total_paused = 0.0
                    self.skip_votes.clear()

                    if played_track and not self.rewind_requested:
                        self.history.append(played_track)
                        if self.loop_mode == "one":
                            age = time.monotonic() - played_track.resolved_at
                            if played_track.resolved_at > 0 and age >= STREAM_URL_REFRESH_AGE_SECONDS:
                                played_track.stream_url = ""
                                played_track.resolved_at = 0.0
                            self.queue.appendleft(played_track)
                        elif self.loop_mode == "all":
                            played_track.stream_url = ""
                            played_track.resolved_at = 0.0
                            self.queue.append(played_track)
                    self.rewind_requested = False
                    self.bot.dispatch("musicbot_queue_updated", self.guild)

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._cancel_near_end_task()
                    self.bot.dispatch("musicbot_playback_error", self.guild, exc)
                    self.current = None
                    await asyncio.sleep(1)

        except asyncio.CancelledError:
            pass

    async def _wait_for_track(self) -> None:
        while not self.queue:
            self.current = None
            self.next_event.clear()
            if self.idle_task is None or self.idle_task.done():
                self.idle_task = asyncio.create_task(self._disconnect_when_idle())
            await self.next_event.wait()
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.idle_task
        self.idle_task = None
        self.current = self.queue.popleft()
        self._total_duration = max(0, self._total_duration - self.current.duration)
        self.bot.dispatch("musicbot_queue_updated", self.guild)

    async def _disconnect_when_idle(self) -> None:
        await asyncio.sleep(self.bot.settings.idle_timeout_seconds)
        if not self.current and not self.queue:
            await self.stop()
            await self.disconnect()
            self.bot.dispatch("musicbot_queue_updated", self.guild)

    async def _try_reconnect(self) -> bool:
        channel = self.voice_client.channel if self.voice_client else None
        if channel is None:
            return False
        for attempt in range(1, VOICE_RECONNECT_ATTEMPTS + 1):
            try:
                self.logger.warning(
                    "Voice client disconnected for guild %s, reconnect attempt %d/%d",
                    self.guild.id, attempt, VOICE_RECONNECT_ATTEMPTS,
                )
                self.voice_client = await channel.connect(self_deaf=True)
                return True
            except Exception as exc:
                self.logger.warning("Reconnect attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(1.0 * attempt)
        return False
