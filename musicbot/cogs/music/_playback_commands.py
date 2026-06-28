"""_playback_commands.py — PlaybackCommandsMixin: join/leave, transport, and panel commands.

Mixed into MusicCog.  Depends on CommandHelpersMixin, LifecycleMixin, NPanelMixin,
ExtractionMixin, and ResolverMixin methods via self.
"""

from __future__ import annotations
from musicbot.cogs.music._context import GuildContext

import dataclasses
import time

import discord
from discord.ext import commands

from musicbot.cogs.music.constants import (
    EMBED_COLOUR,
    LOOP_CYCLE,
    LOOP_ICONS,
    LOOP_LABELS,
    NOW_PLAYING_TIMEOUT_SECONDS,
)
from musicbot.cogs.music.models import NowPlayingController


from musicbot.cogs.music._base import MusicCogBase


class PlaybackCommandsMixin(MusicCogBase):
    """Join/leave, transport control, and now-playing panel commands."""

    @commands.hybrid_command(name="join", aliases=["summon"])  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(2, 5, commands.BucketType.user)
    async def join(self, context: GuildContext) -> None:
        """Dock into your current voice channel."""
        player = await self._join_for_context(context)
        if player.queue:
            self._kick_pipeline(context.guild.id)
        await context.send("Connected to your voice channel.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="leave", aliases=["disconnect"])  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def leave(self, context: GuildContext) -> None:
        """Disconnect and wipe the active session."""
        await self._require_dj(context)
        if not self.players.get(context.guild.id):
            await context.send("I am not connected.")
            return
        gid = context.guild.id
        await self._cleanup_guild(gid)
        await context.send("Disconnected and cleared the queue.")
        await self._refresh_now_playing_message(gid)

    @commands.hybrid_command(name="skipto")  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(2, 5, commands.BucketType.user)
    async def skipto(self, context: GuildContext, position: int) -> None:
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
        dropped = position - 1
        player.replace_queue(queue_list[position - 1 :])
        self._persist_snapshot(context.guild.id)
        target = player.queue[0]
        embed = discord.Embed(title="Jumped to Position", colour=EMBED_COLOUR)
        embed.add_field(
            name="Now Up Next",
            value=f"[{discord.utils.escape_markdown(target.title)}]({target.webpage_url})",
            inline=False,
        )
        embed.add_field(name="Position", value=f"`{position}`", inline=True)
        embed.add_field(name="Dropped", value=f"`{dropped}` track{'s' if dropped != 1 else ''}", inline=True)
        if target.thumbnail_url:
            embed.set_thumbnail(url=target.thumbnail_url)
        await context.send(embed=embed)
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="replay")  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(2, 5, commands.BucketType.user)
    async def replay(self, context: GuildContext) -> None:
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
        if self._check_per_user_limit(player, context.author.id):
            limit = self.bot.settings.max_queue_size_per_user
            await context.send(f"You already have `{limit}` tracks in the queue.")
            return
        clone = dataclasses.replace(player.current, requester_id=context.author.id)
        clone.stream_url = ""
        clone.resolved_at = 0.0
        await player.enqueue(clone, front=True)
        self._persist_snapshot(context.guild.id)
        await self._refresh_now_playing_message(context.guild.id)
        await context.send(f"Re-queued **{player.current.escaped_title}** to play next.")

    @commands.hybrid_command(name="play", aliases=["p"])  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(2, 4, commands.BucketType.user)
    async def play(self, context: GuildContext, *, query: str) -> None:
        """Queue a URL, playlist, or search query."""
        player = await self._join_for_context(context)
        self._kick_pipeline(context.guild.id)
        if len(player.queue) >= self.bot.settings.max_queue_size:
            await context.send("Queue is full.")
            return
        query = self._normalize_query(query)
        is_playlist = self._is_playlist_query(query)
        fetch_msg: discord.Message | None = await context.send(
            "⏳ Loading playlist…"
            if is_playlist
            else "🔍 Fetching…"
            if query.startswith(("http://", "https://"))
            else "🔍 Searching…"
        )
        async with context.typing():
            tracks, skipped = await self._extract_tracks(
                query,
                requester_id=context.author.id,
                guild_id=context.guild.id,
            )
        if not tracks:
            msg = (
                f"No playable results found. Skipped `{skipped}` unavailable items."
                if skipped
                else "No playable results found. Try `!search <query>` to browse manually."
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
            if added == 1
            else f"Queued `{added}` tracks.{suffix}"
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

    @commands.hybrid_command(name="playnext", aliases=["pn"])  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(2, 4, commands.BucketType.user)
    async def playnext(self, context: GuildContext, *, query: str) -> None:
        """Insert a track next in queue."""
        await self._require_dj(context)
        player = await self._join_for_context(context)
        query = self._normalize_query(query)
        fetch_msg = await context.send("🔍 Searching…")
        async with context.typing():
            tracks, _ = await self._extract_tracks(
                query,
                requester_id=context.author.id,
                guild_id=context.guild.id,
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

    @commands.hybrid_command(name="repeat", aliases=["rp"])  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(2, 4, commands.BucketType.user)
    async def repeat(self, context: GuildContext) -> None:
        """Toggle single-track repeat."""
        player = self.players.get(context.guild.id)
        if not player or not player.current:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        prev_label = LOOP_LABELS.get(player.loop_mode, "Off")
        player.loop_mode = "off" if player.loop_mode == "one" else "one"
        self._persist_snapshot(context.guild.id)
        label = LOOP_LABELS.get(player.loop_mode, "Off")
        icon = LOOP_ICONS.get(player.loop_mode, "→")
        await context.send(f"Loop changed: **{prev_label}** → {icon} **{label}**")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="skip", aliases=["next"])  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(3, 5, commands.BucketType.user)
    async def skip(self, context: GuildContext) -> None:
        """Vote-skip or instantly skip if you have control."""
        player = self.players.get(context.guild.id)
        if not player:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        await context.send(await self._skip_for_member(player, context.author))
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="prev", aliases=["previous", "back"])  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(2, 5, commands.BucketType.user)
    async def previous(self, context: GuildContext) -> None:
        """Jump back to the last completed track."""
        player = self.players.get(context.guild.id)
        if not player:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        await context.send(await self._previous_for_member(player, context.author))
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="forceskip", aliases=["fs"])  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(2, 4, commands.BucketType.user)
    async def forceskip(self, context: GuildContext) -> None:
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

    @commands.hybrid_command(name="stop")  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def stop(self, context: GuildContext) -> None:
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

    @commands.hybrid_command(name="pause")  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(3, 5, commands.BucketType.user)
    async def pause(self, context: GuildContext) -> None:
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

    @commands.hybrid_command(name="resume")  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(3, 5, commands.BucketType.user)
    async def resume(self, context: GuildContext) -> None:
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

    @commands.hybrid_command(name="nowplaying", aliases=["np"])  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def nowplaying(self, context: GuildContext) -> None:
        """Open the live control panel."""
        player = self.players.get(context.guild.id)
        if player:
            self._remember_channel(player, context.channel)
            await self._send_now_playing_panel(
                context.guild, player, channel=context.channel, replace_existing=True
            )
            return
        controller = NowPlayingController(
            channel_id=context.channel.id,
            message_id=0,
            expires_at=time.monotonic() + NOW_PLAYING_TIMEOUT_SECONDS,
        )
        await context.send(embed=self._render_now_playing_embed(context.guild, None, controller))

    @commands.hybrid_command(name="loop")  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(2, 4, commands.BucketType.user)
    async def loop(self, context: GuildContext) -> None:
        """Cycle loop mode: off → single track → full queue → off."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or (not player.current and not player.queue):
            await context.send("Nothing is loaded.")
            return
        self._remember_channel(player, context.channel)
        prev_label = LOOP_LABELS.get(player.loop_mode, "Off")
        new_mode = LOOP_CYCLE.get(player.loop_mode, "off")
        if new_mode in ("off", "one", "all"):
            player.loop_mode = new_mode  # type: ignore[assignment]
        self._persist_snapshot(context.guild.id)
        label = LOOP_LABELS.get(player.loop_mode, "Off")
        icon = LOOP_ICONS.get(player.loop_mode, "→")
        await context.send(f"Loop changed: **{prev_label}** → {icon} **{label}**")
        await self._refresh_now_playing_message(context.guild.id)
