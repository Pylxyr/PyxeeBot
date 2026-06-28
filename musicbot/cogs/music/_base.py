"""_base.py — MusicCogBase: shared attribute and method declarations for MusicCog mixins.

Inheriting from commands.Cog achieves two things:
  1. discord.py command decorators type-check correctly (mixin is a Cog subclass).
  2. Cross-mixin attribute access is statically visible to mypy.

Never instantiated directly; MusicCog provides all concrete implementations.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord.ext import commands

from musicbot.cogs.music.constants import NP_REFRESH_DEBOUNCE_SECONDS
from musicbot.cogs.music.models import NowPlayingController, ResolvedTrackData, SearchDebugRecord, Track
from musicbot.cogs.music.player import GuildPlayer

if TYPE_CHECKING:
    from musicbot.bot import MusicBot
    from musicbot.cogs.music._context import GuildContext
    from musicbot.cogs.music.views import QueueView


class MusicCogBase(commands.Cog):
    """Shared type declarations inherited by every MusicCog mixin."""

    # ── Instance variables (initialised in MusicCog.__init__) ──────────────
    bot: MusicBot
    logger: logging.Logger
    players: dict[int, GuildPlayer]
    _player_create_locks: dict[int, asyncio.Lock]
    now_playing_messages: dict[int, NowPlayingController]
    _warned_missing_cookiefile: bool
    _ytdl_base_options: dict[str, Any] | None
    _ytdl_variants: dict[tuple[bool, bool], dict[str, Any]] | None
    _ytdl_executor: ThreadPoolExecutor
    _ytdl_tlocal: threading.local
    _ytdl_timeout_count: int
    _http_session: aiohttp.ClientSession | None
    resolve_tasks: dict[str, asyncio.Task[ResolvedTrackData | None]]
    resolve_cache: OrderedDict[str, tuple[float, ResolvedTrackData]]
    _pipeline_tasks: dict[int, asyncio.Task[None]]
    _snapshot_deadlines: dict[int, float]
    _snapshot_tasks: dict[int, asyncio.Task[None]]
    _np_refresh_deadlines: dict[int, float]
    _np_refresh_tasks: dict[int, asyncio.Task[None]]
    _guild_extract_semaphores: dict[int, asyncio.Semaphore]
    _curation_semaphores: dict[int, asyncio.Semaphore]
    extract_semaphore: asyncio.Semaphore
    _last_search: OrderedDict[int, SearchDebugRecord]
    _last_search_max: int
    _restored_guilds: set[int]

    # ── MusicCog ────────────────────────────────────────────────────────────
    def _bg_task(self, coro: Any, *, name: str | None = None) -> asyncio.Task[Any]: ...

    # ── LifecycleMixin ──────────────────────────────────────────────────────
    async def _get_player(self, guild: discord.Guild) -> GuildPlayer: ...
    def _persist_snapshot(self, guild_id: int) -> None: ...
    async def _flush_snapshot(
        self, guild_id: int, *, entries: list[dict[str, Any]] | None = None
    ) -> None: ...
    def _snapshot_entries(self, guild_id: int) -> list[dict[str, Any]]: ...
    async def _cleanup_guild(self, guild_id: int) -> None: ...
    async def _restore_snapshot(self, player: GuildPlayer) -> None: ...
    async def _warmup_restore(self, tracks: list[Track], *, guild_id: int) -> None: ...

    # ── CommandHelpersMixin ─────────────────────────────────────────────────
    async def _require_dj(self, context: GuildContext) -> None: ...
    async def _is_dj(self, member: discord.Member) -> bool: ...
    def _is_bot_owner(self, user: discord.User | discord.Member) -> bool: ...
    def _is_in_player_voice(self, player: GuildPlayer, member: discord.Member) -> bool: ...
    def _remember_channel(self, player: GuildPlayer, channel: discord.abc.Messageable) -> None: ...
    def _build_queue_view(
        self, guild_id: int, player: GuildPlayer, *, author_id: int, page: int = 0
    ) -> QueueView: ...
    async def _join_for_context(self, context: GuildContext) -> GuildPlayer: ...
    def _check_per_user_limit(self, player: GuildPlayer, user_id: int) -> bool: ...
    async def _skip_for_member(self, player: GuildPlayer, member: discord.Member) -> str: ...
    async def _previous_for_member(self, player: GuildPlayer, member: discord.Member) -> str: ...
    async def _toggle_pause_for_member(self, player: GuildPlayer, member: discord.Member) -> str: ...
    async def _toggle_loop_for_member(self, player: GuildPlayer, member: discord.Member) -> str: ...
    async def _prompt_for_search_selection(
        self,
        context: GuildContext,
        query: str,
        candidates: list[Track],
        *,
        mode: str,
    ) -> Track | None: ...
    def _voice_humans(self, channel: discord.abc.GuildChannel) -> list[discord.Member]: ...
    def _required_skip_votes(self, player: GuildPlayer) -> int: ...
    async def _ensure_author_voice(
        self, context: GuildContext
    ) -> discord.VoiceChannel | discord.StageChannel: ...
    def _user_queue_count(self, player: GuildPlayer, user_id: int) -> int: ...

    # ── ExtractionMixin ─────────────────────────────────────────────────────
    async def _extract_tracks(
        self,
        query: str,
        requester_id: int,
        *,
        guild_id: int | None = None,
        curation_mode: bool = False,
    ) -> tuple[list[Track], int]: ...
    async def _extract_full_tracks(self, query: str, requester_id: int) -> tuple[list[Track], int]: ...
    async def _extract_search_candidates(
        self,
        query: str,
        requester_id: int,
        *,
        limit: int = 0,
        curation_mode: bool = False,
    ) -> tuple[list[Track], int]: ...
    def _normalize_query(self, query: str) -> str: ...
    def _is_playlist_query(self, query: str) -> bool: ...
    def _preprocess_query(self, raw_query: str) -> str: ...
    def _search_text(self, query: str) -> str: ...
    def _search_result_count(self, query: str) -> int: ...
    async def _validate_stream_url(self, track: Track) -> bool: ...
    async def _build_audio_source(self, track: Track) -> discord.AudioSource: ...
    def _build_ytdl_options(
        self, *, flat_playlist: bool = False, flat_search: bool = False
    ) -> dict[str, Any]: ...

    # ── ResolverMixin ───────────────────────────────────────────────────────
    async def _resolve_track(self, track: Track) -> Track | None: ...
    def _kick_pipeline(self, guild_id: int) -> None: ...
    async def _safety_net_refresh(self, guild_id: int) -> None: ...
    def _get_cached_track_data(self, key: str) -> ResolvedTrackData | None: ...
    def _store_cached_track_data(self, data: ResolvedTrackData) -> None: ...
    def _apply_resolved_track_data(self, track: Track, data: ResolvedTrackData) -> Track: ...
    async def _materialize_track(self, query: str, requester_id: int) -> Track | None: ...

    # ── NPanelMixin ─────────────────────────────────────────────────────────
    async def _send_now_playing_panel(
        self,
        guild: discord.Guild,
        player: GuildPlayer,
        *,
        channel: discord.abc.Messageable | None = None,
        replace_existing: bool = False,
        status_text: str = "",
    ) -> discord.Message | None: ...
    async def _refresh_now_playing_message(self, guild_id: int) -> None: ...
    def _render_now_playing_embed(
        self,
        guild: discord.Guild,
        player: GuildPlayer | None,
        controller: NowPlayingController,
    ) -> discord.Embed: ...
    async def _fetch_announce_channel(
        self, guild: discord.Guild, player: GuildPlayer
    ) -> discord.abc.Messageable | None: ...
    def _schedule_np_refresh(self, guild_id: int, *, delay: float = NP_REFRESH_DEBOUNCE_SECONDS) -> None: ...
    def _controller(self, guild_id: int, *, message_id: int | None = None) -> NowPlayingController | None: ...
