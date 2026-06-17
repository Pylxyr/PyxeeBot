"""cog.py — MusicCog: composes all music-subsystem mixins, lifecycle hooks, and the bg-task helper."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TYPE_CHECKING

import aiohttp
from discord.ext import commands

from musicbot.cogs.music._events import EventsMixin
from musicbot.cogs.music._extraction import ExtractionMixin
from musicbot.cogs.music._helpers import CommandHelpersMixin
from musicbot.cogs.music._lifecycle import LifecycleMixin
from musicbot.cogs.music._panel import NPanelMixin
from musicbot.cogs.music._playback_commands import PlaybackCommandsMixin
from musicbot.cogs.music._playlist_commands import PlaylistCommandsMixin
from musicbot.cogs.music._queue_commands import QueueCommandsMixin
from musicbot.cogs.music._resolver import ResolverMixin
from musicbot.cogs.music._search_commands import SearchCommandsMixin
from musicbot.cogs.music.models import NowPlayingController, ResolvedTrackData, SearchDebugRecord
from musicbot.cogs.music.player import GuildPlayer

if TYPE_CHECKING:
    from musicbot.bot import MusicBot


class MusicCog(
    ExtractionMixin,
    ResolverMixin,
    NPanelMixin,
    LifecycleMixin,
    CommandHelpersMixin,
    EventsMixin,
    PlaybackCommandsMixin,
    QueueCommandsMixin,
    SearchCommandsMixin,
    PlaylistCommandsMixin,
    commands.Cog,
):
    def __init__(self, bot: "MusicBot") -> None:
        self.bot = bot
        self.logger = logging.getLogger(__name__)

        self.players: dict[int, GuildPlayer] = {}
        self.now_playing_messages: dict[int, NowPlayingController] = {}

        self._warned_missing_cookiefile = False
        self._ytdl_base_options: dict[str, Any] | None = None
        self._ytdl_variants: dict[tuple[bool, bool], dict[str, Any]] | None = None

        self._ytdl_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ytdlp")
        self._ytdl_tlocal: threading.local = threading.local()

        self._http_session: aiohttp.ClientSession | None = None

        self.resolve_tasks: dict[str, asyncio.Task[ResolvedTrackData | None]] = {}
        self.resolve_cache: OrderedDict[str, tuple[float, ResolvedTrackData]] = OrderedDict()

        self._pipeline_tasks: dict[int, asyncio.Task[None]] = {}
        self._snapshot_deadlines: dict[int, float] = {}
        self._snapshot_tasks: dict[int, asyncio.Task[None]] = {}
        self._np_refresh_deadlines: dict[int, float] = {}
        self._np_refresh_tasks: dict[int, asyncio.Task[None]] = {}

        self._guild_extract_semaphores: dict[int, asyncio.Semaphore] = {}
        self.extract_semaphore = asyncio.Semaphore(self.bot.settings.ytdlp_concurrent_extracts)

        self._last_search: OrderedDict[int, SearchDebugRecord] = OrderedDict()
        self._last_search_max: int = 50
        self._restored_guilds: set[int] = set()

    async def cog_load(self) -> None:
        self._http_session = aiohttp.ClientSession()

    def cog_unload(self) -> None:
        self.now_playing_messages.clear()
        for player in self.players.values():
            self._bg_task(player.destroy(), name="cog-unload-destroy")
        for task_dict in (self._pipeline_tasks, self._snapshot_tasks, self._np_refresh_tasks):
            for task in task_dict.values():
                task.cancel()
        if self._http_session and not self._http_session.closed:
            asyncio.create_task(self._http_session.close())
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

    async def cog_command_error(self, context: commands.Context[Any], error: Exception) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await context.send(f"Slow down — retry in `{error.retry_after:.1f}s`.", delete_after=6)
        elif isinstance(error, commands.CheckFailure):
            await context.send(str(error), delete_after=8)
        elif isinstance(error, commands.BadArgument):
            await context.send(str(error), delete_after=8)
        else:
            raise error

    def _bg_task(self, coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)

        def _on_done(t: asyncio.Task[Any]) -> None:
            if not t.cancelled() and t.exception() is not None:
                self.logger.exception("Background task %r raised", t.get_name(), exc_info=t.exception())

        task.add_done_callback(_on_done)
        return task
