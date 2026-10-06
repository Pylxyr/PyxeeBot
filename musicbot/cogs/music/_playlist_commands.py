from __future__ import annotations

import math

import discord
from discord.ext import commands

from musicbot.cogs.music._base import MusicCogBase
from musicbot.cogs.music._context import GuildContext
from musicbot.cogs.music.constants import (
    EMBED_COLOUR,
    MAX_PLAYLIST_NAME_LENGTH,
    MAX_SAVED_PLAYLISTS_PER_GUILD,
)
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
        playlist_name = name.lower()
        if len(playlist_name) > MAX_PLAYLIST_NAME_LENGTH:
            await context.send(f"Playlist names are limited to `{MAX_PLAYLIST_NAME_LENGTH}` characters.")
            return
        owner_id = await self.bot.database.get_playlist_owner(context.guild.id, playlist_name)
        if owner_id is None:
            if await self.bot.database.count_playlists(context.guild.id) >= MAX_SAVED_PLAYLISTS_PER_GUILD:
                await context.send(
                    f"This server already has `{MAX_SAVED_PLAYLISTS_PER_GUILD}` saved playlists — "
                    "delete one first."
                )
                return
        elif owner_id != context.author.id and not await self._is_dj(context.author):
            await context.send("Only the playlist's creator or a DJ can overwrite it.")
            return
        entries = player.snapshot()
        await self.bot.database.save_playlist(context.guild.id, playlist_name, context.author.id, entries)
        await context.send(f"Saved `{len(entries)}` tracks to playlist `{playlist_name}`.")

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
                if not query:
                    continue
                await player.enqueue(
                    Track(
                        title=row["title"],
                        webpage_url=row["webpage_url"] or "",
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
