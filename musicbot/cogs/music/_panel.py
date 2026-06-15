"""_panel.py — NPanelMixin: NowPlaying embed rendering, panel management, and refresh loop.

Mixed into MusicCog.  Accesses now_playing_messages, players, bot, logger, and the
NowPlayingView / QueueView classes through self.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from typing import Any, TYPE_CHECKING

import discord

from musicbot.cogs.music.constants import (
    EMBED_COLOUR,
    LOOP_ICONS,
    LOOP_LABELS,
    NOW_PLAYING_PREVIEW_LIMIT,
    NOW_PLAYING_TIMEOUT_SECONDS,
    NP_REFRESH_DEBOUNCE_SECONDS,
)
from musicbot.cogs.music.models import NowPlayingController, Track
from musicbot.cogs.music.player import GuildPlayer

if TYPE_CHECKING:
    pass


class NPanelMixin:
    """NowPlaying embed rendering, message send/edit, and debounced refresh loop."""

    # ── Formatting helpers ──────────────────────────────────────────────────

    @staticmethod
    def _format_duration(seconds: float) -> str:
        s = max(0, int(seconds))
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _format_progress_bar(self, elapsed: float, duration: float, *, width: int = 22) -> str:
        if duration <= 0:
            return f"`{'─' * width}`  Live"
        ratio = min(1.0, max(0.0, elapsed / duration))
        filled = round(ratio * width)
        if filled == 0:
            bar = "⬤" + "─" * width
        elif filled >= width:
            bar = "━" * width
        else:
            bar = "━" * (filled - 1) + "⬤" + "─" * (width - filled)
        e = self._format_duration(elapsed)
        t = self._format_duration(duration)
        return f"`{e}`  {bar}  `{t}`"

    def _queue_lines(self, player: GuildPlayer, *, limit: int) -> list[str]:
        lines = []
        for i, track in enumerate(itertools.islice(player.queue, limit), 1):
            dur = f"`{track.duration_label}`" if track.duration else "`…`"
            lines.append(f"`{i}.`  {discord.utils.escape_markdown(track.title[:44])}  {dur}")
        if len(player.queue) > limit:
            lines.append(f"*…and {len(player.queue) - limit} more*")
        return lines

    # ── Embed renderer ──────────────────────────────────────────────────────

    def _render_now_playing_embed(
        self,
        guild: discord.Guild,
        player: GuildPlayer | None,
        controller: NowPlayingController,
    ) -> discord.Embed:
        embed = discord.Embed(colour=EMBED_COLOUR)

        if not player or not player.current:
            embed.set_author(name="♪  Now Playing")
            embed.description = "*Nothing is playing right now.*"
            embed.set_footer(text="⏮ prev  ·  ⏭ skip  ·  ⏯ pause  ·  ↺ loop  ·  ≡ queue")
            return embed

        track = player.current
        is_paused = bool(player.voice_client and player.voice_client.is_paused())
        loop_icon = LOOP_ICONS.get(player.loop_mode, "→")
        loop_label = LOOP_LABELS.get(player.loop_mode, "Off")
        requester = guild.get_member(track.requester_id)
        req_label = requester.display_name if requester else f"<@{track.requester_id}>"

        embed.set_author(name=f"♪  Now Playing  ·  {'⏸  paused' if is_paused else '▶  playing'}")
        embed.description = (
            f"**[{track.escaped_title}]({track.webpage_url})**\n"
            f"{track.escaped_uploader}  ·  `{track.duration_label}`\n\n"
            f"{self._format_progress_bar(player.elapsed_seconds, track.duration)}"
        )

        if track.thumbnail_url:
            embed.set_thumbnail(url=track.thumbnail_url)

        queue_count = len(player.queue)
        if queue_count:
            queue_secs = int(player._total_duration)
            remaining = f"  ·  {self._format_duration(queue_secs)} remaining" if queue_secs else ""
            embed.add_field(
                name=f"Up Next  ·  {queue_count} track{'s' if queue_count != 1 else ''}{remaining}",
                value="\n".join(self._queue_lines(player, limit=NOW_PLAYING_PREVIEW_LIMIT))
                or "Nothing queued.",
                inline=False,
            )

        footer_parts = [f"{loop_icon} {loop_label}", f"req. {req_label}"]
        if controller.status_text:
            footer_parts.append(controller.status_text)
        embed.set_footer(text="  ·  ".join(footer_parts))
        return embed

    # ── Channel / controller helpers ────────────────────────────────────────

    def _controller(self, guild_id: int, *, message_id: int | None = None) -> NowPlayingController | None:
        controller = self.now_playing_messages.get(guild_id)  # type: ignore[attr-defined]
        if controller is None:
            return None
        if controller.expires_at <= time.monotonic():
            self.now_playing_messages.pop(guild_id, None)  # type: ignore[attr-defined]
            return None
        if message_id is not None and controller.message_id != message_id:
            return None
        return controller

    async def _fetch_announce_channel(
        self, guild: discord.Guild, player: GuildPlayer
    ) -> discord.abc.Messageable | None:
        if player.announce_channel_id is None:
            return None
        channel = self.bot.get_channel(player.announce_channel_id)  # type: ignore[attr-defined]
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(player.announce_channel_id)  # type: ignore[attr-defined]
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    # ── Panel send / edit ───────────────────────────────────────────────────

    async def _send_now_playing_panel(
        self,
        guild: discord.Guild,
        player: GuildPlayer,
        *,
        channel: discord.abc.Messageable | None = None,
        replace_existing: bool = False,
        status_text: str = "",
    ) -> discord.Message | None:
        from musicbot.cogs.music.views import NowPlayingView  # local — avoids circularity

        target_channel = channel or await self._fetch_announce_channel(guild, player)
        if target_channel is None:
            return None

        if replace_existing:
            existing = self._controller(guild.id)
            if existing and existing.message_id:
                ch = self.bot.get_channel(existing.channel_id)  # type: ignore[attr-defined]
                if ch and hasattr(ch, "get_partial_message"):
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await ch.get_partial_message(existing.message_id).delete()

        channel_id = getattr(target_channel, "id", None)
        if not isinstance(channel_id, int):
            return None

        view = NowPlayingView(self, guild.id)
        controller = NowPlayingController(
            channel_id=channel_id,
            message_id=0,
            expires_at=time.monotonic() + NOW_PLAYING_TIMEOUT_SECONDS,
            status_text=status_text,
        )
        message = await target_channel.send(
            embed=self._render_now_playing_embed(guild, player, controller),
            view=view,
        )
        controller.message_id = message.id
        self.now_playing_messages[guild.id] = controller  # type: ignore[attr-defined]
        return message

    async def _refresh_now_playing_message(self, guild_id: int) -> None:
        controller = self._controller(guild_id)
        guild = self.bot.get_guild(guild_id)  # type: ignore[attr-defined]
        if controller is None or guild is None:
            return
        channel = self.bot.get_channel(controller.channel_id)  # type: ignore[attr-defined]
        if channel is None or not hasattr(channel, "get_partial_message"):
            return
        player = self.players.get(guild_id)  # type: ignore[attr-defined]

        elapsed_bucket = int(player.elapsed_seconds // 4) if player else 0
        queue_preview = (
            tuple(t.title for t in itertools.islice(player.queue, NOW_PLAYING_PREVIEW_LIMIT))
            if player
            else ()
        )
        current_title = player.current.title if player and player.current else ""
        loop_mode = player.loop_mode if player else "off"
        is_paused = bool(player and player.voice_client and player.voice_client.is_paused())
        queue_count = len(player.queue) if player else 0
        queue_dur_bucket = int(player._total_duration // 30) if player else 0

        state_key = (
            current_title,
            elapsed_bucket,
            queue_preview,
            loop_mode,
            is_paused,
            controller.status_text,
            queue_count,
            queue_dur_bucket,
        )
        if getattr(controller, "_last_render_key", None) == state_key:
            return
        controller._last_render_key = state_key  # type: ignore[attr-defined]

        embed = self._render_now_playing_embed(guild, player, controller)
        partial = channel.get_partial_message(controller.message_id)
        with contextlib.suppress(discord.HTTPException, discord.NotFound):
            await partial.edit(embed=embed)

    # ── Debounced refresh loop ──────────────────────────────────────────────

    def _schedule_np_refresh(self, guild_id: int, *, delay: float = NP_REFRESH_DEBOUNCE_SECONDS) -> None:
        self._np_refresh_deadlines[guild_id] = time.monotonic() + delay  # type: ignore[attr-defined]
        task = self._np_refresh_tasks.get(guild_id)  # type: ignore[attr-defined]
        if task and not task.done():
            return
        self._np_refresh_tasks[guild_id] = self._bg_task(  # type: ignore[attr-defined]
            self._np_refresh_loop(guild_id),
            name=f"np-refresh-{guild_id}",
        )

    async def _np_refresh_loop(self, guild_id: int) -> None:
        try:
            while True:
                deadline = self._np_refresh_deadlines.get(guild_id)  # type: ignore[attr-defined]
                if deadline is None:
                    break
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                self._np_refresh_deadlines.pop(guild_id, None)  # type: ignore[attr-defined]
                await self._refresh_now_playing_message(guild_id)
        finally:
            self._np_refresh_tasks.pop(guild_id, None)  # type: ignore[attr-defined]
