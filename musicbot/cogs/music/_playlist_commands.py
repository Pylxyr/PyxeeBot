from __future__ import annotations

import math

import discord
from discord.ext import commands

from musicbot.cogs.music._base import MusicCogBase
from musicbot.cogs.music._context import GuildContext
from musicbot.cogs.music.constants import EMBED_COLOUR
from musicbot.cogs.music.models import Track, format_requester


class PlaylistCommandsMixin(MusicCogBase):
    @commands.hybrid_group(name="playlist", invoke_without_command=True)
    @commands.guild_only()
    async def playlist(self, context: GuildContext) -> None:
        await context.send(
            "Use `playlist save`, `playlist load`, `playlist list`, `playlist show`, or `playlist delete`."
        )

    @playlist.command(name="save")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def playlist_save(self, context: GuildContext, name: str) -> None:
        player = self.players.get(context.guild.id)
        if not player or (not player.current and not player.queue):
            await context.send("Nothing is loaded to save.")
            return
        entries = player.snapshot()
        await self.bot.database.save_playlist(context.guild.id, name.lower(), context.author.id, entries)
        await context.send(f"Saved `{len(entries)}` tracks to playlist `{name.lower()}`.")

    @playlist.command(name="list")
    @commands.guild_only()
    @commands.cooldown(1, 4, commands.BucketType.user)
    async def playlist_list(self, context: GuildContext) -> None:
        rows = await self.bot.database.list_playlists(context.guild.id)
        if not rows:
            await context.send("No saved playlists for this server.")
            return
        show_mentions = await self.bot.database.get_show_requester_mentions(context.guild.id)
        PAGE = 25
        page_count = math.ceil(len(rows) / PAGE)
        for page in range(page_count):
            chunk = rows[page * PAGE : (page + 1) * PAGE]
            lines = [
                f"`{row['name']}` — {row['track_count']} tracks — "
                f"{format_requester(context.guild, row['created_by'], show_mentions=show_mentions)}"
                for row in chunk
            ]
            title = (
                "Saved Playlists" if page_count == 1 else f"Saved Playlists (page {page + 1}/{page_count})"
            )
            embed = discord.Embed(title=title, description="\n".join(lines), colour=EMBED_COLOUR)
            embed.set_footer(text=f"{len(rows)} playlist(s) total")
            await context.send(embed=embed)

    @playlist.command(name="show")
    @commands.guild_only()
    @commands.cooldown(1, 4, commands.BucketType.user)
    async def playlist_show(self, context: GuildContext, name: str) -> None:
        rows = await self.bot.database.get_playlist_entries(context.guild.id, name.lower())
        if not rows:
            await context.send("Playlist not found.")
            return
        PAGE = 15
        page_count = math.ceil(len(rows) / PAGE)
        for page in range(page_count):
            chunk = rows[page * PAGE : (page + 1) * PAGE]
            lines = [
                f"`{index}.` {discord.utils.escape_markdown(row['title'])}"
                for index, row in enumerate(chunk, start=page * PAGE + 1)
            ]
            title = (
                f"Playlist: {name.lower()}"
                if page_count == 1
                else f"Playlist: {name.lower()} (page {page + 1}/{page_count})"
            )
            embed = discord.Embed(title=title, description="\n".join(lines), colour=EMBED_COLOUR)
            embed.set_footer(text=f"{len(rows)} track(s) total")
            await context.send(embed=embed)

    @playlist.command(name="load")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def playlist_load(self, context: GuildContext, name: str) -> None:
        player = await self._join_for_context(context)
        rows = await self.bot.database.get_playlist_entries(context.guild.id, name.lower())
        if not rows:
            await context.send("Playlist not found.")
            return
        cap_rows = list(rows[: self.bot.settings.max_playlist_size])
        truncated = len(rows) - len(cap_rows)
        added = 0
        hit_user_limit = False
        async with context.typing():
            for row in cap_rows:
                if len(player.queue) >= self.bot.settings.max_queue_size:
                    break
                if self._check_per_user_limit(player, context.author.id):
                    hit_user_limit = True
                    break
                query = row["query"]
                webpage_url = row["webpage_url"] or query
                if not query or not webpage_url:
                    continue
                await player.enqueue(
                    Track(
                        title=row["title"],
                        webpage_url=webpage_url,
                        stream_url="",
                        uploader="Saved playlist",
                        duration=0,
                        requester_id=context.author.id,
                        query=query,
                    )
                )
                added += 1
        queue_skipped = len(cap_rows) - added
        self._persist_snapshot(context.guild.id)
        self._kick_pipeline(context.guild.id)
        parts: list[str] = [f"Loaded `{added}` tracks from playlist `{name.lower()}`."]
        if hit_user_limit:
            limit = self.bot.settings.max_queue_size_per_user
            parts.append(f"Stopped at your `{limit}`-track per-user limit.")
        elif queue_skipped:
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
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def playlist_delete(self, context: GuildContext, name: str) -> None:
        await self._require_dj(context)
        if not await self.bot.database.delete_playlist(context.guild.id, name.lower()):
            await context.send("Playlist not found.")
            return
        await context.send(f"Deleted playlist `{name.lower()}`.")
