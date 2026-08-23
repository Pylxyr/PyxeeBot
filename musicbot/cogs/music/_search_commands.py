from __future__ import annotations

import discord
from discord.ext import commands

from musicbot.cogs.music._base import MusicCogBase
from musicbot.cogs.music._context import _CURRENT_GUILD_ID, GuildContext
from musicbot.cogs.music.constants import SEARCH_SELECTION_LIMIT


class SearchCommandsMixin(MusicCogBase):
    @commands.hybrid_command(name="search", aliases=["find", "s"])
    @commands.guild_only()
    @commands.cooldown(1, 6, commands.BucketType.user)
    async def search(self, context: GuildContext, *, query: str) -> None:
        player = await self._join_for_context(context)
        if len(player.queue) >= self.bot.settings.max_queue_size:
            await context.send("Queue is full.")
            return
        if self._check_per_user_limit(player, context.author.id):
            limit = self.bot.settings.max_queue_size_per_user
            await context.send(f"You already have `{limit}` tracks in the queue.")
            return
        self._remember_channel(player, context.channel)
        search_query = f"ytsearch{SEARCH_SELECTION_LIMIT}:{self._preprocess_query(query)}"
        async with context.typing():
            token = _CURRENT_GUILD_ID.set(context.guild.id)
            try:
                tracks, _ = await self._extract_search_candidates(
                    search_query, requester_id=context.author.id
                )
            finally:
                _CURRENT_GUILD_ID.reset(token)
        selected = await self._prompt_for_search_selection(context, search_query, tracks, mode="play")
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
