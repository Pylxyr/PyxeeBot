"""_events.py — EventsMixin: bot and player event listeners.

Mixed into MusicCog.  Depends on players, bot, and helper methods from NPanelMixin,
ResolverMixin, ExtractionMixin, and LifecycleMixin.
"""

from __future__ import annotations

import contextlib

import discord
from discord.ext import commands

from musicbot.cogs.music.models import Track


class EventsMixin:
    """Bot and player event listeners."""

    # ── Event listeners ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_musicbot_np_auto_refresh(self, guild: discord.Guild) -> None:
        await self._refresh_now_playing_message(guild.id)

    @commands.Cog.listener()
    async def on_musicbot_track_skipped_error(self, guild: discord.Guild, track: Track, reason: str) -> None:
        if not self.bot.settings.error_announce:
            return
        player = self.players.get(guild.id)
        channel = await self._fetch_announce_channel(guild, player) if player else None
        if channel is None:
            channel = guild.system_channel
        if channel:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(f"Skipped **{track.escaped_title}** — {reason}")

    @commands.Cog.listener()
    async def on_musicbot_playback_error(self, guild: discord.Guild, error: Exception) -> None:
        player = self.players.get(guild.id)
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
        await self._send_now_playing_panel(guild, player, replace_existing=True, status_text="Track changed.")

    @commands.Cog.listener()
    async def on_musicbot_queue_updated(self, guild: discord.Guild) -> None:
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
        await self._cleanup_guild(guild.id)

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
                await self._cleanup_guild(member.guild.id)
            elif after.channel is not None and after.channel != before.channel:
                player.voice_client = member.guild.voice_client  # type: ignore[assignment]
                await player.refresh_empty_channel_state()
            return
        if before.channel == tracked_channel or after.channel == tracked_channel:
            await player.refresh_empty_channel_state()
