from __future__ import annotations

import asyncio
import contextlib

import discord
from discord.ext import commands

from musicbot.cogs.music._base import MusicCogBase
from musicbot.cogs.music.models import Track


class EventsMixin(MusicCogBase):
    async def _schedule_rejoin(
        self,
        guild: discord.Guild,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        await asyncio.sleep(5.0)
        player = self.players.get(guild.id)
        if player is None or not player.stay_connected:
            return
        with contextlib.suppress(Exception):
            await player.connect(channel)

    async def _grace_reconnect_or_cleanup(
        self,
        guild_id: int,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        await asyncio.sleep(5.0)
        player = self.players.get(guild_id)
        if player is None:
            return
        if player.voice_client and player.voice_client.is_connected():
            return
        with contextlib.suppress(Exception):
            await player.connect(channel)
            return
        await self._cleanup_guild(guild_id)

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
                was_intentional = player.intentional_disconnect
                player.intentional_disconnect = False
                if was_intentional:
                    return
                if player.stay_connected:
                    self._bg_task(
                        self._schedule_rejoin(member.guild, before.channel),
                        name=f"rejoin-{member.guild.id}",
                    )
                else:
                    self._bg_task(
                        self._grace_reconnect_or_cleanup(member.guild.id, before.channel),
                        name=f"grace-cleanup-{member.guild.id}",
                    )
            elif after.channel is not None and after.channel != before.channel:
                player.voice_client = member.guild.voice_client
                await player.refresh_empty_channel_state()
            return
        if before.channel == tracked_channel or after.channel == tracked_channel:
            await player.refresh_empty_channel_state()
