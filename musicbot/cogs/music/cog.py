"""cog.py — MusicCog: commands and event handlers."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import random
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TYPE_CHECKING

import aiohttp
import discord
from discord.ext import commands

from musicbot.cogs.music._context import _CURRENT_GUILD_ID
from musicbot.cogs.music._extraction import ExtractionMixin
from musicbot.cogs.music._panel import NPanelMixin
from musicbot.cogs.music._resolver import ResolverMixin
from musicbot.cogs.music.constants import (
    EMBED_COLOUR, LOOP_CYCLE, LOOP_ICONS, LOOP_LABELS,
    NOW_PLAYING_TIMEOUT_SECONDS, PRESENCE_DEBOUNCE_SECONDS, SNAPSHOT_DEBOUNCE_SECONDS,
)
from musicbot.cogs.music.models import (
    NowPlayingController, ResolvedTrackData, SearchDebugRecord, Track,
)
from musicbot.cogs.music.player import GuildPlayer
from musicbot.cogs.music.views import (
    NowPlayingView, QueueView, ScoreDebugView, SearchSelectionView,
)

if TYPE_CHECKING:
    from musicbot.bot import MusicBot


class MusicCog(ExtractionMixin, ResolverMixin, NPanelMixin, commands.Cog):
    def __init__(self, bot: "MusicBot") -> None:
        self.bot    = bot
        self.logger = logging.getLogger(__name__)

        self.players: dict[int, GuildPlayer] = {}
        self.now_playing_messages: dict[int, NowPlayingController] = {}

        self._warned_missing_cookiefile = False
        self._ytdl_base_options: dict[str, Any] | None = None
        self._ytdl_variants: dict[tuple[bool, bool], dict[str, Any]] | None = None

        import threading
        self._ytdl_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ytdlp")
        self._ytdl_tlocal: threading.local = threading.local()

        self._http_session: aiohttp.ClientSession | None = None

        self.resolve_tasks: dict[str, asyncio.Task[ResolvedTrackData | None]] = {}
        self.resolve_cache: OrderedDict[str, tuple[float, ResolvedTrackData]] = OrderedDict()

        self._pipeline_tasks:      dict[int, asyncio.Task[None]] = {}
        self._snapshot_deadlines:  dict[int, float] = {}
        self._snapshot_tasks:      dict[int, asyncio.Task[None]] = {}
        self._np_refresh_deadlines: dict[int, float] = {}
        self._np_refresh_tasks:    dict[int, asyncio.Task[None]] = {}

        self._presence_task:     asyncio.Task[None] | None = None
        self._presence_deadline: float = 0.0

        self._guild_extract_semaphores: dict[int, asyncio.Semaphore] = {}
        self.extract_semaphore = asyncio.Semaphore(self.bot.settings.ytdlp_concurrent_extracts)

        self._last_search:     OrderedDict[int, SearchDebugRecord] = OrderedDict()
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
        if self._presence_task and not self._presence_task.done():
            self._presence_task.cancel()
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

    async def cog_command_error(
        self, context: commands.Context[Any], error: Exception
    ) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await context.send(
                f"Slow down — retry in `{error.retry_after:.1f}s`.", delete_after=6
            )
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
                self.logger.exception("Background task %r raised", t.get_name(),
                                      exc_info=t.exception())
        task.add_done_callback(_on_done)
        return task

    # ── Player lifecycle ────────────────────────────────────────────────────

    async def _get_player(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if not player:
            player = await GuildPlayer.create(
                self.bot, guild,
                self._resolve_track, self._build_audio_source, self._validate_stream_url,
            )
            self.players[guild.id] = player
            await self._restore_snapshot(player)
        return player

    async def _restore_snapshot(self, player: GuildPlayer) -> None:
        if not self.bot.settings.restore_queue_on_restart:
            await self.bot.database.save_queue_snapshot(player.guild.id, [])
            return
        rows = await self.bot.database.load_queue_snapshot(player.guild.id)
        if player.queue:
            return
        restored = [
            Track(
                title=row["title"], webpage_url=row["webpage_url"],
                stream_url="", uploader="Restored queue",
                duration=0, requester_id=int(row["requester_id"]), query=row["query"],
            )
            for row in rows
        ]
        player.replace_queue(restored)
        if restored:
            self._restored_guilds.add(player.guild.id)
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

    # ── Snapshot persistence ────────────────────────────────────────────────

    def _persist_snapshot(self, guild_id: int) -> None:
        self._snapshot_deadlines[guild_id] = time.monotonic() + SNAPSHOT_DEBOUNCE_SECONDS
        task = self._snapshot_tasks.get(guild_id)
        if task and not task.done():
            return
        self._snapshot_tasks[guild_id] = self._bg_task(
            self._snapshot_loop(guild_id), name=f"snapshot-{guild_id}"
        )

    async def _snapshot_loop(self, guild_id: int) -> None:
        try:
            while True:
                deadline = self._snapshot_deadlines.get(guild_id)
                if deadline is None:
                    break
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
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
        return player.snapshot() if player is not None else []

    async def _write_snapshot(
        self, guild_id: int, *, entries: list[dict[str, Any]] | None = None
    ) -> None:
        if not self.bot.database.is_open or getattr(self.bot, "_shutting_down", False):
            return
        snapshot = self._snapshot_entries(guild_id) if entries is None else entries
        await self.bot.database.save_queue_snapshot(guild_id, snapshot)

    # ── Bot presence ────────────────────────────────────────────────────────

    async def _update_bot_presence(self) -> None:
        self._presence_deadline = time.monotonic() + PRESENCE_DEBOUNCE_SECONDS
        if self._presence_task and not self._presence_task.done():
            return
        self._presence_task = self._bg_task(self._presence_loop(), name="presence-update")

    async def _presence_loop(self) -> None:
        try:
            while True:
                remaining = self._presence_deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                active = [
                    p for p in self.players.values()
                    if p.current and p.voice_client and p.voice_client.is_playing()
                ]
                with contextlib.suppress(Exception):
                    if not active:
                        await self.bot.change_presence(activity=discord.Activity(
                            type=discord.ActivityType.watching,
                            name="pylxyr.github.io/PyxeeBot-Page/",
                        ))
                    elif len(active) == 1:
                        await self.bot.change_presence(activity=discord.Activity(
                            type=discord.ActivityType.listening,
                            name=active[0].current.title[:128],
                        ))
                    else:
                        await self.bot.change_presence(activity=discord.Activity(
                            type=discord.ActivityType.listening,
                            name=f"music in {len(active)} servers",
                        ))
                if self._presence_deadline <= time.monotonic():
                    break
        except asyncio.CancelledError:
            return

    # ── Permission helpers ──────────────────────────────────────────────────

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
        self, context: commands.Context[Any], query: str,
        candidates: list[Track], *, mode: str,
    ) -> Track | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        view = SearchSelectionView(
            author_id=context.author.id, candidates=candidates, mode=mode,
            query_text=self._search_text(query), prefix=context.clean_prefix,
            bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
            guild_icon_url=(
                context.guild.icon.url if context.guild and context.guild.icon else None
            ),
        )
        prompt = await context.send(embed=view.build_embed(), view=view)
        view.message = prompt
        return await view.wait_for_selection()

    def _user_queue_count(self, player: GuildPlayer, user_id: int) -> int:
        return sum(1 for t in player.queue if t.requester_id == user_id)

    def _check_per_user_limit(self, player: GuildPlayer, user_id: int) -> bool:
        limit = self.bot.settings.max_queue_size_per_user
        return limit > 0 and self._user_queue_count(player, user_id) >= limit

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
        prev_label       = LOOP_LABELS.get(player.loop_mode, "Off")
        player.loop_mode = LOOP_CYCLE.get(player.loop_mode, "off")
        self._persist_snapshot(member.guild.id)
        label = LOOP_LABELS.get(player.loop_mode, "Off")
        icon  = LOOP_ICONS.get(player.loop_mode, "→")
        return f"Loop changed: **{prev_label}** → {icon} **{label}**"

    # ── Event listeners ─────────────────────────────────────────────────────

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
                await channel.send(f"Skipped **{track.escaped_title}** — {reason}")

    @commands.Cog.listener()
    async def on_musicbot_playback_error(self, guild: discord.Guild, error: Exception) -> None:
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
        self, member: discord.Member,
        before: discord.VoiceState, after: discord.VoiceState,
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

    # ── Commands ────────────────────────────────────────────────────────────

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
        """Show the last tracks played this session."""
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
        embed = discord.Embed(title="Recent History", description="\n".join(lines[:20]),
                              colour=EMBED_COLOUR)
        embed.set_footer(text=f"{len(hist)} track(s) in session history.")
        await context.send(embed=embed)

    @commands.hybrid_command(name="why", aliases=["searchdebug", "scorewhy"])
    @commands.guild_only()
    async def why(self, context: commands.Context[Any]) -> None:
        """Show how the last search's results were scored."""
        record = self._last_search.get(context.guild.id)
        if record is None:
            await context.send("No search has been run this session.")
            return
        stale_suffix = ""
        age = time.monotonic() - record.timestamp
        if age > 300:
            stale_suffix = f"\n> ⚠️ This breakdown is {int(age // 60)}m old."
        embed = discord.Embed(
            title=f"Score breakdown — `{discord.utils.escape_markdown(record.query_text)}`",
            colour=EMBED_COLOUR,
        )
        lines: list[str] = []
        for c in record.candidates:
            sel       = "  ✓" if c.selected else ""
            dur_m, dur_s = divmod(c.duration, 60)
            dur_label = f"{dur_m}:{dur_s:02d}" if c.duration else "?"
            detail = (
                f"title={c.title_overlap:.2f} artist={c.uploader_overlap:.2f} "
                f"anchor={c.anchor_score:+.2f} jp={c.jp_original_bonus:+.2f} "
                f"views={c.view_bonus:+.2f} penalty={-c.discouraged_penalty:+.2f}"
            )
            lines.append(
                f"`#{c.rank}` **{c.final_score:+.3f}**{sel} "
                f"[{discord.utils.escape_markdown(c.title[:52])}]({c.webpage_url})"
                f"\n└ `{dur_label}` · {detail}"
            )
        embed.description = "\n\n".join(lines) + stale_suffix if lines else "No data."
        embed.set_footer(text="Press the button for a full per-component DM breakdown.")
        await context.send(embed=embed, view=ScoreDebugView(author_id=context.author.id, record=record))

    @commands.hybrid_command(name="skipto")
    @commands.guild_only()
    async def skipto(self, context: commands.Context[Any], position: int) -> None:
        """Skip ahead to a specific queue position."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is empty.")
            return
        self._remember_channel(player, context.channel)
        size = len(player.queue)
        if position < 1 or position > size:
            await context.send(f"Position `{position}` out of range (queue has {size} tracks).")
            return
        if position == 1:
            await context.send("That is already the next track.")
            return
        queue_list = list(player.queue)
        dropped    = position - 1
        player.replace_queue(queue_list[position - 1:])
        self._persist_snapshot(context.guild.id)
        target = player.queue[0]
        embed  = discord.Embed(title="Jumped to Position", colour=EMBED_COLOUR)
        embed.add_field(name="Now Up Next",
                        value=f"[{discord.utils.escape_markdown(target.title)}]({target.webpage_url})",
                        inline=False)
        embed.add_field(name="Position", value=f"`{position}`", inline=True)
        embed.add_field(name="Dropped",  value=f"`{dropped}` track{'s' if dropped != 1 else ''}", inline=True)
        if target.thumbnail_url:
            embed.set_thumbnail(url=target.thumbnail_url)
        await context.send(embed=embed)

    @commands.hybrid_command(name="replay")
    @commands.guild_only()
    async def replay(self, context: commands.Context[Any]) -> None:
        """Re-queue the current track to play next."""
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
        await context.send(f"Re-queued **{player.current.escaped_title}** to play next.")

    @commands.hybrid_command(name="qsearch", aliases=["qs"])
    @commands.guild_only()
    async def qsearch(self, context: commands.Context[Any], *, keyword: str) -> None:
        """Search within the current queue."""
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is empty.")
            return
        kw      = keyword.strip().lower()
        matches = [
            (i + 1, t) for i, t in enumerate(player.queue)
            if kw in t.title.lower() or kw in (t.uploader or "").lower()
        ]
        if not matches:
            await context.send(f"No tracks matching `{discord.utils.escape_markdown(keyword)}`.")
            return
        lines = [
            f"`{pos}.` [{discord.utils.escape_markdown(t.title)}]({t.webpage_url})"
            for pos, t in matches[:20]
        ]
        embed = discord.Embed(title=f"Queue Search: {discord.utils.escape_markdown(keyword)}",
                              description="\n".join(lines), colour=EMBED_COLOUR)
        embed.set_footer(text=(
            f"Showing first 20 of {len(matches)} matches."
            if len(matches) > 20 else f"{len(matches)} match(es) found."
        ))
        await context.send(embed=embed)

    @commands.hybrid_command(name="play", aliases=["p"])
    @commands.guild_only()
    @commands.cooldown(2, 4, commands.BucketType.user)
    async def play(self, context: commands.Context[Any], *, query: str) -> None:
        """Queue a URL, playlist, or search query."""
        player = await self._join_for_context(context)
        self._kick_pipeline(context.guild.id)
        if len(player.queue) >= self.bot.settings.max_queue_size:
            await context.send("Queue is full.")
            return
        query       = self._normalize_query(query)
        is_playlist = self._is_playlist_query(query)
        fetch_msg: discord.Message | None = await context.send(
            "⏳ Loading playlist…" if is_playlist
            else "🔍 Fetching…" if query.startswith(("http://", "https://"))
            else "🔍 Searching…"
        )
        async with context.typing():
            tracks, skipped = await self._extract_tracks(
                query, requester_id=context.author.id, guild_id=context.guild.id,
            )
        if not tracks:
            msg = (
                f"No playable results found. Skipped `{skipped}` unavailable items."
                if skipped else "No playable results found. Try `!search <query>` to browse manually."
            )
            await (fetch_msg.edit(content=msg) if fetch_msg else context.send(msg))
            return
        added = 0
        hit_user_limit = False
        for track in tracks:
            if len(player.queue) >= self.bot.settings.max_queue_size:
                break
            if self._check_per_user_limit(player, context.author.id):
                hit_user_limit = True
                break
            await player.enqueue(track)
            added += 1
        self._persist_snapshot(context.guild.id)
        self._kick_pipeline(context.guild.id)
        await self._refresh_now_playing_message(context.guild.id)
        suffix = f" Skipped `{skipped}` unavailable items." if skipped else ""
        if hit_user_limit:
            limit = self.bot.settings.max_queue_size_per_user
            suffix += f" Stopped at your `{limit}`-track per-user limit."
        result = (
            f"Queued [{tracks[0].escaped_title}]({tracks[0].webpage_url}).{suffix}"
            if added == 1 else f"Queued `{added}` tracks.{suffix}"
        )
        if context.guild.id in self._restored_guilds:
            restored_count = len(player.queue) - added
            if restored_count > 0:
                result += (
                    f"\n> 📋 `{restored_count}` track{'s' if restored_count != 1 else ''} "
                    f"from your last session are in the queue. Run `!clear` to start fresh."
                )
            self._restored_guilds.discard(context.guild.id)
        await (fetch_msg.edit(content=result) if fetch_msg else context.send(result))

    @commands.hybrid_command(name="playnext", aliases=["pn"])
    @commands.guild_only()
    @commands.cooldown(2, 4, commands.BucketType.user)
    async def playnext(self, context: commands.Context[Any], *, query: str) -> None:
        """Insert a track next in queue."""
        await self._require_dj(context)
        player    = await self._join_for_context(context)
        query     = self._normalize_query(query)
        fetch_msg = await context.send("🔍 Searching…")
        async with context.typing():
            tracks, _ = await self._extract_tracks(
                query, requester_id=context.author.id, guild_id=context.guild.id,
            )
        track = tracks[0] if tracks else None
        if track is None:
            await fetch_msg.edit(content="No playable result found.")
            return
        if self._check_per_user_limit(player, context.author.id):
            limit = self.bot.settings.max_queue_size_per_user
            await fetch_msg.edit(content=f"You already have `{limit}` tracks in the queue.")
            return
        await player.enqueue(track, front=True)
        self._persist_snapshot(context.guild.id)
        self._kick_pipeline(context.guild.id)
        await self._refresh_now_playing_message(context.guild.id)
        await fetch_msg.edit(content=f"Queued next: [{track.escaped_title}]({track.webpage_url}).")

    @commands.hybrid_command(name="repeat", aliases=["rp"])
    @commands.guild_only()
    async def repeat(self, context: commands.Context[Any]) -> None:
        """Toggle single-track repeat."""
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
    @commands.cooldown(1, 6, commands.BucketType.user)
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
        selected = await self._prompt_for_search_selection(
            context, search_query, tracks, mode="play"
        )
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
        await context.send(await self._skip_for_member(player, context.author))
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
        await context.send(await self._previous_for_member(player, context.author))
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
        """Open the live control panel."""
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
        player.replace_queue([])
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
        """Move a track from one queue position to another."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is empty.")
            return
        self._remember_channel(player, context.channel)
        size = len(player.queue)
        if not (1 <= from_index <= size):
            await context.send(f"Position `{from_index}` out of range.")
            return
        if not (1 <= to_index <= size):
            await context.send(f"Position `{to_index}` out of range.")
            return
        if from_index == to_index:
            await context.send("Source and destination are the same.")
            return
        queue_list = list(player.queue)
        track      = queue_list.pop(from_index - 1)
        queue_list.insert(to_index - 1, track)
        player.replace_queue(queue_list)
        self._persist_snapshot(context.guild.id)
        await context.send(f"Moved **{track.escaped_title}** from `{from_index}` to `{to_index}`.")
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
            title = ("Saved Playlists" if page_count == 1
                     else f"Saved Playlists (page {page+1}/{page_count})")
            embed = discord.Embed(title=title, description="\n".join(lines), colour=EMBED_COLOUR)
            embed.set_footer(text=f"{len(rows)} playlist(s) total")
            await context.send(embed=embed)

    @playlist.command(name="show")
    @commands.guild_only()
    async def playlist_show(self, context: commands.Context[Any], name: str) -> None:
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
            title = (f"Playlist: {name.lower()}" if page_count == 1
                     else f"Playlist: {name.lower()} (page {page+1}/{page_count})")
            embed = discord.Embed(title=title, description="\n".join(lines), colour=EMBED_COLOUR)
            embed.set_footer(text=f"{len(rows)} track(s) total")
            await context.send(embed=embed)

    @playlist.command(name="load")
    @commands.guild_only()
    async def playlist_load(self, context: commands.Context[Any], name: str) -> None:
        player    = await self._join_for_context(context)
        rows      = await self.bot.database.get_playlist_entries(context.guild.id, name.lower())
        if not rows:
            await context.send("Playlist not found.")
            return
        cap_rows  = list(rows[: self.bot.settings.max_playlist_size])
        truncated = len(rows) - len(cap_rows)
        added     = 0
        async with context.typing():
            for row in cap_rows:
                if len(player.queue) >= self.bot.settings.max_queue_size:
                    break
                query       = row["query"]
                webpage_url = row["webpage_url"] or query
                if not query or not webpage_url:
                    continue
                await player.enqueue(Track(
                    title=row["title"], webpage_url=webpage_url, stream_url="",
                    uploader="Saved playlist", duration=0,
                    requester_id=context.author.id, query=query,
                ))
                added += 1
        queue_skipped = len(cap_rows) - added
        self._persist_snapshot(context.guild.id)
        self._kick_pipeline(context.guild.id)
        parts: list[str] = [f"Loaded `{added}` tracks from playlist `{name.lower()}`."]
        if queue_skipped:
            parts.append(f"Skipped `{queue_skipped}` items (queue full).")
        if truncated:
            parts.append(
                f"`{truncated}` items were not loaded (playlist exceeds the "
                f"`{self.bot.settings.max_playlist_size}`-track limit)."
            )
        await context.send(" ".join(parts))
        await self._refresh_now_playing_message(context.guild.id)

    @playlist.command(name="delete")
    @commands.guild_only()
    async def playlist_delete(self, context: commands.Context[Any], name: str) -> None:
        await self._require_dj(context)
        if not await self.bot.database.delete_playlist(context.guild.id, name.lower()):
            await context.send("Playlist not found.")
            return
        await context.send(f"Deleted playlist `{name.lower()}`.")
