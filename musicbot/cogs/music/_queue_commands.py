"""_queue_commands.py — QueueCommandsMixin: queue inspection and mutation commands.

Mixed into MusicCog.  Depends on CommandHelpersMixin and LifecycleMixin methods via self.
"""

from __future__ import annotations

import random
from typing import Any

import discord
from discord.ext import commands

from musicbot.cogs.music.constants import EMBED_COLOUR


class QueueCommandsMixin:
    """Queue inspection and mutation commands."""

    @commands.hybrid_command(name="history")
    @commands.guild_only()
    async def history(self, context: commands.Context[Any]) -> None:
        """Show the last tracks played this session."""
        player = self.players.get(context.guild.id)
        hist = list(player.history) if player else []
        if not hist:
            await context.send("No tracks have been played this session.")
            return
        lines = [
            f"`{i}.` [{t.escaped_title}]({t.webpage_url}) `[{t.duration_label}]` — <@{t.requester_id}>"
            for i, t in enumerate(reversed(hist), start=1)
        ]
        embed = discord.Embed(title="Recent History", description="\n".join(lines[:20]), colour=EMBED_COLOUR)
        embed.set_footer(text=f"{len(hist)} track(s) in session history.")
        await context.send(embed=embed)

    @commands.hybrid_command(name="toptracks", aliases=["top"])
    @commands.guild_only()
    async def toptracks(self, context: commands.Context[Any]) -> None:
        """Show the most-played tracks for this server, all-time."""
        rows = await self.bot.database.get_top_played(context.guild.id, limit=10)
        if not rows:
            await context.send("No play history recorded yet.")
            return
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [
            f"{medals.get(i, f'`{i}.`')} **{row['title']}** — {row['play_count']}× plays"
            for i, row in enumerate(rows, start=1)
        ]
        embed = discord.Embed(
            title="Most Played Tracks",
            description="\n".join(lines),
            colour=EMBED_COLOUR,
        )
        embed.set_footer(text=f"{context.guild.name} · all-time")
        await context.send(embed=embed)

    @commands.hybrid_command(name="qsearch", aliases=["qs"])
    @commands.guild_only()
    async def qsearch(self, context: commands.Context[Any], *, keyword: str) -> None:
        """Search within the current queue."""
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is empty.")
            return
        kw = keyword.strip().lower()
        matches = [
            (i + 1, t)
            for i, t in enumerate(player.queue)
            if kw in t.title.lower() or kw in (t.uploader or "").lower()
        ]
        if not matches:
            await context.send(f"No tracks matching `{discord.utils.escape_markdown(keyword)}`.")
            return
        lines = [
            f"`{pos}.` [{discord.utils.escape_markdown(t.title)}]({t.webpage_url})" for pos, t in matches[:20]
        ]
        embed = discord.Embed(
            title=f"Queue Search: {discord.utils.escape_markdown(keyword)}",
            description="\n".join(lines),
            colour=EMBED_COLOUR,
        )
        embed.set_footer(
            text=(
                f"Showing first 20 of {len(matches)} matches."
                if len(matches) > 20
                else f"{len(matches)} match(es) found."
            )
        )
        await context.send(embed=embed)

    @commands.hybrid_command(name="queue", aliases=["q"])
    @commands.guild_only()
    async def queue(self, context: commands.Context[Any]) -> None:
        """Inspect the current track stack."""
        player = self.players.get(context.guild.id)
        if not player or (not player.current and not player.queue):
            await context.send("Queue is empty.")
            return
        self._remember_channel(player, context.channel)
        view = self._build_queue_view(context.guild.id, player, author_id=context.author.id)
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
        removed = queue_list[index - 1]
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
        await self._flush_snapshot(context.guild.id, entries=[])
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
        track = queue_list.pop(from_index - 1)
        queue_list.insert(to_index - 1, track)
        player.replace_queue(queue_list)
        self._persist_snapshot(context.guild.id)
        embed = discord.Embed(
            description=f"Moved **{track.escaped_title}** from `{from_index}` to `{to_index}`.",
            colour=EMBED_COLOUR,
        )
        if track.thumbnail_url:
            embed.set_thumbnail(url=track.thumbnail_url)
        await context.send(embed=embed)
        await self._refresh_now_playing_message(context.guild.id)
