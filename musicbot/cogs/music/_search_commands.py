"""_search_commands.py — SearchCommandsMixin: search browsing and score-debug commands.

Mixed into MusicCog.  Depends on CommandHelpersMixin and ExtractionMixin methods via self.
"""

from __future__ import annotations
from musicbot.cogs.music._context import GuildContext

import time

import discord
from discord.ext import commands

from musicbot.cogs.music._context import _CURRENT_GUILD_ID
from musicbot.cogs.music.constants import EMBED_COLOUR
from musicbot.cogs.music.views import ScoreDebugView


from musicbot.cogs.music._base import MusicCogBase


class SearchCommandsMixin(MusicCogBase):
    """Search browsing and score-debug commands."""

    @commands.hybrid_command(name="why", aliases=["searchdebug", "scorewhy"])  # type: ignore[arg-type]
    @commands.guild_only()
    async def why(self, context: GuildContext) -> None:
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
            sel = "  ✓" if c.selected else ""
            dur_m, dur_s = divmod(c.duration, 60)
            dur_label = f"{dur_m}:{dur_s:02d}" if c.duration else "?"
            detail = (
                f"title={c.title_overlap:.2f} artist={c.uploader_overlap:.2f} "
                f"anchor={c.anchor_score:+.2f} jp={c.jp_original_bonus:+.2f} "
                f"recency={c.recency_bonus:+.2f} views={c.view_bonus:+.2f} "
                f"penalty={-c.discouraged_penalty:+.2f}"
            )
            lines.append(
                f"`#{c.rank}` **{c.final_score:+.3f}**{sel} "
                f"[{discord.utils.escape_markdown(c.title[:52])}]({c.webpage_url})"
                f"\n└ `{dur_label}` · {detail}"
            )
        embed.description = "\n\n".join(lines) + stale_suffix if lines else "No data."
        embed.set_footer(text="Press the button for a full per-component DM breakdown.")
        await context.send(embed=embed, view=ScoreDebugView(author_id=context.author.id, record=record))

    @commands.hybrid_command(name="search", aliases=["find", "s"])  # type: ignore[arg-type]
    @commands.guild_only()
    @commands.cooldown(1, 6, commands.BucketType.user)
    async def search(self, context: GuildContext, *, query: str) -> None:
        """Browse search results and pick one to queue."""
        player = await self._join_for_context(context)
        if len(player.queue) >= self.bot.settings.max_queue_size:
            await context.send("Queue is full.")
            return
        self._remember_channel(player, context.channel)
        search_query = f"ytsearch{self._search_result_count(query)}:{self._preprocess_query(query)}"
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
