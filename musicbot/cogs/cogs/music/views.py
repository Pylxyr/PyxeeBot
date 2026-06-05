"""views.py — All discord.ui views for the music subsystem.

SearchSelectionView, QueueView, NowPlayingView, ScoreDebugView.
Fix #6: NowPlayingView.on_timeout uses channel.get_partial_message() instead
        of storing a full discord.Message object.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from typing import TYPE_CHECKING, Any

import discord

from musicbot.cogs.music.constants import (
    EMBED_COLOUR,
    LOOP_ICONS,
    LOOP_LABELS,
    NOW_PLAYING_TIMEOUT_SECONDS,
    QUEUE_PAGE_SIZE,
    QUEUE_VIEW_TIMEOUT_SECONDS,
    SEARCH_SELECTION_PAGE_SIZE,
    SEARCH_SELECTION_TIMEOUT_SECONDS,
)
from musicbot.cogs.music.models import NowPlayingController, SearchDebugRecord, Track

if TYPE_CHECKING:
    from musicbot.cogs.music.cog import MusicCog
    from musicbot.cogs.music.player import GuildPlayer


def _disable_view_items(view: discord.ui.View) -> None:
    for item in view.children:
        if hasattr(item, "disabled"):
            item.disabled = True


# ---------------------------------------------------------------------------
# Search selector
# ---------------------------------------------------------------------------

class SearchSelectionMenu(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose the exact track to queue...",
            min_values=1, max_values=1, row=0,
            options=[discord.SelectOption(label="Loading...", value="0")],
        )

    def refresh_options(self, parent: "SearchSelectionView") -> None:
        start = parent.page_index * SEARCH_SELECTION_PAGE_SIZE
        options: list[discord.SelectOption] = []
        for offset, track in enumerate(parent.current_page_candidates(), start=start + 1):
            duration = track.duration_label if track.duration else "pending"
            options.append(
                discord.SelectOption(
                    label=f"{offset}. {track.title[:90]}",
                    description=f"{track.uploader} | {duration}"[:100],
                    value=str(offset - 1),
                )
            )
        self.options = options

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            return
        await self.view.handle_selection(interaction, int(self.values[0]))


class SearchSelectionView(discord.ui.View):
    def __init__(
        self,
        *,
        author_id: int,
        candidates: list[Track],
        mode: str,
        query_text: str,
        prefix: str,
        bot_avatar_url: str | None = None,
        guild_icon_url: str | None = None,
    ) -> None:
        super().__init__(timeout=SEARCH_SELECTION_TIMEOUT_SECONDS)
        self.author_id      = author_id
        self.candidates     = candidates
        self.mode           = mode
        self.query_text     = query_text
        self.prefix         = prefix
        self.bot_avatar_url = bot_avatar_url
        self.guild_icon_url = guild_icon_url
        self.message: discord.Message | None = None
        self.page_index = 0
        self.page_count = max(1, math.ceil(len(candidates) / SEARCH_SELECTION_PAGE_SIZE))
        self.selection: asyncio.Future[Track | None] = asyncio.get_running_loop().create_future()
        self.menu = SearchSelectionMenu()
        self.add_item(self.menu)
        self._sync_controls()

    def current_page_candidates(self) -> list[Track]:
        start = self.page_index * SEARCH_SELECTION_PAGE_SIZE
        return self.candidates[start: start + SEARCH_SELECTION_PAGE_SIZE]

    def _sync_controls(self) -> None:
        self.menu.refresh_options(self)
        self.previous_page.disabled = self.page_index <= 0
        self.next_page.disabled     = self.page_index >= self.page_count - 1

    def build_embed(self) -> discord.Embed:
        action = "Queue next" if self.mode == "playnext" else "Queue"
        timeout_label = (
            f"{int(SEARCH_SELECTION_TIMEOUT_SECONDS // 60)} minutes"
            if SEARCH_SELECTION_TIMEOUT_SECONDS >= 60
            else f"{int(SEARCH_SELECTION_TIMEOUT_SECONDS)} seconds"
        )
        embed = discord.Embed(
            title="Pick A Search Result",
            description=(
                f"{action} a result for `{discord.utils.escape_markdown(self.query_text)}`.\n"
                f"Use the dropdown below within `{timeout_label}`."
            ),
            colour=EMBED_COLOUR,
        )
        if self.bot_avatar_url:
            embed.set_author(name="PyxeeBot Search Selector", icon_url=self.bot_avatar_url)
        page_candidates = self.current_page_candidates()
        start = self.page_index * SEARCH_SELECTION_PAGE_SIZE
        lines: list[str] = []
        for offset, track in enumerate(page_candidates, start=start + 1):
            duration = track.duration_label if track.duration else "pending"
            lines.append(
                f"`{offset}.` **{track.escaped_title}**"
                f" by {track.escaped_uploader}"
                f" `[{duration}]`"
            )
        embed.add_field(
            name=f"Results (page {self.page_index + 1}/{self.page_count})",
            value="\n".join(lines) or "No results.",
            inline=False,
        )
        thumbnail = next(
            (t.thumbnail_url for t in page_candidates if t.thumbnail_url), self.guild_icon_url
        )
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        return embed

    async def handle_selection(self, interaction: discord.Interaction, index: int) -> None:
        if self.selection.done():
            await interaction.response.defer()
            return
        if 0 <= index < len(self.candidates):
            self.selection.set_result(self.candidates[index])
        else:
            self.selection.set_result(None)
        self.stop()
        self.candidates = []   # release Track refs; the selection future holds the chosen one
        _disable_view_items(self)
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.edit_message(view=self)

    async def wait_for_selection(self) -> Track | None:
        try:
            return await asyncio.wait_for(
                asyncio.shield(self.selection), timeout=SEARCH_SELECTION_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran the search can pick a result.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page_index = max(0, self.page_index - 1)
        self._sync_controls()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page_index = min(self.page_count - 1, self.page_index + 1)
        self._sync_controls()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        if not self.selection.done():
            self.selection.set_result(None)
        self.candidates = []   # release Track refs
        _disable_view_items(self)
        if self.message:
            with contextlib.suppress(discord.HTTPException, discord.NotFound):
                await self.message.edit(view=self)


# ---------------------------------------------------------------------------
# Queue view
# ---------------------------------------------------------------------------

class QueueView(discord.ui.View):
    def __init__(
        self,
        cog: "MusicCog",
        guild_id: int,
        player: "GuildPlayer",
        *,
        author_id: int,
        page_index: int = 0,
    ) -> None:
        super().__init__(timeout=QUEUE_VIEW_TIMEOUT_SECONDS)
        self.cog        = cog
        self.guild_id   = guild_id
        self.player     = player
        self.author_id  = author_id
        self.page_index = page_index
        self.message: discord.Message | None = None
        self._sync_controls()

    def _page_count(self) -> int:
        total = len(self.player.queue) + (1 if self.player.current else 0)
        return max(1, math.ceil(total / QUEUE_PAGE_SIZE))

    def _sync_controls(self) -> None:
        self.previous_page.disabled = self.page_index <= 0
        self.next_page.disabled     = self.page_index >= self._page_count() - 1

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="Queue", colour=EMBED_COLOUR)
        tracks: list[Track] = []
        if self.player.current:
            tracks.append(self.player.current)
        tracks.extend(self.player.queue)

        start = self.page_index * QUEUE_PAGE_SIZE
        page  = tracks[start: start + QUEUE_PAGE_SIZE]

        lines: list[str] = []
        for i, track in enumerate(page, start=start):
            duration = track.duration_label if track.duration else "pending"
            prefix   = "▶" if i == 0 and self.player.current else f"{i}."
            lines.append(
                f"`{prefix}` [{track.escaped_title}]"
                f"({track.webpage_url}) `[{duration}]` — <@{track.requester_id}>"
            )
        embed.description = "\n".join(lines) if lines else "Nothing queued."

        summary: list[str] = [f"{len(tracks)} track(s)"]
        if self._page_count() > 1:
            summary.append(f"Page {self.page_index + 1}/{self._page_count()}")
        # Use the running total from GuildPlayer rather than iterating the whole queue.
        total_secs = self.player._total_duration + (self.player.current.duration if self.player.current else 0)
        if total_secs > 0:
            h, rem = divmod(int(total_secs), 3600)
            m, s   = divmod(rem, 60)
            summary.append(f"Total: `{h}:{m:02d}:{s:02d}`" if h else f"Total: `{m}:{s:02d}`")
        embed.add_field(name="Summary", value=" • ".join(summary), inline=False)
        embed.set_footer(text="Use the buttons below to browse the queue.")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        player = self.cog.players.get(self.guild_id)
        if player and self.cog._is_in_player_voice(player, interaction.user):
            return True
        if interaction.user and interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "Join my voice channel to browse the queue.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page_index = max(0, self.page_index - 1)
        self._sync_controls()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page_index = min(self._page_count() - 1, self.page_index + 1)
        self._sync_controls()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close_panel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)

    async def on_timeout(self) -> None:
        _disable_view_items(self)
        if self.message is None:
            return
        with contextlib.suppress(discord.HTTPException, discord.NotFound):
            await self.message.edit(view=self)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _: discord.ui.Item[Any],
    ) -> None:
        logging.getLogger(__name__).exception("QueueView interaction failed", exc_info=error)
        if not interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "That queue interaction failed. Run the command again.", ephemeral=True
                )


# ---------------------------------------------------------------------------
# Now-playing view
# ---------------------------------------------------------------------------

class NowPlayingView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int) -> None:
        super().__init__(timeout=NOW_PLAYING_TIMEOUT_SECONDS)
        self.cog      = cog
        self.guild_id = guild_id
        # Wire the pause/resume button reference eagerly from self.children,
        # which is already populated by the @discord.ui.button decorators during
        # super().__init__(). This avoids the lazy-first-press approach that
        # would leave _pause_btn=None whenever other buttons are pressed first.
        self._pause_btn: discord.ui.Button | None = next(
            (
                item for item in self.children
                if isinstance(item, discord.ui.Button)
                and getattr(item, "callback", None) is not None
                and getattr(item.callback, "__name__", "") == "pause_resume"
            ),
            None,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        player = self.cog.players.get(self.guild_id)
        if player and self.cog._is_in_player_voice(player, interaction.user):
            return True
        await interaction.response.send_message(
            "Join my voice channel to use the controls.", ephemeral=True
        )
        return False

    def _sync_pause_emoji(self, player: "GuildPlayer | None") -> None:
        if self._pause_btn is None:
            return
        is_paused = bool(player and player.voice_client and player.voice_client.is_paused())
        self._pause_btn.emoji = discord.PartialEmoji(name="▶" if is_paused else "⏸")

    async def _respond(self, interaction: discord.Interaction, status_text: str) -> None:
        controller = self.cog._controller(self.guild_id)
        if controller is None:
            await interaction.response.defer()
            return
        controller.status_text = status_text
        controller.expires_at  = time.monotonic() + NOW_PLAYING_TIMEOUT_SECONDS
        guild  = self.cog.bot.get_guild(self.guild_id)
        player = self.cog.players.get(self.guild_id)
        if guild is None:
            await interaction.response.defer()
            return
        self._sync_pause_emoji(player)
        embed = self.cog._render_now_playing_embed(guild, player, controller)
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog.players.get(self.guild_id)
        if player is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        msg = await self.cog._previous_for_member(player, interaction.user)
        await self._respond(interaction, msg)

    @discord.ui.button(emoji="\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog.players.get(self.guild_id)
        if player is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        msg = await self.cog._skip_for_member(player, interaction.user)
        await self._respond(interaction, msg)

    @discord.ui.button(emoji="\N{BLACK RIGHT-POINTING TRIANGLE WITH DOUBLE VERTICAL BAR}", style=discord.ButtonStyle.secondary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.cog.players.get(self.guild_id)
        if player is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        msg = await self.cog._toggle_pause_for_member(player, interaction.user)
        await self._respond(interaction, msg)

    @discord.ui.button(emoji="\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog.players.get(self.guild_id)
        if player is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        msg = await self.cog._toggle_loop_for_member(player, interaction.user)
        await self._respond(interaction, msg)

    @discord.ui.button(emoji="\N{SCROLL}", style=discord.ButtonStyle.secondary)
    async def queue(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog.players.get(self.guild_id)
        if player is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        view = self.cog._build_queue_view(self.guild_id, player, author_id=interaction.user.id, page=1)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    async def on_timeout(self) -> None:
        _disable_view_items(self)
        # Fix #6: use get_partial_message — no full Message object stored.
        controller = self.cog.now_playing_messages.get(self.guild_id)
        if controller is None or time.monotonic() >= controller.expires_at:
            return
        channel = self.cog.bot.get_channel(controller.channel_id)
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            return
        partial = channel.get_partial_message(controller.message_id)  # type: ignore[attr-defined]
        with contextlib.suppress(discord.HTTPException, discord.NotFound):
            await partial.edit(view=self)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _: discord.ui.Item[Any],
    ) -> None:
        logging.getLogger(__name__).exception("NowPlayingView interaction failed", exc_info=error)
        if not interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "That control failed. Try the command again.", ephemeral=True
                )


# ---------------------------------------------------------------------------
# Score debug view
# ---------------------------------------------------------------------------

class ScoreDebugView(discord.ui.View):
    def __init__(self, author_id: int, record: SearchDebugRecord) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.record    = record

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran `!why` can use this button.", ephemeral=True
        )
        return False

    @discord.ui.button(label="DM me full breakdown", style=discord.ButtonStyle.secondary)
    async def dm_breakdown(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        lines: list[str] = [
            f"**Search score breakdown** — `{discord.utils.escape_markdown(self.record.query_text)}`\n"
        ]
        for c in self.record.candidates:
            sel       = " ← **queued**" if c.selected else ""
            dur_m, dur_s = divmod(c.duration, 60)
            dur_label = f"{dur_m}:{dur_s:02d}" if c.duration else "?"
            lines.append(
                f"**#{c.rank}** [{discord.utils.escape_markdown(c.title[:80])}]({c.webpage_url})"
                f" `{dur_label}`{sel}\n"
                f"```\n"
                f"FINAL          {c.final_score:+.4f}\n"
                f"title_overlap  {c.title_overlap:+.3f}\n"
                f"upldr_overlap  {c.uploader_overlap:+.3f}\n"
                f"seq_ratio      {c.ratio:+.3f}\n"
                f"topic_bonus    {c.topic_bonus:+.3f}\n"
                f"upldr_pref     {c.uploader_pref_bonus:+.3f}\n"
                f"anchor         {c.anchor_score:+.3f}\n"
                f"artist_match   {c.artist_match_bonus:+.3f}\n"
                f"completion     {c.artist_completion_bonus:+.3f}\n"
                f"synergy        {c.title_uploader_synergy:+.3f}\n"
                f"preferred      {c.preferred_bonus:+.3f}\n"
                f"discouraged   {-c.discouraged_penalty:+.3f}\n"
                f"jp_original    {c.jp_original_bonus:+.3f}\n"
                f"view_count     {c.view_bonus:+.3f}\n"
                f"verified       {c.verified_bonus:+.3f}\n"
                f"duration       {c.duration_bonus:+.3f}\n"
                f"```\n"
            )
        chunks: list[str] = []
        current = ""
        for block in lines:
            if len(current) + len(block) > 1900:
                chunks.append(current)
                current = block
            else:
                current += block
        if current:
            chunks.append(current)
        try:
            for chunk in chunks:
                await interaction.user.send(chunk)
            await interaction.followup.send("Sent to your DMs.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "Couldn't DM you — enable DMs from server members in your privacy settings.",
                ephemeral=True,
            )

    async def on_timeout(self) -> None:
        _disable_view_items(self)
