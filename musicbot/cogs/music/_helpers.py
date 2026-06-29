"""_helpers.py — CommandHelpersMixin: permission checks and shared command-support utilities.

Mixed into MusicCog.  Depends on bot, players, _get_player, _persist_snapshot, _search_text,
and is called both from hybrid commands and from NowPlayingView button callbacks.
"""

from __future__ import annotations
from musicbot.cogs.music._context import GuildContext

import math

import discord
from discord.ext import commands

from musicbot.cogs.music.constants import LOOP_CYCLE, LOOP_ICONS, LOOP_LABELS
from musicbot.cogs.music.models import Track
from musicbot.cogs.music.player import GuildPlayer
from musicbot.cogs.music.views import QueueView, SearchSelectionView


from musicbot.cogs.music._base import MusicCogBase


class CommandHelpersMixin(MusicCogBase):
    """Permission checks and shared utilities used across commands and view callbacks."""

    # ── Permission helpers ──────────────────────────────────────────────────

    async def _ensure_author_voice(
        self, context: GuildContext
    ) -> discord.VoiceChannel | discord.StageChannel:
        voice_state = context.author.voice
        if not voice_state or not voice_state.channel:
            raise commands.BadArgument("Join a voice channel first.")
        return voice_state.channel

    def _voice_humans(self, channel: discord.abc.GuildChannel) -> list[discord.Member]:
        return [m for m in getattr(channel, "members", []) if not m.bot]

    def _is_bot_owner(self, user: discord.User | discord.Member) -> bool:
        if user.id in self.bot.settings.bot_owners:
            return True
        if self.bot.owner_id is not None and user.id == self.bot.owner_id:
            return True
        owner_ids = self.bot.owner_ids
        return owner_ids is not None and user.id in owner_ids

    async def _is_dj(self, member: discord.Member) -> bool:
        if self._is_bot_owner(member):
            return True
        if member.guild_permissions.manage_guild:
            return True
        role_id = await self.bot.database.get_dj_role_id(member.guild.id)
        return bool(role_id and any(r.id == role_id for r in member.roles))

    async def _require_dj(self, context: GuildContext) -> None:
        if not await self._is_dj(context.author):
            raise commands.CheckFailure("DJ role or Manage Server permission required.")

    async def _join_for_context(self, context: GuildContext) -> GuildPlayer:
        channel = await self._ensure_author_voice(context)
        player = await self._get_player(context.guild)
        self._remember_channel(player, context.channel)
        await player.connect(channel)
        return player

    def _remember_channel(self, player: GuildPlayer, channel: discord.abc.Messageable) -> None:
        channel_id = getattr(channel, "id", None)
        if isinstance(channel_id, int):
            player.set_announce_channel(channel_id)

    def _required_skip_votes(self, player: GuildPlayer) -> int:
        if not player.voice_client or not player.voice_client.channel:
            return 1
        return max(1, math.ceil(len(self._voice_humans(player.voice_client.channel)) / 2))

    def _is_in_player_voice(self, player: GuildPlayer, member: discord.Member) -> bool:
        return bool(
            player.voice_client
            and player.voice_client.channel
            and member in player.voice_client.channel.members
        )

    def _build_queue_view(
        self, guild_id: int, player: GuildPlayer, *, author_id: int, page: int = 0
    ) -> QueueView:
        return QueueView(self, guild_id, player, author_id=author_id, page_index=page)

    async def _prompt_for_search_selection(
        self,
        context: GuildContext,
        query: str,
        candidates: list[Track],
        *,
        mode: str,
    ) -> Track | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        view = SearchSelectionView(
            author_id=context.author.id,
            candidates=candidates,
            mode=mode,
            query_text=self._search_text(query),
            prefix=context.clean_prefix,
            bot_avatar_url=self.bot.user.display_avatar.url if self.bot.user else None,
            guild_icon_url=(context.guild.icon.url if context.guild and context.guild.icon else None),
        )
        prompt = await context.send(embed=view.build_embed(), view=view)
        view.message = prompt
        return await view.wait_for_selection()

    def _user_queue_count(self, player: GuildPlayer, user_id: int) -> int:
        return sum(1 for t in player.queue if t.requester_id == user_id)

    def _check_per_user_limit(self, player: GuildPlayer, user_id: int) -> bool:
        limit = self.bot.settings.max_queue_size_per_user
        return limit > 0 and self._user_queue_count(player, user_id) >= limit

    async def _skip_for_member(self, player: GuildPlayer, member: discord.Member) -> str:
        if not player.current or not player.voice_client or not player.voice_client.channel:
            return "Nothing is playing."
        if not self._is_in_player_voice(player, member):
            return "Join my voice channel to vote skip."
        if player.current.requester_id == member.id or await self._is_dj(member):
            player.skip_votes.clear()
            player.skip()
            return "Skipped the current track."
        player.skip_votes.add(member.id)
        needed = self._required_skip_votes(player)
        current_votes = len(player.skip_votes)
        if current_votes >= needed:
            player.skip_votes.clear()
            player.skip()
            return f"Skip vote passed with `{current_votes}` votes."
        return f"Skip vote added. `{current_votes}/{needed}` votes."

    async def _previous_for_member(self, player: GuildPlayer, member: discord.Member) -> str:
        if not self._is_in_player_voice(player, member):
            return "Join my voice channel first."
        if not await self._is_dj(member) and (not player.current or player.current.requester_id != member.id):
            return "Only the current requester or a DJ can go to the previous track."
        if not player.play_previous():
            return "There is no previous track to return to."
        return "Returned to the previous track."

    async def _toggle_pause_for_member(self, player: GuildPlayer, member: discord.Member) -> str:
        if not self._is_in_player_voice(player, member):
            return "Join my voice channel first."
        if not player.voice_client:
            return "Nothing is connected."
        if player.voice_client.is_paused():
            player.resume()
            return "Resumed playback."
        if player.voice_client.is_playing():
            player.pause()
            return "Paused playback."
        return "Nothing is playing."

    async def _toggle_loop_for_member(self, player: GuildPlayer, member: discord.Member) -> str:
        if not self._is_in_player_voice(player, member):
            return "Join my voice channel first."
        if not await self._is_dj(member):
            return "DJ role or Manage Server permission required."
        if not player.current and not player.queue:
            return "Nothing is loaded."
        prev_label = LOOP_LABELS.get(player.loop_mode, "Off")
        player.loop_mode = LOOP_CYCLE[player.loop_mode]
        self._persist_snapshot(member.guild.id)
        label = LOOP_LABELS.get(player.loop_mode, "Off")
        icon = LOOP_ICONS.get(player.loop_mode, "→")
        return f"Loop changed: **{prev_label}** → {icon} **{label}**"
