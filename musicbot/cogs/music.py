from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
from difflib import SequenceMatcher
import itertools
import logging
import math
import random
import re
import time
from functools import lru_cache
from collections import OrderedDict, deque
import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord
from discord.ext import commands
from yt_dlp import DownloadError, YoutubeDL

if TYPE_CHECKING:
    from musicbot.bot import MusicBot

# ContextVar so _extract_info can find the current guild's semaphore without
# threading guild_id through every call in the chain.
_CURRENT_GUILD_ID: ContextVar[int | None] = ContextVar("_CURRENT_GUILD_ID", default=None)


FFMPEG_BEFORE_OPTIONS = (
    "-nostdin "
    "-threads 1 "
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_delay_max 5"
)
FFMPEG_OPTIONS = "-vn -ar 48000 -ac 2"  # Discord native: 48kHz stereo, no resampling


YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": False,
    "skip_download": True,
    "default_search": "ytsearch",
    # Silently skip unavailable/private/deleted playlist entries instead of
    # aborting the entire extraction.
    "ignoreerrors": True,
    # source_address intentionally omitted: binding to 0.0.0.0 can cause
    # ENETUNREACH on Oracle instances where the public IP is provided via NAT.
    "extract_flat": False,
}


NOW_PLAYING_PREVIEW_LIMIT = 5
QUEUE_MESSAGE_LIMIT = 20
QUEUE_PAGE_SIZE = 8
QUEUE_VIEW_TIMEOUT_SECONDS = 300
NOW_PLAYING_TIMEOUT_SECONDS = 1800
SNAPSHOT_DEBOUNCE_SECONDS = 0.5
STREAM_URL_REFRESH_AGE_SECONDS = 4 * 60 * 60
SEARCH_SELECTION_PAGE_SIZE = 5
SEARCH_SELECTION_LIMIT = 10
SEARCH_SELECTION_TIMEOUT_SECONDS = 600
VOICE_RECONNECT_ATTEMPTS = 2

# Loop modes cycle: off -> one -> all -> off
LOOP_CYCLE: dict[str, str] = {"off": "one", "one": "all", "all": "off"}
LOOP_LABELS: dict[str, str] = {"off": "Off", "one": "Single track", "all": "Entire queue"}
LOOP_ICONS: dict[str, str] = {"off": "→", "one": "↻¹", "all": "↻"}

NP_REFRESH_DEBOUNCE_SECONDS = 0.8
EMBED_COLOUR = discord.Colour.from_rgb(255, 170, 64)  # bot brand colour

# Tokens that carry NO signal when alone (ignored in scoring unless combined).
SEARCH_GENERIC_TOKENS = {
    "audio", "full", "hd", "hq", "lyrics", "lyric",
    "music", "official", "song", "ver", "version", "video",
}
# These ARE signal tokens in anime/OST contexts — never strip them.
# (e.g. "naruto op", "chainsaw man ost", "one piece opening")
SEARCH_ANIME_SIGNAL_TOKENS = {"op", "ed", "ost", "opening", "ending", "theme", "anime", "tv"}

# Penalise results that contain these tokens unless the user asked for them.
SEARCH_DISCOURAGED_TOKENS: dict[str, float] = {
    "amv": 0.60, "cast": 0.70, "cover": 0.60, "edit": 0.15,
    "instrumental": 0.60, "karaoke": 0.70, "nightcore": 0.70,
    "remix": 0.45, "reverb": 0.22, "seiyuu": 0.70, "slowed": 0.45,
    "live": 0.20,
    # Instrument tokens — title containing these almost always means a cover upload
    "guitar": 0.50, "piano": 0.50, "violin": 0.45,
    "acoustic": 0.35, "fingerstyle": 0.55, "ukulele": 0.55,
    "bass": 0.45, "drums": 0.45, "drum": 0.40,
    "flute": 0.45, "cello": 0.45, "harp": 0.45, "saxophone": 0.45,
    # Lyrics/translation uploads — not the original track
    "lyrics": 0.80, "lyric": 0.50, "romaji": 0.70,
    "subtitles": 0.35, "kanji": 0.35, "translation": 0.45,
}
SEARCH_DISCOURAGED_PHRASES: dict[str, float] = {
    "cast version": 0.80, "cast ver": 0.75, "character song": 0.65,
    "female version": 0.40, "male version": 0.40,
    "lyric video": 0.45,
    "lyrics video": 0.50,
    "with lyrics": 0.50,
    "english cover": 0.80,
    "first take": 0.65,   # THE FIRST TAKE = studio live, not the original release
    "short ver": 0.30, "short version": 0.30, "sped up": 0.45,
    "tv size": 0.22,
    # Shortened anime broadcast versions — not the full release
    "anime size": 0.40, "anime ver": 0.35, "anime version": 0.35,
    "op ver": 0.35, "ed ver": 0.35,
    # Hour-long loops — almost never the original track
    "1 hour": 0.90, "one hour": 0.90, "10 hours": 0.90,
    "2 hours": 0.90, "3 hours": 0.90,
    # Extended/compilation markers
    "extended mix": 0.30, "full album": 0.60,
    "compilation": 0.50, "best of": 0.35,
}
# Reward results that contain these phrases (quality signals).
SEARCH_PREFERRED_PHRASES: dict[str, float] = {
    "official audio": 0.30,       # strongest: clean studio audio / audio-first official release
    "official music video": 0.22,  # official visual upload
    "official mv": 0.20,           # common abbreviation
    "official ver": 0.18,
    "official version": 0.18,
    "official video": 0.16,       # slightly weaker
    "music video": 0.20,
}
# Reward results whose uploader name contains these tokens.
SEARCH_PREFERRED_UPLOADER_TOKENS: dict[str, float] = {
    "topic": 0.35,    # "Artist - Topic" = YouTube Music auto-generated, cleanest source
    "vevo": 0.28,     # label-owned official channel
    # Korean music labels
    "hybe": 0.22, "bighit": 0.22, "smtown": 0.22,
    "ygentertainment": 0.22, "jyp": 0.18, "starship": 0.16,
    "official": 0.22,  # "ArtistOfficial" channels
    "records": 0.10,
    "music": 0.06,
    # Japanese music labels
    "avex": 0.18, "ponycanyon": 0.18, "kingrecords": 0.18,
    "sonymusic": 0.18, "columbia": 0.15, "victor": 0.15,
    "tokyorecords": 0.15, "lantis": 0.15, "kicm": 0.12,
    # International labels
    "universal": 0.14, "warner": 0.14, "atlantic": 0.14,
    "capitol": 0.14, "interscope": 0.12, "republic": 0.12,
}

# Regex patterns that signal the user is searching for anime content.
# When detected, TV-size / OP / ED penalties are relaxed.
_ANIME_INTENT_RE = re.compile(
    r"\b(op|ed|ost|opening|ending|theme|insert\s*song|anime|season)\b",
    re.IGNORECASE,
)
# "artist - song" pattern — strong intent signal for exact title
_DASH_SEPARATED_RE = re.compile(r"^.+\s*[-–]\s*.+$")

# Japanese bracket prefix that almost always signals an instrument cover or
# "I tried playing/singing it" upload — NOT the original track.
# e.g. 【ギター】, 【ピアノ】, 【弾いてみた】, 【歌ってみた】
_JP_COVER_BRACKET_RE = re.compile(
    r"^[\s\[【\(]*"
    r"(ギター|ピアノ|バイオリン|チェロ|ベース|ドラム|弾いてみた|歌ってみた|叩いてみた|カバー|アレンジ|フル)"
    r"[\s\]】\)]*",
    re.IGNORECASE,
)
# Strip bracket/parenthesis annotation from titles before CJK ratio check
# e.g. "ヨルシカ - 春泥棒（OFFICIAL VIDEO）" → "ヨルシカ - 春泥棒"
_BRACKET_STRIP_RE = re.compile(r'[\(\[（【][^\)\]）】]*[\)\]）】]')
# Matches any CJK or kana character
_CJK_RE = re.compile(r'[\u3040-\u30ff\u4e00-\u9fff]')
# Korean Hangul — used to detect Korean fan content vs Japanese originals
_HANGUL_RE = re.compile(r'[\uAC00-\uD7AF\u3130-\u318F]')


def _wb(p: str, t: str) -> bool:
    """Word-boundary check: phrase must appear as whole word(s) in text."""
    return (
        p == t
        or t.startswith(p + " ")
        or t.endswith(" " + p)
        or (" " + p + " ") in t
    )


@lru_cache(maxsize=4096)
def _normalize_text_cached(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


@lru_cache(maxsize=4096)
def _tokenize_text_cached(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def _disable_view_items(view: discord.ui.View) -> None:
    for item in view.children:
        if hasattr(item, "disabled"):
            item.disabled = True


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    stream_url: str
    uploader: str
    duration: int
    requester_id: int
    query: str
    thumbnail_url: str = ""
    resolved_at: float = 0.0
    tags: list[str] = field(default_factory=list)

    @property
    def duration_label(self) -> str:
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


@dataclass(slots=True)
class ResolvedTrackData:
    title: str
    webpage_url: str
    stream_url: str
    uploader: str
    duration: int
    query: str
    resolved_at: float
    thumbnail_url: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NowPlayingController:
    channel_id: int
    message_id: int
    expires_at: float
    status_text: str = ""
    # Cached Message object so _refresh_now_playing_message never needs to
    # fetch it via HTTP — one API call saved on every queue update.
    message: discord.Message | None = field(default=None, compare=False)


@dataclass(slots=True)
class SearchQueryContext:
    normalized_query: str
    raw_query_tokens: list[str]
    query_tokens: list[str]
    query_token_set: set[str]
    anchor_phrases: list[str]
    intent: dict[str, bool] = field(default_factory=dict)


@dataclass(slots=True)
class SearchEntryContext:
    item: dict[str, Any]
    normalized_title: str
    normalized_uploader: str
    normalized_metadata: str
    title_tokens: list[str]
    uploader_tokens: list[str]
    metadata_tokens: list[str]
    title_token_set: set[str]
    uploader_token_set: set[str]
    metadata_token_set: set[str]
    duration: int
    view_count: int        # raw view count from yt-dlp flat extraction
    channel_is_verified: bool  # blue checkmark from yt-dlp


@dataclass(slots=True)
class ScoreBreakdown:
    """Per-candidate score components captured during the last search."""
    rank: int
    title: str
    uploader: str
    webpage_url: str
    duration: int
    final_score: float
    title_overlap: float
    uploader_overlap: float
    ratio: float
    topic_bonus: float
    uploader_pref_bonus: float
    anchor_score: float
    artist_match_bonus: float
    artist_completion_bonus: float
    title_uploader_synergy: float
    preferred_bonus: float
    discouraged_penalty: float
    duration_bonus: float
    jp_original_bonus: float = 0.0
    view_bonus: float = 0.0
    verified_bonus: float = 0.0
    selected: bool = False


@dataclass(slots=True)
class SearchDebugRecord:
    query_text: str
    guild_id: int
    timestamp: float
    candidates: list[ScoreBreakdown]
# ---------------------------------------------------------------------------

class SearchSelectionMenu(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose the exact track to queue...",
            min_values=1,
            max_values=1,
            row=0,
            options=[discord.SelectOption(label="Loading...", value="0")],
        )

    def refresh_options(self, parent: "SearchSelectionView") -> None:
        start = parent.page_index * SEARCH_SELECTION_PAGE_SIZE
        options: list[discord.SelectOption] = []
        for offset, track in enumerate(parent.current_page_candidates(), start=start + 1):
            duration = track.duration_label if track.duration else "pending"
            description = f"{track.uploader} | {duration}"
            options.append(
                discord.SelectOption(
                    label=f"{offset}. {track.title[:90]}",
                    description=description[:100],
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
        self.author_id = author_id
        self.candidates = candidates
        self.mode = mode
        self.query_text = query_text
        self.prefix = prefix
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
        end = start + SEARCH_SELECTION_PAGE_SIZE
        return self.candidates[start:end]

    def _sync_controls(self) -> None:
        self.menu.refresh_options(self)
        self.previous_page.disabled = self.page_index <= 0
        self.next_page.disabled = self.page_index >= self.page_count - 1

    def _set_embed_art(self, embed: discord.Embed, track: Track | None = None) -> None:
        if self.bot_avatar_url:
            embed.set_author(name="PyxeeBot Search Selector", icon_url=self.bot_avatar_url)
        art_url = track.thumbnail_url if track and track.thumbnail_url else self.guild_icon_url
        if art_url:
            embed.set_thumbnail(url=art_url)

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
        page_tracks = self.current_page_candidates()
        self._set_embed_art(embed, page_tracks[0] if page_tracks else None)

        lines = []
        start = self.page_index * SEARCH_SELECTION_PAGE_SIZE
        for offset, track in enumerate(page_tracks, start=start + 1):
            duration = track.duration_label if track.duration else "pending"
            uploader = discord.utils.escape_markdown(track.uploader or "Unknown uploader")
            title = discord.utils.escape_markdown(track.title)
            lines.append(f"`{offset}.` [{title}]({track.webpage_url})\n`{duration}` by `{uploader}`")

        embed.add_field(
            name=f"Results • Page `{self.page_index + 1}/{self.page_count}`",
            value="\n\n".join(lines) if lines else "No results on this page.",
            inline=False,
        )
        if self.page_count > 1:
            embed.add_field(
                name="Navigation",
                value="Use `Previous` and `Next` to browse more matches before you pick one.",
                inline=False,
            )
        embed.set_footer(text=f"{self.prefix}{self.mode} opens this selector for text searches.")
        return embed

    def build_status_embed(
        self,
        title: str,
        description: str,
        colour: discord.Colour,
        *,
        track: Track | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(title=title, description=description, colour=colour)
        self._set_embed_art(embed, track)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "Only the requester can use this selector.", ephemeral=True
        )
        return False

    async def _dismiss_selector(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        target = self.message or interaction.message
        if target is None:
            return
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await target.delete()
        self.message = None

    async def handle_selection(self, interaction: discord.Interaction, index: int) -> None:
        selected = self.candidates[index]
        if not self.selection.done():
            self.selection.set_result(selected)
        _disable_view_items(self)
        await self._dismiss_selector(interaction)
        self.stop()

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

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.selection.done():
            self.selection.set_result(None)
        _disable_view_items(self)
        await self._dismiss_selector(interaction)
        self.stop()

    async def on_timeout(self) -> None:
        if not self.selection.done():
            self.selection.set_result(None)
        _disable_view_items(self)
        if self.message is None:
            return
        embed = self.build_status_embed(
            "Search Selection Timed Out",
            "Run the command again when you want to choose a result.",
            discord.Colour.red(),
        )
        with contextlib.suppress(discord.HTTPException):
            await self.message.edit(embed=embed, view=self)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _: discord.ui.Item[Any],
    ) -> None:
        logging.getLogger(__name__).exception("Search selector interaction failed", exc_info=error)
        # Always resolve the future so wait_for_selection() doesn't hang.
        if not self.selection.done():
            self.selection.set_result(None)
        self.stop()
        if interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "That selector interaction failed. Run the search again.",
                    ephemeral=True,
                )
            return
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(
                "That selector interaction failed. Run the search again.",
                ephemeral=True,
            )

    async def wait_for_selection(self) -> Track | None:
        return await self.selection


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Queue browser
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
        self.cog = cog
        self.guild_id = guild_id
        self.player = player
        self.author_id = author_id
        self.page_index = max(0, page_index)
        self.message: discord.Message | None = None
        self._sync_controls()

    def _queue_tracks(self) -> list[Track]:
        return list(self.player.queue)

    def _page_count(self) -> int:
        return max(1, math.ceil(max(1, len(self.player.queue)) / QUEUE_PAGE_SIZE))

    def _sync_controls(self) -> None:
        page_count = self._page_count()
        self.page_index = min(self.page_index, page_count - 1)
        self.previous_page.disabled = self.page_index <= 0
        self.next_page.disabled = self.page_index >= page_count - 1

    def build_embed(self) -> discord.Embed:
        guild = self.cog.bot.get_guild(self.guild_id)
        current = self.player.current
        tracks = self._queue_tracks()
        page_count = self._page_count()
        start = self.page_index * QUEUE_PAGE_SIZE
        page_tracks = tracks[start:start + QUEUE_PAGE_SIZE]

        embed = discord.Embed(
            title="Queue Overview",
            colour=EMBED_COLOUR,
        )

        if current is not None:
            requester = guild.get_member(current.requester_id) if guild else None
            requester_label = requester.mention if requester else f"<@{current.requester_id}>"
            duration = current.duration_label if current.duration else "pending"
            embed.add_field(
                name="Now Playing",
                value=(
                    f"[{discord.utils.escape_markdown(current.title)}]({current.webpage_url})\n"
                    f"`{duration}` • requested by {requester_label}"
                ),
                inline=False,
            )

        if page_tracks:
            lines: list[str] = []
            for index, track in enumerate(page_tracks, start=start + 1):
                requester = guild.get_member(track.requester_id) if guild else None
                requester_label = requester.mention if requester else f"<@{track.requester_id}>"
                duration = track.duration_label if track.duration else "pending"
                title = discord.utils.escape_markdown(track.title)
                if len(title) > 55:
                    title = f"{title[:52]}..."
                lines.append(
                    f"`{index}.` {title} • `{duration}` • {requester_label}"
                )
            embed.add_field(
                name=f"Up Next • Page `{self.page_index + 1}/{page_count}`",
                value="\n\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Up Next",
                value="Nothing queued after the current track.",
                inline=False,
            )

        summary = [
            f"Loaded: `{len(tracks) + (1 if current else 0)}`",
            f"Upcoming: `{len(tracks)}`",
            f"Loop: `{LOOP_LABELS.get(self.player.loop_mode, 'Off')}`",
        ]
        total_secs = sum(t.duration for t in tracks) + (current.duration if current else 0)
        if total_secs > 0:
            h, rem = divmod(int(total_secs), 3600)
            m, s = divmod(rem, 60)
            total_label = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            summary.append(f"Total: `{total_label}`")
        embed.add_field(name="Summary", value=" • ".join(summary), inline=False)
        embed.set_footer(text="Use the buttons below to browse the queue.")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # FIX #12: any member in the bot's voice channel can browse the queue —
        # the original requester-only lock was inconsistent with NowPlayingView.
        player = self.cog.players.get(self.guild_id)
        if player and self.cog._is_in_player_voice(player, interaction.user):
            return True
        # Fall back to original-requester if user isn't in VC (e.g. mobile / gone).
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
        # FIX #11: guard against editing a message that was already deleted or
        # never assigned (e.g. ephemeral panels from NowPlayingView).
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
                    "That queue interaction failed. Run the command again.",
                    ephemeral=True,
                )


# ---------------------------------------------------------------------------
# Now-playing button panel
# ---------------------------------------------------------------------------

class NowPlayingView(discord.ui.View):
    """Button panel attached to the now-playing embed.

    Each button responds entirely within its own interaction — no HTTP fetch
    needed to find the message, no reaction removal round-trip.
    """

    def __init__(self, cog: "MusicCog", guild_id: int) -> None:
        super().__init__(timeout=NOW_PLAYING_TIMEOUT_SECONDS)
        self.cog = cog
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        player = self.cog.players.get(self.guild_id)
        if player and self.cog._is_in_player_voice(player, interaction.user):
            return True
        await interaction.response.send_message(
            "Join my voice channel to use the controls.", ephemeral=True
        )
        return False

    async def _respond(self, interaction: discord.Interaction, status_text: str) -> None:
        """Update the controller status and refresh the embed in one interaction response."""
        controller = self.cog._controller(self.guild_id)
        if controller is None:
            await interaction.response.defer()
            return
        controller.status_text = status_text
        controller.expires_at = time.monotonic() + NOW_PLAYING_TIMEOUT_SECONDS
        guild = self.cog.bot.get_guild(self.guild_id)
        player = self.cog.players.get(self.guild_id)
        if guild is None:
            await interaction.response.defer()
            return
        # FIX #16: keep the pause/resume button emoji in sync with player state.
        self._sync_pause_emoji(player)
        embed = self.cog._render_now_playing_embed(guild, player, controller)
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.edit_message(embed=embed, view=self)

    def _sync_pause_emoji(self, player: "GuildPlayer | None") -> None:
        """Set the pause/resume button emoji to match the actual playback state."""
        is_paused = bool(player and player.voice_client and player.voice_client.is_paused())
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.callback.__name__ == "pause_resume":
                item.emoji = discord.PartialEmoji(name="▶" if is_paused else "⏸")
                break

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
    async def pause_resume(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
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
        view = self.cog._build_queue_view(
            self.guild_id,
            player,
            author_id=interaction.user.id,
            page=1,
        )
        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )

    async def on_timeout(self) -> None:
        _disable_view_items(self)
        # FIX #11: only edit if the controller is still alive for this guild —
        # skip the API call when the session was already closed/replaced.
        controller = self.cog.now_playing_messages.get(self.guild_id)
        if controller and controller.message and time.monotonic() < controller.expires_at:
            with contextlib.suppress(discord.HTTPException):
                await controller.message.edit(view=self)

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
# Score debug view (attached to !why responses)
# ---------------------------------------------------------------------------

class ScoreDebugView(discord.ui.View):
    """Offers a DM with the full per-component score breakdown from the last search."""

    def __init__(self, author_id: int, record: "SearchDebugRecord") -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.record = record

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
            sel = " ← **queued**" if c.selected else ""
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
        # Split into <=1900-char chunks to respect DM limits.
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
# ---------------------------------------------------------------------------

class GuildPlayer:
    def __init__(
        self,
        bot: "MusicBot",
        guild: discord.Guild,
        track_resolver: Callable[[Track], Awaitable[Track | None]],
        audio_source_factory: Callable[[Track], Awaitable[discord.AudioSource]],
    ) -> None:
        self.bot = bot
        self.guild = guild
        self.track_resolver = track_resolver
        self.audio_source_factory = audio_source_factory
        self.logger = logging.getLogger(f"musicbot.player.{guild.id}")
        self.voice_client: discord.VoiceClient | None = None
        self.queue: deque[Track] = deque()
        self.history: deque[Track] = deque(maxlen=20)
        self.current: Track | None = None
        self.announce_channel_id: int | None = None
        self.loop_mode: str = "off"  # "off" | "one" | "all"
        self.rewind_requested = False
        self._connected_at: float = 0.0   # stamped on connect(), used for stutter guard
        self.next_event = asyncio.Event()
        self.idle_task: asyncio.Task[None] | None = None
        self.empty_channel_task: asyncio.Task[None] | None = None
        self.near_end_task: asyncio.Task[None] | None = None
        self.np_refresh_task: asyncio.Task[None] | None = None
        self.skip_votes: set[int] = set()
        # Playback timing (for progress bar)
        self.started_at: float = 0.0
        self._pause_started: float = 0.0
        self._total_paused: float = 0.0
        # Per-track yt-dlp resolve failure count for exponential backoff
        self._resolve_fail_counts: dict[str, int] = {}
        self.player_task = asyncio.create_task(self._player_loop())

    async def connect(self, channel: discord.VoiceChannel | discord.StageChannel) -> discord.VoiceClient:
        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.channel != channel:
                await self.voice_client.move_to(channel)
                await self.refresh_empty_channel_state()
            return self.voice_client
        self.voice_client = await channel.connect(self_deaf=True)
        self._connected_at = time.monotonic()
        await self.refresh_empty_channel_state()
        return self.voice_client

    async def enqueue(self, track: Track, *, front: bool = False) -> None:
        if front:
            self.queue.appendleft(track)
        else:
            self.queue.append(track)
        self.next_event.set()

    def pause(self) -> bool:
        """Pause playback and record when the pause started."""
        if not self.voice_client or not self.voice_client.is_playing():
            return False
        self.voice_client.pause()
        self._pause_started = time.monotonic()
        return True

    def resume(self) -> bool:
        """Resume playback and accumulate paused time."""
        if not self.voice_client or not self.voice_client.is_paused():
            return False
        if self._pause_started > 0:
            self._total_paused += time.monotonic() - self._pause_started
            self._pause_started = 0.0
        self.voice_client.resume()
        return True

    @property
    def elapsed_seconds(self) -> float:
        """Seconds of audio that have actually played (excludes pause time)."""
        if self.started_at <= 0:
            return 0.0
        elapsed = time.monotonic() - self.started_at - self._total_paused
        if self._pause_started > 0:
            elapsed -= time.monotonic() - self._pause_started
        return max(0.0, elapsed)

    def set_announce_channel(self, channel_id: int) -> None:
        self.announce_channel_id = channel_id

    async def stop(self) -> None:
        self.queue.clear()
        self.history.clear()
        self.loop_mode = "off"
        self.rewind_requested = False
        self.skip_votes.clear()
        self.started_at = 0.0
        self._pause_started = 0.0
        self._total_paused = 0.0
        self._resolve_fail_counts.clear()
        await self._cancel_near_end_task()
        await self._cancel_np_refresh_task()
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        self.current = None

    def play_previous(self) -> bool:
        if not self.history:
            return False
        previous_track = self.history.pop()
        if self.current:
            self.queue.appendleft(self.current)
        self.queue.appendleft(previous_track)
        self.rewind_requested = True
        self.next_event.set()
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        return True

    async def disconnect(self) -> None:
        if self.idle_task:
            self.idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.idle_task
            self.idle_task = None
        if self.empty_channel_task:
            self.empty_channel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.empty_channel_task
            self.empty_channel_task = None
        await self._cancel_near_end_task()
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect(force=False)
        self.voice_client = None
        self.current = None
        self.started_at = 0.0
        self._pause_started = 0.0
        self._total_paused = 0.0
        self._resolve_fail_counts.clear()
        self.history.clear()
        self.rewind_requested = False
        self.skip_votes.clear()
        await self._cancel_np_refresh_task()

    async def _cancel_near_end_task(self) -> None:
        if self.near_end_task is None:
            return
        self.near_end_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.near_end_task
        self.near_end_task = None

    async def _cancel_np_refresh_task(self) -> None:
        if self.np_refresh_task is None:
            return
        self.np_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.np_refresh_task
        self.np_refresh_task = None

    def skip(self) -> bool:
        """Signal the voice client to stop, triggering the after_playback callback."""
        if self.voice_client and (
            self.voice_client.is_playing() or self.voice_client.is_paused()
        ):
            self.voice_client.stop()
            return True
        return False

    async def _trigger_near_end_preload(self, delay_seconds: float) -> None:
        await asyncio.sleep(delay_seconds)
        self.bot.dispatch("musicbot_track_near_end", self.guild)

    async def _auto_refresh_np_loop(self) -> None:
        """Periodically dispatch a refresh event so the NP embed bar stays current."""
        interval = self.bot.settings.np_auto_refresh_interval
        try:
            while True:
                await asyncio.sleep(interval)
                if not self.current:
                    break
                self.bot.dispatch("musicbot_np_auto_refresh", self.guild)
        except asyncio.CancelledError:
            pass

    async def destroy(self) -> None:
        await self.stop()
        await self.disconnect()
        self.player_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.player_task

    def snapshot(self) -> list[dict[str, Any]]:
        entries: list[Track] = []
        if self.current:
            entries.append(self.current)
        entries.extend(self.queue)
        return [
            {
                "query": track.query,
                "title": track.title,
                "webpage_url": track.webpage_url,
                "requester_id": track.requester_id,
            }
            for track in entries
        ]

    async def _player_loop(self) -> None:
        _guild_ctx_token = _CURRENT_GUILD_ID.set(self.guild.id)
        try:
            while True:
                try:
                    await self._wait_for_track()
                    if not self.current:
                        continue

                    # ContextVar already set before the loop; refresh each iteration
                    # so child coroutines always see the correct guild.
                    _CURRENT_GUILD_ID.set(self.guild.id)
                    resolved_track = await self.track_resolver(self.current)
                    if resolved_track is None:
                        # Track failed to resolve — apply exponential backoff then
                        # notify the cog so it can post an error message.
                        key = self.current.query or self.current.webpage_url
                        fails = self._resolve_fail_counts.get(key, 0) + 1
                        # Cap dict size to avoid unbounded growth on broken playlists.
                        if len(self._resolve_fail_counts) > 200:
                            self._resolve_fail_counts.clear()
                        self._resolve_fail_counts[key] = fails
                        backoff = min(30.0, 1.0 * (2 ** (fails - 1)))
                        self.bot.dispatch(
                            "musicbot_track_skipped_error",
                            self.guild,
                            self.current,
                            f"Could not resolve stream (attempt {fails}). Retrying in {backoff:.0f}s." if fails < 4
                            else "Track is unavailable, skipping.",
                        )
                        self.current = None
                        if fails < 4:
                            await asyncio.sleep(backoff)
                        self.bot.dispatch("musicbot_queue_updated", self.guild)
                        continue
                    # Clear fail count on success
                    key = resolved_track.query or resolved_track.webpage_url
                    self._resolve_fail_counts.pop(key, None)
                    self.current = resolved_track

                    # FIX #8: only HEAD-validate URLs that are near expiry
                    # (>= STREAM_URL_REFRESH_AGE_SECONDS old). The FFmpeg reconnect
                    # flags already handle transient CDN hiccups on fresh URLs, so
                    # the 1800 s (30 min) threshold was adding 200–600 ms of latency
                    # before every track for no benefit.
                    _url_age = time.monotonic() - resolved_track.resolved_at
                    if (
                        resolved_track.stream_url
                        and _url_age >= STREAM_URL_REFRESH_AGE_SECONDS
                        and not await self.bot.get_cog("MusicCog")._validate_stream_url(resolved_track)  # type: ignore[union-attr]
                    ):
                        self.logger.info(
                            "Stream URL pre-validation failed for %s, forcing re-resolve.",
                            resolved_track.webpage_url,
                        )
                        resolved_track.stream_url = ""
                        resolved_track.resolved_at = 0.0
                        re_resolved = await self.track_resolver(self.current)
                        if re_resolved is None:
                            self.bot.dispatch(
                                "musicbot_track_skipped_error",
                                self.guild,
                                self.current,
                                "Stream URL expired and could not be refreshed, skipping.",
                            )
                            self.current = None
                            self.bot.dispatch("musicbot_queue_updated", self.guild)
                            continue
                        self.current = re_resolved

                    source = await self.audio_source_factory(self.current)

                    finished = asyncio.Event()

                    def after_playback(error: Exception | None) -> None:
                        if error:
                            self.bot.loop.call_soon_threadsafe(
                                self.bot.dispatch,
                                "musicbot_playback_error",
                                self.guild,
                                error,
                            )
                        self.bot.loop.call_soon_threadsafe(finished.set)

                    # Attempt reconnect if the voice client dropped between resolve and play.
                    if not self.voice_client or not self.voice_client.is_connected():
                        reconnected = await self._try_reconnect()
                        if not reconnected:
                            self.current = None
                            continue

                    self.skip_votes.clear()
                    await self._cancel_near_end_task()

                    # Give the WebRTC voice connection time to fill its jitter buffer
                    # on the first play after a fresh join. Without this, the first
                    # ~0.5 s sounds choppy as frames arrive before the buffer is ready.
                    if self._connected_at > 0:
                        wait = 0.75 - (time.monotonic() - self._connected_at)
                        if wait > 0:
                            await asyncio.sleep(wait)
                        self._connected_at = 0.0  # only guard the very first play

                    self.started_at = time.monotonic()
                    self._pause_started = 0.0
                    self._total_paused = 0.0
                    self.voice_client.play(source, after=after_playback)
                    preload_window = max(
                        0,
                        min(
                            self.bot.settings.near_end_prefetch_seconds,
                            max(self.current.duration - 1, 0),
                        ),
                    )
                    if self.current.duration > 0 and (self.queue or self.loop_mode != "off"):
                        self.near_end_task = asyncio.create_task(
                            self._trigger_near_end_preload(
                                max(self.current.duration - preload_window, 0)
                            )
                        )
                    # Start auto-refresh task for the Now Playing progress bar.
                    if self.bot.settings.np_auto_refresh:
                        await self._cancel_np_refresh_task()
                        self.np_refresh_task = asyncio.create_task(
                            self._auto_refresh_np_loop()
                        )
                    self.bot.dispatch("musicbot_track_started", self.guild, self.current)
                    await finished.wait()

                    await self._cancel_near_end_task()
                    await self._cancel_np_refresh_task()
                    played_track = self.current
                    self.current = None
                    self.started_at = 0.0
                    self._pause_started = 0.0
                    self._total_paused = 0.0
                    self.skip_votes.clear()
                    if played_track and not self.rewind_requested:
                        self.history.append(played_track)
                    if played_track and not self.rewind_requested:
                        if self.loop_mode == "one":
                            # Keep fresh URL — only evict if truly expired.
                            age = time.monotonic() - played_track.resolved_at
                            if played_track.resolved_at > 0 and age >= STREAM_URL_REFRESH_AGE_SECONDS:
                                played_track.stream_url = ""
                                played_track.resolved_at = 0.0
                            self.queue.appendleft(played_track)
                        elif self.loop_mode == "all":
                            # Always re-resolve: the URL will be expired by the time
                            # this track rotates back to the front of a long queue.
                            played_track.stream_url = ""
                            played_track.resolved_at = 0.0
                            self.queue.append(played_track)
                    self.rewind_requested = False
                    self.bot.dispatch("musicbot_queue_updated", self.guild)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - defensive guard
                    await self._cancel_near_end_task()
                    self.bot.dispatch("musicbot_playback_error", self.guild, exc)
                    self.current = None
                    # Brief sleep so a persistent failure doesn't spin the event loop.
                    await asyncio.sleep(1)
        finally:
            _CURRENT_GUILD_ID.reset(_guild_ctx_token)

    async def _try_reconnect(self) -> bool:
        """Attempt to reconnect to the last known voice channel."""
        channel = (
            self.voice_client.channel
            if self.voice_client
            else None
        )
        if channel is None:
            return False
        for attempt in range(1, VOICE_RECONNECT_ATTEMPTS + 1):
            try:
                logging.getLogger(__name__).warning(
                    "Voice client disconnected mid-session for guild %s, "
                    "reconnect attempt %d/%d",
                    self.guild.id,
                    attempt,
                    VOICE_RECONNECT_ATTEMPTS,
                )
                self.voice_client = await channel.connect(self_deaf=True)
                return True
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Reconnect attempt %d failed: %s", attempt, exc
                )
                await asyncio.sleep(1.0 * attempt)
        return False

    async def _wait_for_track(self) -> None:
        while not self.queue:
            self.current = None
            self.next_event.clear()
            if self.idle_task is None or self.idle_task.done():
                self.idle_task = asyncio.create_task(self._disconnect_when_idle())
            await self.next_event.wait()

        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.idle_task
        self.idle_task = None
        self.current = self.queue.popleft()
        self.bot.dispatch("musicbot_queue_updated", self.guild)

    async def _disconnect_when_idle(self) -> None:
        await asyncio.sleep(self.bot.settings.idle_timeout_seconds)
        if not self.current and not self.queue:
            await self.stop()
            await self.disconnect()
            self.bot.dispatch("musicbot_queue_updated", self.guild)

    def _has_human_listeners(self) -> bool:
        if not self.voice_client or not self.voice_client.channel:
            return False
        return any(not member.bot for member in self.voice_client.channel.members)

    async def refresh_empty_channel_state(self) -> None:
        if not self.voice_client or not self.voice_client.is_connected():
            if self.empty_channel_task:
                self.empty_channel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.empty_channel_task
                self.empty_channel_task = None
            return
        if self._has_human_listeners():
            if self.empty_channel_task:
                self.empty_channel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.empty_channel_task
                self.empty_channel_task = None
            return
        if self.empty_channel_task is None or self.empty_channel_task.done():
            self.empty_channel_task = asyncio.create_task(self._disconnect_when_empty())

    async def _disconnect_when_empty(self) -> None:
        await asyncio.sleep(self.bot.settings.empty_channel_timeout_seconds)
        if (
            self.voice_client
            and self.voice_client.is_connected()
            and not self._has_human_listeners()
        ):
            await self.stop()
            await self.disconnect()
            self.bot.dispatch("musicbot_queue_updated", self.guild)


# ---------------------------------------------------------------------------
# Music cog
# ---------------------------------------------------------------------------

class MusicCog(commands.Cog):
    def __init__(self, bot: "MusicBot") -> None:
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.now_playing_messages: dict[int, NowPlayingController] = {}
        self.logger = logging.getLogger(__name__)
        self._warned_missing_cookiefile = False
        self._ytdl_base_options: dict[str, Any] | None = None  # lazy cache
        self._ytdl_variants: dict[tuple[bool, bool], dict[str, Any]] | None = None
        self.resolve_tasks: dict[str, asyncio.Task[ResolvedTrackData | None]] = {}
        self.prefetch_tasks: dict[int, asyncio.Task[None]] = {}
        self.snapshot_tasks: dict[int, asyncio.Task[None]] = {}
        self.np_refresh_tasks: dict[int, asyncio.Task[None]] = {}
        self.resolve_cache: OrderedDict[str, tuple[float, ResolvedTrackData]] = OrderedDict()
        self.extract_semaphore = asyncio.Semaphore(self.bot.settings.ytdlp_concurrent_extracts)
        # Per-guild semaphores so one slow guild can't block others.
        # Each guild gets its own 1-slot semaphore; the global one is a system-wide cap.
        self._guild_extract_semaphores: dict[int, asyncio.Semaphore] = {}
        # Per-guild last search debug record for !why command (OrderedDict for LRU eviction)
        self._last_search: OrderedDict[int, SearchDebugRecord] = OrderedDict()
        # Shared HTTP session — available immediately so the YouTube API client
        # can be used during search before any track is played.
        self._http_session: aiohttp.ClientSession = aiohttp.ClientSession()

        # Oracle Micro memory management
        self._resolve_cache_max = 500  # ~25MB cap
        self._last_search_max = 100    # ~500KB cap

    # ------------------------------------------------------------------
    # yt-dlp helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_duration(seconds: float) -> str:
        s = max(0, int(seconds))
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _format_progress_bar(
        self, elapsed: float, duration: float, *, width: int = 16
    ) -> str:
        if duration <= 0:
            return f"`{'─' * width}`  Live"
        ratio = min(1.0, max(0.0, elapsed / duration))
        filled = round(ratio * width)
        bar = "▓" * filled + "░" * (width - filled)
        return f"`{bar}` {self._format_duration(elapsed)} / {self._format_duration(duration)}"

    def _build_ytdl_options(
        self, *, flat_playlist: bool = False, flat_search: bool = False
    ) -> dict[str, Any]:
        # Build base options once and cache all 4 variants so callers never copy
        # a dict more than once per cold start (FIX #9).
        if self._ytdl_base_options is None:
            base = dict(YTDL_OPTIONS)
            base["socket_timeout"] = self.bot.settings.ytdlp_socket_timeout
            base["playlistend"] = self.bot.settings.max_playlist_size
            if self.bot.settings.ytdlp_cookies_file:
                if self.bot.settings.ytdlp_cookies_file.exists():
                    base["cookiefile"] = str(self.bot.settings.ytdlp_cookies_file)
                elif not self._warned_missing_cookiefile:
                    self.logger.warning(
                        "YTDLP_COOKIES_FILE does not exist: %s",
                        self.bot.settings.ytdlp_cookies_file,
                    )
                    self._warned_missing_cookiefile = True
            if self.bot.settings.ytdlp_js_runtime_path:
                base["js_runtimes"] = {"node": {"path": self.bot.settings.ytdlp_js_runtime_path}}
            self._ytdl_base_options = base
            # Pre-build all four variants once.
            fp = dict(base); fp["extract_flat"] = "in_playlist"; fp["lazy_playlist"] = True
            fs = dict(base); fs["extract_flat"] = True
            fps = dict(fp); fps["extract_flat"] = True
            self._ytdl_variants: dict[tuple[bool, bool], dict[str, Any]] = {
                (False, False): dict(base),
                (True,  False): fp,
                (False, True):  fs,
                (True,  True):  fps,
            }
        return dict(self._ytdl_variants[(flat_playlist, flat_search)])

    async def _validate_stream_url(self, track: Track) -> bool:
        """Quick HEAD check that the stream URL is still alive. Returns False if dead."""
        url = track.stream_url
        if not url or not url.startswith("http"):
            return False
        session = self._http_session
        if session is None or session.closed:
            self._http_session = aiohttp.ClientSession()
            session = self._http_session
        try:
            async with session.head(
                url, timeout=aiohttp.ClientTimeout(total=5), allow_redirects=True
            ) as resp:
                return resp.status < 400
        except Exception:
            return False

    async def _build_audio_source(self, track: Track) -> discord.AudioSource:
        try:
            source: discord.AudioSource = await discord.FFmpegOpusAudio.from_probe(
                track.stream_url,
                method="fallback",
                before_options=FFMPEG_BEFORE_OPTIONS,
                options=FFMPEG_OPTIONS,
            )
        except (discord.ClientException, OSError, TypeError, ValueError) as exc:
            self.logger.warning("Opus probe fallback engaged for %s: %s", track.webpage_url, exc)
            try:
                source = discord.FFmpegOpusAudio(
                    track.stream_url,
                    bitrate=self.bot.settings.opus_bitrate_kbps,
                    codec="libopus",
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                )
            except (discord.ClientException, OSError, TypeError, ValueError) as exc2:
                self.logger.warning(
                    "Opus source fallback failed for %s: %s — skipping track.",
                    track.webpage_url,
                    exc2,
                )
                raise

        return source

    # ------------------------------------------------------------------
    # Search scoring
    # ------------------------------------------------------------------

    def _normalize_query(self, query: str) -> str:
        query = self._preprocess_query(query)
        if query.startswith(("http://", "https://")) or query.startswith("ytsearch"):
            return query
        return f"ytsearch{self._search_result_count(query)}:{query}"

    def _search_text(self, query: str) -> str:
        match = re.match(r"^ytsearch(?:all|\d+)?:", query, flags=re.IGNORECASE)
        if not match:
            return query.strip()
        return query[match.end():].strip()

    def _normalize_text(self, value: str) -> str:
        return _normalize_text_cached(value)

    def _tokenize_text(self, value: str) -> tuple[str, ...]:
        return _tokenize_text_cached(value)

    def _signal_query_tokens(self, query: str) -> list[str]:
        tokens = self._tokenize_text(query)
        # Always keep anime/OST signal tokens even though they're "generic" alone.
        filtered = [
            t for t in tokens
            if t not in SEARCH_GENERIC_TOKENS or t in SEARCH_ANIME_SIGNAL_TOKENS
        ]
        return filtered or tokens

    def _detect_query_intent(self, query: str) -> dict[str, bool]:
        """Detect what kind of result the user most likely wants."""
        q = query.strip()
        return {
            "anime":        bool(_ANIME_INTENT_RE.search(q)),
            "dash_format":  bool(_DASH_SEPARATED_RE.match(q)),
            "has_artist":   " " in q.strip(),
        }

    def _preprocess_query(self, raw_query: str) -> str:
        """Normalise the raw query before passing to yt-dlp.

        - Strips leading/trailing whitespace
        - Collapses multiple spaces
        - Keeps URLs and ytsearch: prefixes untouched
        """
        if raw_query.startswith(("http://", "https://")) or raw_query.startswith("ytsearch"):
            return raw_query
        # Collapse whitespace and strip padding
        cleaned = re.sub(r"\s+", " ", raw_query).strip()
        return cleaned

    def _token_overlap_ratio(
        self, query_tokens: list[str], candidate_tokens: list[str]
    ) -> float:
        if not query_tokens or not candidate_tokens:
            return 0.0
        candidate_set = set(candidate_tokens)
        matches = sum(1 for token in query_tokens if token in candidate_set)
        return matches / len(query_tokens)

    def _search_result_count(self, query: str) -> int:
        base_count = max(self.bot.settings.ytdlp_search_results, SEARCH_SELECTION_LIMIT)
        signal_tokens = self._signal_query_tokens(query)
        if len(signal_tokens) >= 4:
            return max(base_count, 8)
        if len(signal_tokens) >= 3:
            return max(base_count, 6)
        return base_count

    def _candidate_title_text(self, item: dict[str, Any]) -> str:
        return self._normalize_text(str(item.get("title") or ""))

    def _candidate_uploader_text(self, item: dict[str, Any]) -> str:
        uploader_parts = [
            item.get("channel"),
            item.get("uploader"),
            item.get("artist"),
            item.get("creator"),
        ]
        uploader = " ".join(
            part.strip()
            for part in dict.fromkeys(
                str(part) for part in uploader_parts
                if isinstance(part, str) and part.strip()
            )
        )
        return self._normalize_text(uploader)

    def _prepare_search_entry(self, item: dict[str, Any]) -> SearchEntryContext:
        normalized_title = self._candidate_title_text(item)
        normalized_uploader = self._candidate_uploader_text(item)
        # FIX #3: tokenize the already-normalized strings (one regex pass each)
        # instead of re-normalizing the raw title a second time.
        title_tokens = self._tokenize_text(normalized_title)
        uploader_tokens = self._tokenize_text(normalized_uploader)
        metadata_tokens = title_tokens + uploader_tokens
        return SearchEntryContext(
            item=item,
            normalized_title=normalized_title,
            normalized_uploader=normalized_uploader,
            normalized_metadata=" ".join(
                part for part in (normalized_title, normalized_uploader) if part
            ),
            title_tokens=title_tokens,
            uploader_tokens=uploader_tokens,
            metadata_tokens=metadata_tokens,
            title_token_set=set(title_tokens),
            uploader_token_set=set(uploader_tokens),
            metadata_token_set=set(metadata_tokens),
            duration=int(item.get("duration") or 0),
            view_count=int(item.get("view_count") or 0),
            channel_is_verified=bool(item.get("channel_is_verified", False)),
        )

    def _prepare_search_query_context(
        self, query: str, entries: list[SearchEntryContext]
    ) -> SearchQueryContext | None:
        search_text = self._search_text(query)
        if not search_text:
            return None
        query_tokens = self._signal_query_tokens(search_text)
        normalized_query = self._normalize_text(search_text)
        if not normalized_query:
            return None
        intent = self._detect_query_intent(search_text)
        ctx = SearchQueryContext(
            normalized_query=normalized_query,
            raw_query_tokens=self._tokenize_text(search_text),
            query_tokens=query_tokens,
            query_token_set=set(query_tokens),
            anchor_phrases=self._derive_artist_anchor_phrases(query_tokens, entries),
            intent=intent,
        )
        return ctx

    def _derive_artist_anchor_phrases(
        self, query_tokens: list[str], entries: list[SearchEntryContext]
    ) -> list[str]:
        if len(query_tokens) < 1:
            return []
        uploader_texts = [e.normalized_uploader for e in entries if e.normalized_uploader]
        if not uploader_texts or not any(uploader_texts):
            return []

        max_phrase_size = min(3, len(query_tokens))

        # Single-result case: anchor against that one uploader directly.
        if len(uploader_texts) == 1:
            single_text = uploader_texts[0]
            for size in range(max_phrase_size, 0, -1):
                phrases: list[str] = []
                seen: set[str] = set()
                for start in range(len(query_tokens) - size + 1):
                    phrase = " ".join(query_tokens[start: start + size])
                    if phrase in seen:
                        continue
                    seen.add(phrase)
                    if phrase in single_text:
                        phrases.append(phrase)
                if phrases:
                    return phrases[:4]
            return []

        for size in range(max_phrase_size, 0, -1):
            matches: list[tuple[int, str]] = []
            seen: set[str] = set()
            for start in range(len(query_tokens) - size + 1):
                phrase = " ".join(query_tokens[start: start + size])
                if phrase in seen:
                    continue
                seen.add(phrase)
                # FIX #4: use module-level _wb — no closure allocation per iteration.
                # Skip single tokens that are very short (Japanese particles,
                # prepositions, etc.) — they cause spurious substring matches.
                if size == 1 and len(phrase) <= 2:
                    continue
                match_count = sum(1 for text in uploader_texts if _wb(phrase, text))
                if 0 < match_count < len(uploader_texts):
                    matches.append((match_count, phrase))
            if matches:
                matches.sort(key=lambda pair: (pair[0], pair[1]))
                best_count = matches[0][0]
                return [phrase for count, phrase in matches if count == best_count][:4]
        return []

    def _score_artist_anchor_match(
        self, entry: SearchEntryContext, anchor_phrases: list[str]
    ) -> float:
        if not anchor_phrases or not entry.normalized_metadata:
            return 0.0
        # FIX #4: uses module-level _wb — no per-call closure allocation.
        uploader_matches = [p for p in anchor_phrases if _wb(p, entry.normalized_uploader)]
        if uploader_matches:
            longest = max(len(p.split()) for p in uploader_matches)
            return 1.05 + ((longest - 1) * 0.20)
        # Title-only match: artist name is in the video title (fan upload signal).
        # Use word-boundary matching and reduce the bonus.
        title_tokens = entry.normalized_metadata.split()
        title_only_matches = [
            p for p in anchor_phrases
            if _wb(p, entry.normalized_metadata)
            and not _wb(p, entry.normalized_uploader)
        ]
        if title_only_matches:
            longest = max(len(p.split()) for p in title_only_matches)
            return 0.20 + ((longest - 1) * 0.10)
        return -0.30

    def _score_search_entry(
        self,
        query: SearchQueryContext,
        entry: SearchEntryContext,
        *,
        breakdown: dict[str, float] | None = None,
    ) -> float:
        if not query.normalized_query or not entry.normalized_metadata:
            return 0.0

        intent: dict[str, bool] = query.intent
        is_anime_query = intent.get("anime", False)
        is_dash_query = intent.get("dash_format", False)

        title_overlap = self._token_overlap_ratio(query.query_tokens, entry.title_tokens)
        uploader_overlap = self._token_overlap_ratio(query.query_tokens, entry.uploader_tokens)
        metadata_overlap = self._token_overlap_ratio(query.query_tokens, entry.metadata_tokens)
        missing_title_tokens = [
            t for t in query.query_tokens if t not in entry.title_token_set
        ]
        missing_title_uploader_overlap = self._token_overlap_ratio(
            missing_title_tokens, entry.uploader_tokens
        )
        ratio = SequenceMatcher(
            None, query.normalized_query, entry.normalized_title, autojunk=False
        ).ratio()
        metadata_ratio = SequenceMatcher(
            None, query.normalized_query, entry.normalized_metadata, autojunk=False
        ).quick_ratio()
        exact_metadata_match = 1.0 if query.normalized_query in entry.normalized_metadata else 0.0
        metadata_prefix_match = 1.0 if entry.normalized_metadata.startswith(query.normalized_query) else 0.0
        all_title_tokens_match = (
            1.0 if query.query_token_set and query.query_token_set.issubset(entry.title_token_set) else 0.0
        )
        all_metadata_tokens_match = (
            1.0 if query.query_token_set and query.query_token_set.issubset(entry.metadata_token_set) else 0.0
        )

        # Artist/uploader bonuses
        artist_token_matches = len(query.query_token_set & entry.uploader_token_set)
        artist_match_bonus = 0.28 if artist_token_matches >= 2 else (0.12 if artist_token_matches == 1 else 0.0)
        strong_uploader_bonus = 0.18 if uploader_overlap >= 0.45 else 0.0

        # "Artist - Topic" channel is the cleanest official source on YouTube Music
        topic_bonus = 0.30 if "topic" in entry.uploader_token_set else 0.0

        # Uploader quality tokens (vevo, official channel, etc.)
        uploader_preference_bonus = sum(
            weight
            for token, weight in SEARCH_PREFERRED_UPLOADER_TOKENS.items()
            if token in entry.uploader_token_set
        )

        # Penalise unofficial remixes/covers unless the query asks for them
        artist_completion_bonus = 0.0
        title_only_penalty = 0.0
        if missing_title_tokens and title_overlap >= 0.45:
            artist_completion_bonus += missing_title_uploader_overlap * 0.90
            if missing_title_uploader_overlap >= 0.99:
                artist_completion_bonus += 0.45
            elif missing_title_uploader_overlap >= 0.50:
                artist_completion_bonus += 0.20
            elif title_overlap >= 0.75:
                title_only_penalty = 0.40
        elif not missing_title_tokens and uploader_overlap >= 0.20:
            artist_completion_bonus += 0.12

        # Extra reward when both title and artist are present in the result
        title_uploader_synergy = 0.0
        if title_overlap >= 0.55 and uploader_overlap >= 0.20:
            title_uploader_synergy = 0.36
        elif title_overlap >= 0.75 and missing_title_tokens and missing_title_uploader_overlap >= 0.50:
            title_uploader_synergy = 0.24

        # Extra reward for "artist - song" query format matching title structure
        dash_format_bonus = 0.0
        if is_dash_query and ratio >= 0.70:
            dash_format_bonus = 0.18

        # Quality phrase bonuses (official audio, official music video, etc.)
        preferred_bonus = sum(
            weight
            for phrase, weight in SEARCH_PREFERRED_PHRASES.items()
            if phrase in entry.normalized_metadata
        )

        # Penalty for unwanted content types — relaxed for anime queries since
        # tv size, op, ed are expected and legit in that context.
        discouraged_penalty = 0.0
        raw_query_token_set = set(query.raw_query_tokens)
        for token, weight in SEARCH_DISCOURAGED_TOKENS.items():
            if token not in raw_query_token_set and token in entry.metadata_token_set:
                # Relax live penalty for anime (live performances are sometimes
                # the only official upload), and skip tv/op/ed penalties entirely.
                if is_anime_query and token in {"live"}:
                    discouraged_penalty += weight * 0.3
                else:
                    discouraged_penalty += weight
        for phrase, weight in SEARCH_DISCOURAGED_PHRASES.items():
            if phrase not in query.normalized_query and phrase in entry.normalized_metadata:
                # tv size is expected for anime op/ed queries
                if is_anime_query and phrase == "tv size":
                    continue
                discouraged_penalty += weight

        # Japanese bracket cover notation: 【ギター】, 【弾いてみた】 etc.
        # These are instrument/cover uploads — penalise unless the user asked for it.
        raw_title = str(entry.item.get("title") or "")
        if _JP_COVER_BRACKET_RE.search(raw_title):
            query_asks_for_instrument = any(
                tok in raw_query_token_set
                for tok in ("guitar", "piano", "violin", "bass", "acoustic",
                            "cover", "fingerstyle", "ukulele")
            )
            if not query_asks_for_instrument:
                discouraged_penalty += 0.75

        # Duration heuristic: 2–10 min typical; long videos get a moderate penalty.
        duration_bonus = 0.0
        if 90 <= entry.duration <= 600:
            duration_bonus = 0.10
        elif 60 <= entry.duration <= 660:
            duration_bonus = 0.05
        elif entry.duration > 900:
            duration_bonus = -0.12

        anchor_score = self._score_artist_anchor_match(entry, query.anchor_phrases)

        # View count signal (log scale) — official releases vastly outperform
        # fan uploads in view count for the same song.
        # Scale: log10(1K)=3 → 0.0, log10(1B)=9 → 0.35 (capped)
        vc = entry.view_count
        # Topic channels get view_bonus = 0 when the upload is brand-new (< 1000 views).
        # Skip the floor for them since topic_bonus already rewards the channel.
        _is_topic = "topic" in entry.uploader_token_set
        if vc >= 1000:
            view_bonus = min(0.35, (math.log10(vc) - 3.0) / 6.0 * 0.35)
        elif _is_topic:
            view_bonus = 0.05  # small baseline for fresh Topic uploads
        else:
            view_bonus = 0.0

        # Verified channel bonus — blue checkmark is a strong legitimacy signal
        verified_bonus = 0.15 if entry.channel_is_verified else 0.0

        # Authentic Japanese original release bonus.
        # Predominantly-Japanese titles are crushed by title_overlap=0 against
        # English queries even though they are exactly what the user wants.
        # Boost them when they come from a preferred/official channel.
        jp_original_bonus = 0.0
        title_core = _BRACKET_STRIP_RE.sub("", raw_title).strip()
        # Gate: skip if content already heavily penalized (AMV, lyric vid, cover)
        if discouraged_penalty < 0.50 and _CJK_RE.search(title_core):
            latin_chars = len(re.findall(r"[a-zA-Z]", title_core))
            total_chars = len(title_core.replace(" ", ""))
            hangul_count = len(_HANGUL_RE.findall(title_core))
            cjk_count = len(re.findall(r'[\u3040-\u30ff\u4e00-\u9fff]', title_core))
            latin_ratio = latin_chars / total_chars if total_chars else 1.0
            # Exclude Korean fan videos about JP music (Hangul-dominant mixed titles)
            is_jp_dominant = latin_ratio < 0.35 and (hangul_count == 0 or cjk_count > hangul_count * 1.5)
            if is_jp_dominant:
                jp_original_bonus = 0.55

        final = (
            (ratio * 0.32)
            + (metadata_ratio * 0.20)
            + (title_overlap * 0.44)
            + (uploader_overlap * 0.50)
            + (metadata_overlap * 0.36)
            + (exact_metadata_match * 0.18)
            + (metadata_prefix_match * 0.10)
            + (all_title_tokens_match * 0.16)
            + (all_metadata_tokens_match * 0.24)
            + artist_match_bonus
            + strong_uploader_bonus
            + topic_bonus
            + uploader_preference_bonus
            + artist_completion_bonus
            + title_uploader_synergy
            + dash_format_bonus
            + preferred_bonus
            + duration_bonus
            + anchor_score
            + jp_original_bonus
            + view_bonus
            + verified_bonus
            - title_only_penalty
            - discouraged_penalty
        )

        if breakdown is not None:
            breakdown.update({
                "title_overlap": title_overlap,
                "uploader_overlap": uploader_overlap,
                "ratio": ratio,
                "metadata_ratio": metadata_ratio,
                "topic_bonus": topic_bonus,
                "uploader_pref_bonus": uploader_preference_bonus,
                "anchor_score": anchor_score,
                "artist_match_bonus": artist_match_bonus,
                "strong_uploader_bonus": strong_uploader_bonus,
                "artist_completion_bonus": artist_completion_bonus,
                "title_uploader_synergy": title_uploader_synergy,
                "preferred_bonus": preferred_bonus,
                "discouraged_penalty": discouraged_penalty,
                "duration_bonus": duration_bonus,
                "jp_original_bonus": jp_original_bonus,
                "view_bonus":         view_bonus,
                "verified_bonus":     verified_bonus,
                "final": final,
            })

        return final

    def _rank_prepared_search_entries(
        self, query: str, entries: list[dict[str, Any]]
    ) -> list[tuple[float, int, dict[str, Any], SearchEntryContext]]:
        prepared = [
            (index, entry, self._prepare_search_entry(entry))
            for index, entry in enumerate(entries)
            if entry
        ]
        if not prepared:
            return []
        query_context = self._prepare_search_query_context(
            query, [p for _, _, p in prepared]
        )
        if query_context is None:
            # No scoring possible (e.g. pure-CJK query — _normalize_text returns "").
            # Return entries in original order with a neutral score so callers
            # always receive 4-tuples: (score, orig_index, item, entry_ctx).
            return [(0.0, oi, item, ectx) for (oi, item, ectx) in prepared]

        # Score every entry; anchor is already included inside _score_search_entry
        # so we do NOT add it again here (fix for double-counted anchor bug).
        scored: list[tuple[float, int, dict[str, Any], SearchEntryContext, dict[str, float]]] = []
        for orig_index, item, entry_ctx in prepared:
            bd: dict[str, float] = {}
            score = self._score_search_entry(query_context, entry_ctx, breakdown=bd)
            scored.append((score, orig_index, item, entry_ctx, bd))

        scored.sort(key=lambda t: (t[0], -t[1]), reverse=True)

        # Store debug record for !why, keyed by the current guild.
        guild_id = _CURRENT_GUILD_ID.get()
        if guild_id is not None:
            records: list[ScoreBreakdown] = []
            for rank, (sc, _oi, item, ectx, bd) in enumerate(scored[:8], start=1):
                records.append(ScoreBreakdown(
                    rank=rank,
                    title=str(item.get("title") or ""),
                    uploader=str(item.get("uploader") or ectx.normalized_uploader),
                    webpage_url=self._playlist_entry_url(item) or "",
                    duration=ectx.duration,
                    final_score=round(sc, 4),
                    title_overlap=round(bd.get("title_overlap", 0.0), 3),
                    uploader_overlap=round(bd.get("uploader_overlap", 0.0), 3),
                    ratio=round(bd.get("ratio", 0.0), 3),
                    topic_bonus=round(bd.get("topic_bonus", 0.0), 3),
                    uploader_pref_bonus=round(bd.get("uploader_pref_bonus", 0.0), 3),
                    anchor_score=round(bd.get("anchor_score", 0.0), 3),
                    artist_match_bonus=round(bd.get("artist_match_bonus", 0.0), 3),
                    artist_completion_bonus=round(bd.get("artist_completion_bonus", 0.0), 3),
                    title_uploader_synergy=round(bd.get("title_uploader_synergy", 0.0), 3),
                    preferred_bonus=round(bd.get("preferred_bonus", 0.0), 3),
                    discouraged_penalty=round(bd.get("discouraged_penalty", 0.0), 3),
                    duration_bonus=round(bd.get("duration_bonus", 0.0), 3),
                    jp_original_bonus=round(bd.get("jp_original_bonus", 0.0), 3),
                    view_bonus=round(bd.get("view_bonus", 0.0), 3),
                    verified_bonus=round(bd.get("verified_bonus", 0.0), 3),
                ))
            if records:
                records[0].selected = True
            # FIX #7: LRU eviction — move to end on write, pop oldest from front.
            self._last_search[guild_id] = SearchDebugRecord(
                query_text=self._search_text(query),
                guild_id=guild_id,
                timestamp=time.monotonic(),
                candidates=records,
            )
            self._last_search.move_to_end(guild_id)
            while len(self._last_search) > self._last_search_max:
                self._last_search.popitem(last=False)
            self.logger.debug(
                "Search scores | guild=%s query=%r | %s",
                guild_id,
                self._search_text(query),
                " | ".join(f"[{r.rank}] {r.title!r} score={r.final_score}" for r in records),
            )

        # Return scores alongside entries so callers can apply API bonuses.
        return [(sc, oi, item, ectx) for (sc, oi, item, ectx, _bd) in scored]


    def _rank_search_entries(
        self, query: str, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [item for (_, _, item, _) in self._rank_prepared_search_entries(query, entries)]

    # ------------------------------------------------------------------
    # Now-playing controller helpers
    # ------------------------------------------------------------------

    def _controller(
        self, guild_id: int, *, message_id: int | None = None
    ) -> NowPlayingController | None:
        controller = self.now_playing_messages.get(guild_id)
        if controller is None:
            return None
        if controller.expires_at <= time.monotonic():
            self.now_playing_messages.pop(guild_id, None)
            return None
        if message_id is not None and controller.message_id != message_id:
            return None
        return controller

    def _remember_channel(self, player: GuildPlayer, channel: discord.abc.Messageable) -> None:
        channel_id = getattr(channel, "id", None)
        if isinstance(channel_id, int):
            player.set_announce_channel(channel_id)

    def _build_queue_view(
        self,
        guild_id: int,
        player: GuildPlayer,
        *,
        author_id: int,
        page: int = 0,
    ) -> QueueView:
        view = QueueView(
            self,
            guild_id,
            player,
            author_id=author_id,
            page_index=page,
        )
        return view

    def _queue_lines(self, player: GuildPlayer, *, limit: int) -> list[str]:
        lines: list[str] = []
        if player.current:
            lines.append(
                f"Now: `{discord.utils.escape_markdown(player.current.title)}` "
                f"[{player.current.duration_label}]"
            )
        for index, track in enumerate(itertools.islice(player.queue, limit), start=1):
            duration = track.duration_label if track.duration else "pending"
            lines.append(
                f"{index}. `{discord.utils.escape_markdown(track.title)}` [{duration}]"
            )
        if len(player.queue) > limit:
            lines.append(f"...and {len(player.queue) - limit} more.")
        return lines

    def _render_now_playing_embed(
        self,
        guild: discord.Guild,
        player: GuildPlayer | None,
        controller: NowPlayingController,
    ) -> discord.Embed:
        embed = discord.Embed(colour=EMBED_COLOUR)
        footer_parts = ["⏮ prev", "⏭ skip", "⏯ pause", "↻ loop", "≡ queue"]
        footer = "  ·  ".join(footer_parts)
        if controller.status_text:
            footer = f"{footer}  ·  {controller.status_text}"

        if not player or not player.current:
            embed.title = "Now Playing"
            embed.description = "Nothing is playing right now."
            embed.set_footer(text=footer)
            return embed

        track = player.current
        is_paused = bool(player.voice_client and player.voice_client.is_paused())
        state_label = "Paused" if is_paused else "Playing"
        loop_label = LOOP_LABELS.get(player.loop_mode, "Off")
        loop_icon = LOOP_ICONS.get(player.loop_mode, "→")
        requester = guild.get_member(track.requester_id)
        requester_label = requester.mention if requester else f"<@{track.requester_id}>"

        embed.title = "Now Playing" if not is_paused else "Paused"
        embed.add_field(
            name="Track",
            value=f"[{discord.utils.escape_markdown(track.title)}]({track.webpage_url})",
            inline=False,
        )

        # Progress bar (uses accurate elapsed_seconds that excludes pause time).
        progress = self._format_progress_bar(player.elapsed_seconds, track.duration)
        embed.add_field(
            name=f"Progress — {state_label}",
            value=progress,
            inline=False,
        )

        embed.add_field(
            name="Uploader",
            value=discord.utils.escape_markdown(track.uploader or "Unknown"),
            inline=True,
        )
        embed.add_field(name="Requested by", value=requester_label, inline=True)
        embed.add_field(
            name=f"Loop — {loop_icon}",
            value=f"`{loop_label}`",
            inline=True,
        )

        preview_lines = self._queue_lines(player, limit=NOW_PLAYING_PREVIEW_LIMIT)
        # _queue_lines includes the current track as "Now:" — strip it.
        if preview_lines and player.current:
            preview_lines = preview_lines[1:]
        embed.add_field(
            name="Up Next",
            value="\n".join(preview_lines) if preview_lines else "Nothing queued.",
            inline=False,
        )

        if track.thumbnail_url:
            embed.set_thumbnail(url=track.thumbnail_url)

        embed.set_footer(text=footer)
        return embed

    async def _fetch_announce_channel(
        self, guild: discord.Guild, player: GuildPlayer
    ) -> discord.abc.Messageable | None:
        if player.announce_channel_id is None:
            return None
        channel = self.bot.get_channel(player.announce_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(player.announce_channel_id)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                return None
        return channel if isinstance(channel, discord.abc.Messageable) else None


    async def _update_bot_presence(self) -> None:
        """Set bot activity based on how many guilds are actively playing.

        Discord allows only one global status per bot account — last writer wins
        when multiple guilds are active.  This shows a summary instead of a
        race-condition winner.
        """
        active = [
            p for p in self.players.values()
            if p.current and p.voice_client and p.voice_client.is_playing()
        ]
        with contextlib.suppress(Exception):
            if len(active) == 0:
                prefix = self.bot.settings.default_prefix
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.listening,
                        name=f"{prefix}play",
                    )
                )
            elif len(active) == 1:
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.listening,
                        name=active[0].current.title[:128],
                    )
                )
            else:
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.listening,
                        name=f"music in {len(active)} servers",
                    )
                )

    async def _send_now_playing_panel(
        self,
        guild: discord.Guild,
        player: GuildPlayer,
        *,
        channel: discord.abc.Messageable | None = None,
        replace_existing: bool = False,
        status_text: str = "",
    ) -> discord.Message | None:
        target_channel = channel or await self._fetch_announce_channel(guild, player)
        if target_channel is None:
            return None

        # Delete the old panel if requested.
        if replace_existing:
            existing = self._controller(guild.id)
            if existing and existing.message:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await existing.message.delete()

        channel_id = getattr(target_channel, "id", None)
        if not isinstance(channel_id, int):
            return None

        view = NowPlayingView(self, guild.id)
        controller = NowPlayingController(
            channel_id=channel_id,
            message_id=0,
            expires_at=time.monotonic() + NOW_PLAYING_TIMEOUT_SECONDS,
            status_text=status_text,
        )
        message = await target_channel.send(
            embed=self._render_now_playing_embed(guild, player, controller),
            view=view,
        )
        controller.message_id = message.id
        controller.message = message  # cache so refreshes never need fetch_message
        self.now_playing_messages[guild.id] = controller
        return message

    async def _refresh_now_playing_message(self, guild_id: int) -> None:
        controller = self._controller(guild_id)
        guild = self.bot.get_guild(guild_id)
        if controller is None or guild is None:
            return
        # Use the cached message object — zero extra HTTP calls.
        if controller.message is None:
            return
        player = self.players.get(guild_id)
        embed = self._render_now_playing_embed(guild, player, controller)
        with contextlib.suppress(discord.HTTPException):
            await controller.message.edit(embed=embed)

    def _schedule_np_refresh(
        self, guild_id: int, *, delay: float = NP_REFRESH_DEBOUNCE_SECONDS
    ) -> None:
        """Debounced now-playing refresh — coalesces rapid queue updates."""
        existing = self.np_refresh_tasks.get(guild_id)
        if existing and not existing.done():
            existing.cancel()
        self.np_refresh_tasks[guild_id] = asyncio.create_task(
            self._delayed_np_refresh(guild_id, delay)
        )

    async def _delayed_np_refresh(self, guild_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._refresh_now_playing_message(guild_id)
        finally:
            if self.np_refresh_tasks.get(guild_id) is asyncio.current_task():
                self.np_refresh_tasks.pop(guild_id, None)

    def cog_unload(self) -> None:
        self.now_playing_messages.clear()
        for player in self.players.values():
            self._bg_task(player.destroy(), name="cog-unload-destroy")
        for task in self.prefetch_tasks.values():
            task.cancel()
        for task in self.snapshot_tasks.values():
            task.cancel()
        for task in self.np_refresh_tasks.values():
            task.cancel()
        if self._http_session and not self._http_session.closed:
            asyncio.ensure_future(self._http_session.close())
        # lru caches are now module-level; no self-reference to clear.

    # ------------------------------------------------------------------
    # Player management
    # ------------------------------------------------------------------

    def _bg_task(self, coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
        """Create a background task that logs exceptions instead of silently swallowing them."""
        task = asyncio.create_task(coro, name=name)
        def _on_done(t: asyncio.Task[Any]) -> None:
            if not t.cancelled() and t.exception() is not None:
                self.logger.exception(
                    "Background task %r raised an exception", t.get_name(),
                    exc_info=t.exception(),
                )
        task.add_done_callback(_on_done)
        return task

    async def _get_player(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if not player:
            player = GuildPlayer(
                self.bot,
                guild,
                self._resolve_track,
                self._build_audio_source,
            )
            self.players[guild.id] = player
            await self._restore_snapshot(player)
        return player

    async def _restore_snapshot(self, player: GuildPlayer) -> None:
        rows = await self.bot.database.load_queue_snapshot(player.guild.id)
        if player.queue:
            return
        restored = [
            Track(
                title=row["title"],
                webpage_url=row["webpage_url"],
                stream_url="",
                uploader="Restored queue",
                duration=0,
                requester_id=int(row["requester_id"]),
                query=row["query"],
            )
            for row in rows
        ]
        # Cap restored queue to max_queue_size to avoid unbounded memory on Oracle Micro.
        cap = self.bot.settings.max_queue_size
        player.queue = deque(restored[:cap])
        # Pre-resolve the first 2 tracks in the background so there's no gap
        # after a bot restart.
        if restored:
            self._bg_task(
                self._warmup_restore(list(restored[:2]), guild_id=player.guild.id),
                name="warmup-restore",
            )

    async def _warmup_restore(self, tracks: list[Track], *, guild_id: int) -> None:
        """Pre-resolve the first N tracks after a snapshot restore so playback
        starts immediately without a cold-resolve delay.
        Acquires the guild extract semaphore so warmup on multiple guilds
        simultaneously doesn't spike yt-dlp usage on Oracle Micro.
        """
        sem = self._guild_extract_semaphores.setdefault(guild_id, asyncio.Semaphore(1))
        for track in tracks:
            async with sem:
                with contextlib.suppress(Exception):
                    await self._resolve_track(track)

    async def _delayed_snapshot(self, guild_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._write_snapshot(guild_id)
        finally:
            if self.snapshot_tasks.get(guild_id) is asyncio.current_task():
                self.snapshot_tasks.pop(guild_id, None)

    async def _cancel_snapshot_task(self, guild_id: int) -> None:
        task = self.snapshot_tasks.pop(guild_id, None)
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _snapshot_entries(self, guild_id: int) -> list[dict[str, Any]]:
        """Serialize the current track + queue into the format expected by the database."""
        player = self.players.get(guild_id)
        if player is None:
            return []
        tracks: list[Track] = []
        if player.current:
            tracks.append(player.current)
        tracks.extend(player.queue)
        return [
            {
                "query":       t.query or t.webpage_url or t.title,
                "title":       t.title,
                "webpage_url": t.webpage_url or "",
                "requester_id": str(t.requester_id),
            }
            for t in tracks
        ]

    async def _write_snapshot(
        self,
        guild_id: int,
        *,
        entries: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self.bot.database.is_open or getattr(self.bot, "_shutting_down", False):
            return
        snapshot = self._snapshot_entries(guild_id) if entries is None else entries
        await self.bot.database.save_queue_snapshot(guild_id, snapshot)

    async def _flush_snapshot(
        self,
        guild_id: int,
        *,
        entries: list[dict[str, Any]] | None = None,
    ) -> None:
        """Cancel any pending debounce and write immediately."""
        await self._cancel_snapshot_task(guild_id)
        await self._write_snapshot(guild_id, entries=entries)

    def _persist_snapshot(self, guild_id: int) -> None:
        """Schedule a debounced snapshot write."""
        existing = self.snapshot_tasks.get(guild_id)
        if existing and not existing.done():
            existing.cancel()
        self.snapshot_tasks[guild_id] = self._bg_task(
            self._delayed_snapshot(guild_id, SNAPSHOT_DEBOUNCE_SECONDS),
            name=f"snapshot-{guild_id}",
        )

    # ------------------------------------------------------------------
    # Track extraction & resolution
    # ------------------------------------------------------------------

    def _is_playlist_query(self, query: str) -> bool:
        if not query.startswith(("http://", "https://")):
            return False
        return "list" in parse_qs(urlparse(query).query)

    def _playlist_entry_url(self, item: dict[str, Any]) -> str | None:
        for candidate in (item.get("webpage_url"), item.get("original_url"), item.get("url")):
            if not candidate:
                continue
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
            if item.get("ie_key") == "Youtube" or item.get("extractor_key") == "Youtube":
                return f"https://www.youtube.com/watch?v={candidate}"
        video_id = item.get("id")
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        return None

    def _item_thumbnail_url(self, item: dict[str, Any]) -> str:
        thumbnail = item.get("thumbnail")
        if isinstance(thumbnail, str) and thumbnail.startswith(("http://", "https://")):
            return thumbnail
        thumbnails = item.get("thumbnails")
        if isinstance(thumbnails, list):
            for candidate in reversed(thumbnails):
                if not isinstance(candidate, dict):
                    continue
                url = candidate.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url
        return ""

    def _search_result_track(self, item: dict[str, Any], requester_id: int) -> Track | None:
        webpage_url = self._playlist_entry_url(item)
        if not webpage_url:
            return None
        return Track(
            title=item.get("title", "Unknown title"),
            webpage_url=webpage_url,
            stream_url="",
            uploader=item.get("channel") or item.get("uploader") or "Search result",
            duration=int(item.get("duration") or 0),
            requester_id=requester_id,
            query=webpage_url,
            thumbnail_url=self._item_thumbnail_url(item),
        )

    async def _extract_info(self, options: dict[str, Any], query: str) -> dict[str, Any]:
        """Extract info using a fresh YoutubeDL instance per call (thread-safe).

        Reads the current guild_id from _CURRENT_GUILD_ID contextvar so guilds
        never block each other, while the global semaphore caps total concurrency.
        """
        guild_id = _CURRENT_GUILD_ID.get()
        guild_sem = (
            self._guild_extract_semaphores.setdefault(guild_id, asyncio.Semaphore(1))
            if guild_id is not None
            else None
        )

        async def _do_extract() -> dict[str, Any]:
            async with self.extract_semaphore:
                try:
                    def _run() -> dict[str, Any]:
                        with YoutubeDL(options) as ydl:
                            return ydl.extract_info(query, download=False)

                    result = await asyncio.wait_for(
                        asyncio.to_thread(_run),
                        timeout=self.bot.settings.ytdlp_extract_timeout_seconds,
                    )
                    if result is None:
                        raise commands.BadArgument(
                            "No information could be extracted for the provided source."
                        )
                    return result
                except asyncio.TimeoutError as exc:
                    self.logger.warning("yt-dlp timed out for query %s", query)
                    raise commands.BadArgument(
                        f"Source lookup timed out after "
                        f"{self.bot.settings.ytdlp_extract_timeout_seconds} seconds."
                    ) from exc

        if guild_sem is not None:
            async with guild_sem:
                return await _do_extract()
        return await _do_extract()

    async def _extract_playlist_tracks(
        self, query: str, requester_id: int
    ) -> tuple[list[Track], int]:
        try:
            info = await self._extract_info(
                self._build_ytdl_options(flat_playlist=True), query
            )
        except commands.BadArgument:
            raise
        except DownloadError as exc:
            self.logger.warning("yt-dlp playlist scan failed for %s: %s", query, exc)
            raise commands.BadArgument(f"Failed to fetch media: {exc}") from exc

        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return [], 0

        tracks: list[Track] = []
        skipped = 0
        for item in entries:
            if len(tracks) >= self.bot.settings.max_playlist_size:
                break
            if not item:
                skipped += 1
                continue
            webpage_url = self._playlist_entry_url(item)
            if not webpage_url:
                skipped += 1
                continue
            tracks.append(
                Track(
                    title=item.get("title", "Unknown title"),
                    webpage_url=webpage_url,
                    stream_url="",
                    uploader=item.get("channel") or item.get("uploader") or "Playlist item",
                    duration=int(item.get("duration") or 0),
                    requester_id=requester_id,
                    query=webpage_url,
                )
            )
        return tracks, skipped

    async def _extract_single_track(
        self, item: dict[str, Any], query: str, requester_id: int
    ) -> Track | None:
        if "url" not in item and item.get("webpage_url"):
            try:
                item = await self._extract_info(
                    self._build_ytdl_options(), item["webpage_url"]
                )
            except DownloadError as exc:
                self.logger.warning("Skipping unplayable item %s: %s", item.get("webpage_url"), exc)
                return None

        stream_url = item.get("url")
        webpage_url = item.get("webpage_url") or query
        if not stream_url:
            return None

        return Track(
            title=item.get("title", "Unknown title"),
            webpage_url=webpage_url,
            stream_url=stream_url,
            uploader=item.get("uploader", "Unknown uploader"),
            duration=int(item.get("duration") or 0),
            requester_id=requester_id,
            query=webpage_url,
            thumbnail_url=item.get("thumbnail") or "",
            resolved_at=time.monotonic(),
            tags=list(item.get("tags") or []) + list(item.get("categories") or []),
        )

    async def _extract_full_tracks(
        self, query: str, requester_id: int
    ) -> tuple[list[Track], int]:
        try:
            info = await self._extract_info(self._build_ytdl_options(), query)
        except commands.BadArgument:
            raise
        except DownloadError as exc:
            self.logger.warning("yt-dlp failed for query %s: %s", query, exc)
            raise commands.BadArgument(f"Failed to fetch media: {exc}") from exc

        entries = info.get("entries") if isinstance(info, dict) else None
        info_items: list[dict[str, Any]]
        if entries:
            info_items = [entry for entry in entries if entry][: self.bot.settings.max_playlist_size]
        elif isinstance(info, dict):
            info_items = [info]
        else:
            return [], 0

        tracks: list[Track] = []
        skipped = 0
        for item in info_items:
            track = await self._extract_single_track(item, query, requester_id)
            if track is None:
                skipped += 1
                continue
            tracks.append(track)
        return tracks, skipped

    async def _extract_search_candidates(
        self,
        query: str,
        requester_id: int,
        *,
        limit: int = SEARCH_SELECTION_LIMIT,
    ) -> tuple[list[Track], int]:
        try:
            info = await self._extract_info(
                self._build_ytdl_options(flat_search=True), query
            )
        except commands.BadArgument:
            raise
        except DownloadError as exc:
            self.logger.warning("yt-dlp search failed for %s: %s", query, exc)
            raise commands.BadArgument(f"Failed to fetch media: {exc}") from exc

        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return [], 0

        tracks: list[Track] = []
        skipped = 0
        ranked_items = self._rank_search_entries(query, entries)

        for item in ranked_items:
            track = self._search_result_track(item, requester_id)
            if track is None:
                skipped += 1
                continue
            tracks.append(track)
            if len(tracks) >= limit:
                break
        return tracks, skipped

    async def _extract_search_tracks(
        self, query: str, requester_id: int
    ) -> tuple[list[Track], int]:
        return await self._extract_search_candidates(query, requester_id, limit=1)

    async def _extract_tracks(
        self, query: str, requester_id: int, *, guild_id: int | None = None
    ) -> tuple[list[Track], int]:
        token = _CURRENT_GUILD_ID.set(guild_id)
        try:
            if query.startswith("ytsearch"):
                return await self._extract_search_tracks(query, requester_id)
            if self._is_playlist_query(query):
                return await self._extract_playlist_tracks(query, requester_id)
            return await self._extract_full_tracks(query, requester_id)
        finally:
            _CURRENT_GUILD_ID.reset(token)

    def _cache_key(self, track: Track) -> str:
        return track.webpage_url or track.query

    def _get_cached_track_data(self, key: str) -> ResolvedTrackData | None:
        cached = self.resolve_cache.get(key)
        if cached is None:
            return None
        expires_at, data = cached
        if expires_at <= time.monotonic():
            self.resolve_cache.pop(key, None)
            return None
        self.resolve_cache.move_to_end(key)
        return data

    def _store_cached_track_data(self, data: ResolvedTrackData) -> None:
        expires_at = time.monotonic() + self.bot.settings.ytdlp_resolve_cache_ttl_seconds
        self.resolve_cache[data.webpage_url] = (expires_at, data)
        self.resolve_cache.move_to_end(data.webpage_url)
        while len(self.resolve_cache) > self.bot.settings.ytdlp_resolve_cache_size:
            self.resolve_cache.popitem(last=False)
        # FIX #7: LRU eviction via popitem — no full-clear, no sorted() O(n log n).
        while len(self._last_search) > self._last_search_max:
            self._last_search.popitem(last=False)

    def _apply_resolved_track_data(self, track: Track, data: ResolvedTrackData) -> Track:
        track.title = data.title
        track.webpage_url = data.webpage_url
        track.stream_url = data.stream_url
        track.uploader = data.uploader
        track.duration = data.duration
        track.query = data.query
        track.resolved_at = data.resolved_at
        if data.thumbnail_url:
            track.thumbnail_url = data.thumbnail_url
        if data.tags:
            track.tags = data.tags
        return track

    async def _resolve_track_data(self, track: Track) -> ResolvedTrackData | None:
        cache_key = self._cache_key(track)
        cached = self._get_cached_track_data(cache_key)
        if cached is not None:
            return cached

        pending = self.resolve_tasks.get(cache_key)
        if pending is None:
            async def runner() -> ResolvedTrackData | None:
                tracks, _ = await self._extract_full_tracks(
                    track.webpage_url or track.query, track.requester_id
                )
                if not tracks:
                    return None
                resolved = tracks[0]
                data = ResolvedTrackData(
                    title=resolved.title,
                    webpage_url=resolved.webpage_url,
                    stream_url=resolved.stream_url,
                    uploader=resolved.uploader,
                    duration=resolved.duration,
                    query=resolved.query,
                    resolved_at=resolved.resolved_at or time.monotonic(),
                    thumbnail_url=resolved.thumbnail_url,
                    tags=resolved.tags,
                )
                self._store_cached_track_data(data)
                return data

            pending = asyncio.create_task(runner())
            self.resolve_tasks[cache_key] = pending

        try:
            return await pending
        finally:
            if self.resolve_tasks.get(cache_key) is pending and pending.done():
                self.resolve_tasks.pop(cache_key, None)

    async def _resolve_track(self, track: Track) -> Track | None:
        if track.stream_url:
            return track
        data = await self._resolve_track_data(track)
        if data is None:
            return None
        return self._apply_resolved_track_data(track, data)

    async def _materialize_track(self, query: str, requester_id: int) -> Track | None:
        tracks, _ = await self._extract_tracks(query, requester_id=requester_id)
        if not tracks:
            return None
        return await self._resolve_track(tracks[0])

    def _schedule_prefetch(self, guild_id: int) -> None:
        if self.bot.settings.ytdlp_prefetch_count < 1:
            return
        task = self.prefetch_tasks.get(guild_id)
        if task and not task.done():
            return
        self.prefetch_tasks[guild_id] = asyncio.create_task(
            self._prefetch_tracks(guild_id)
        )

    async def _prefetch_tracks(self, guild_id: int) -> None:
        try:
            player = self.players.get(guild_id)
            if player is None:
                return
            candidates: list[Track] = []
            if player.current and not player.current.stream_url:
                candidates.append(player.current)
            for track in player.queue:
                if len(candidates) >= self.bot.settings.ytdlp_prefetch_count:
                    break
                if not track.stream_url:
                    candidates.append(track)
            token = _CURRENT_GUILD_ID.set(guild_id)
            try:
                for track in candidates:
                    with contextlib.suppress(commands.BadArgument, Exception):
                        await self._resolve_track(track)
            finally:
                _CURRENT_GUILD_ID.reset(token)
            self._persist_snapshot(guild_id)
        finally:
            self.prefetch_tasks.pop(guild_id, None)

    async def _preload_next_track(
        self, guild_id: int, *, force_refresh: bool = False
    ) -> None:
        player = self.players.get(guild_id)
        if player is None or not player.queue:
            return

        next_track = player.queue[0]
        if next_track.stream_url and not force_refresh:
            return
        if force_refresh and next_track.stream_url:
            age = time.monotonic() - next_track.resolved_at
            if age < STREAM_URL_REFRESH_AGE_SECONDS:
                self.logger.debug(
                    "Near-end refresh skipped for guild %s — URL age %.0fs < threshold %.0fs",
                    guild_id,
                    age,
                    STREAM_URL_REFRESH_AGE_SECONDS,
                )
                return
            next_track.stream_url = ""
            next_track.resolved_at = 0.0

        try:
            token = _CURRENT_GUILD_ID.set(guild_id)
            try:
                resolved = await self._resolve_track(next_track)
            finally:
                _CURRENT_GUILD_ID.reset(token)
        except commands.BadArgument as exc:
            self.logger.warning("Near-end preload failed for guild %s: %s", guild_id, exc)
            return

        if resolved is not None:
            self._persist_snapshot(guild_id)

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    async def _ensure_author_voice(
        self, context: commands.Context[Any]
    ) -> discord.VoiceChannel | discord.StageChannel:
        voice_state = context.author.voice
        if not voice_state or not voice_state.channel:
            raise commands.BadArgument("Join a voice channel first.")
        return voice_state.channel

    def _voice_humans(self, channel: discord.abc.GuildChannel) -> list[discord.Member]:
        members = getattr(channel, "members", [])
        return [member for member in members if not member.bot]

    def _is_bot_owner(self, user: discord.User | discord.Member) -> bool:
        """Return True if user is a configured bot owner — bypasses all guild checks."""
        return user.id in self.bot.settings.bot_owners or (
            self.bot.owner_id is not None and user.id == self.bot.owner_id
        )

    async def _is_dj(self, member: discord.Member) -> bool:
        if self._is_bot_owner(member):
            return True
        if member.guild_permissions.manage_guild:
            return True
        role_id = await self.bot.database.get_dj_role_id(member.guild.id)
        return bool(role_id and any(role.id == role_id for role in member.roles))

    async def _require_dj(self, context: commands.Context[Any]) -> None:
        if not await self._is_dj(context.author):
            raise commands.CheckFailure("DJ role or Manage Server permission required.")

    async def _join_for_context(self, context: commands.Context[Any]) -> GuildPlayer:
        channel = await self._ensure_author_voice(context)
        player = await self._get_player(context.guild)
        self._remember_channel(player, context.channel)
        await player.connect(channel)
        return player

    def _required_skip_votes(self, player: GuildPlayer) -> int:
        if not player.voice_client or not player.voice_client.channel:
            return 1
        human_count = len(self._voice_humans(player.voice_client.channel))
        return max(1, math.ceil(human_count / 2))

    def _is_in_player_voice(self, player: GuildPlayer, member: discord.Member) -> bool:
        return bool(
            player.voice_client
            and player.voice_client.channel
            and member in player.voice_client.channel.members
        )

    # ------------------------------------------------------------------
    # Shared action helpers (used by commands AND NowPlayingView)
    # ------------------------------------------------------------------

    async def _skip_for_member(
        self, player: GuildPlayer, member: discord.Member
    ) -> str:
        if not player.current or not player.voice_client or not player.voice_client.channel:
            return "Nothing is playing."
        if not self._is_in_player_voice(player, member):
            return "Join my voice channel to vote skip."
        requester_match = player.current.requester_id == member.id
        if requester_match or await self._is_dj(member):
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

    async def _previous_for_member(
        self, player: GuildPlayer, member: discord.Member
    ) -> str:
        if not self._is_in_player_voice(player, member):
            return "Join my voice channel first."
        if not await self._is_dj(member) and (
            not player.current or player.current.requester_id != member.id
        ):
            return "Only the current requester or a DJ can go to the previous track."
        if not player.play_previous():
            return "There is no previous track to return to."
        return "Returned to the previous track."

    async def _toggle_pause_for_member(
        self, player: GuildPlayer, member: discord.Member
    ) -> str:
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

    async def _toggle_loop_for_member(
        self, player: GuildPlayer, member: discord.Member
    ) -> str:
        if not self._is_in_player_voice(player, member):
            return "Join my voice channel first."
        if not await self._is_dj(member):
            return "DJ role or Manage Server permission required."
        if not player.current and not player.queue:
            return "Nothing is loaded."
        # FIX #17: capture previous state before cycling.
        prev_label = LOOP_LABELS.get(player.loop_mode, "Off")
        player.loop_mode = LOOP_CYCLE.get(player.loop_mode, "off")
        self._persist_snapshot(member.guild.id)
        label = LOOP_LABELS.get(player.loop_mode, "Off")
        icon = LOOP_ICONS.get(player.loop_mode, "→")
        return f"Loop changed: **{prev_label}** → {icon} **{label}**"

    async def _prompt_for_search_selection(
        self,
        context: commands.Context[Any],
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
            guild_icon_url=context.guild.icon.url if context.guild and context.guild.icon else None,
        )
        prompt = await context.send(embed=view.build_embed(), view=view)
        view.message = prompt
        return await view.wait_for_selection()

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_musicbot_np_auto_refresh(self, guild: discord.Guild) -> None:
        await self._refresh_now_playing_message(guild.id)

    @commands.Cog.listener()
    async def on_musicbot_track_skipped_error(
        self, guild: discord.Guild, track: Track, reason: str
    ) -> None:
        if not self.bot.settings.error_announce:
            return
        player = self.players.get(guild.id)
        channel = await self._fetch_announce_channel(guild, player) if player else None
        if channel is None:
            channel = guild.system_channel
        if channel:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(
                    f"Skipped **{discord.utils.escape_markdown(track.title)}** — {reason}"
                )

    @commands.Cog.listener()
    async def on_musicbot_playback_error(
        self, guild: discord.Guild, error: Exception
    ) -> None:
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
        # Update bot activity (multi-server aware).
        await self._update_bot_presence()
        await self._send_now_playing_panel(
            guild, player, replace_existing=True, status_text="Track changed."
        )

    @commands.Cog.listener()
    async def on_musicbot_queue_updated(self, guild: discord.Guild) -> None:
        player = self.players.get(guild.id)
        # Reset activity to idle when queue empties.
        if player and not player.current and not player.queue:
            await self._update_bot_presence()
        self._persist_snapshot(guild.id)
        self._schedule_prefetch(guild.id)
        self._schedule_np_refresh(guild.id)

    @commands.Cog.listener()
    async def on_musicbot_track_near_end(self, guild: discord.Guild) -> None:
        player = self.players.get(guild.id)
        # No point preloading when repeating the same track.
        if player is None or player.loop_mode == "one":
            return
        await self._preload_next_track(guild.id, force_refresh=True)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Clean up all resources when the bot is kicked or leaves a guild."""
        player = self.players.pop(guild.id, None)
        if player:
            await player.destroy()
        self._guild_extract_semaphores.pop(guild.id, None)
        await self._flush_snapshot(guild.id, entries=[])
        for task_dict in (self.snapshot_tasks, self.np_refresh_tasks, self.prefetch_tasks):
            task = task_dict.pop(guild.id, None)
            if task and not task.done():
                task.cancel()
        self.now_playing_messages.pop(guild.id, None)

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

        # Bot's own voice state changed.
        if self.bot.user is not None and member.id == self.bot.user.id:
            if before.channel is not None and after.channel is None:
                # Bot was forcibly disconnected — destroy session.
                await player.destroy()
                self.players.pop(member.guild.id, None)
                self._guild_extract_semaphores.pop(member.guild.id, None)
                await self._flush_snapshot(member.guild.id, entries=[])
                await self._refresh_now_playing_message(member.guild.id)
                await self._update_bot_presence()
            elif after.channel is not None and after.channel != before.channel:
                # Bot was moved to a different channel — update reference and
                # re-evaluate empty-channel state for the new channel.
                player.voice_client = member.guild.voice_client  # type: ignore[assignment]
                await player.refresh_empty_channel_state()
            return

        if before.channel == tracked_channel or after.channel == tracked_channel:
            await player.refresh_empty_channel_state()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="join", aliases=["summon"])
    @commands.guild_only()
    async def join(self, context: commands.Context[Any]) -> None:
        """Dock into your current voice channel."""
        player = await self._join_for_context(context)
        if player.queue:
            self._schedule_prefetch(context.guild.id)
        await context.send("Connected to your voice channel.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="leave", aliases=["disconnect"])
    @commands.guild_only()
    async def leave(self, context: commands.Context[Any]) -> None:
        """Disconnect and wipe the active session."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player:
            await context.send("I am not connected.")
            return
        await player.destroy()
        self.players.pop(context.guild.id, None)
        await self._flush_snapshot(context.guild.id, entries=[])
        await context.send("Disconnected and cleared the queue.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="history")
    @commands.guild_only()
    async def history(self, context: commands.Context[Any]) -> None:
        """Show the last tracks that were played this session."""
        player = self.players.get(context.guild.id)
        hist = list(player.history) if player else []
        if not hist:
            await context.send("No tracks have been played this session.")
            return
        lines = [
            f"`{i}.` [{discord.utils.escape_markdown(t.title)}]({t.webpage_url})"
            f" — <@{t.requester_id}>"
            for i, t in enumerate(reversed(hist), start=1)
        ]
        embed = discord.Embed(
            title="Recent History",
            description="\n".join(lines[:20]),
            colour=EMBED_COLOUR,
        )
        embed.set_footer(text=f"{len(hist)} track(s) in session history.")
        await context.send(embed=embed)

    @commands.hybrid_command(name="why", aliases=["searchdebug", "scorewhy"])
    @commands.guild_only()
    async def why(self, context: commands.Context[Any]) -> None:
        """Show how the last search's results were scored. Use after !play to see why a track won."""
        record = self._last_search.get(context.guild.id)
        if record is None:
            await context.send("No search has been run this session. Use `!play <query>` first.")
            return

        embed = discord.Embed(
            title=f"Score breakdown — `{discord.utils.escape_markdown(record.query_text)}`",
            colour=EMBED_COLOUR,
        )
        lines: list[str] = []
        for c in record.candidates:
            sel = "  ✓" if c.selected else ""
            dur_m, dur_s = divmod(c.duration, 60)
            dur_label = f"{dur_m}:{dur_s:02d}" if c.duration else "?"
            title_short = discord.utils.escape_markdown(c.title[:52])
            detail = (
                f"title={c.title_overlap:.2f} "
                f"artist={c.uploader_overlap:.2f} "
                f"anchor={c.anchor_score:+.2f} "
                f"jp={c.jp_original_bonus:+.2f} "
                f"views={c.view_bonus:+.2f} "
                f"penalty={-c.discouraged_penalty:+.2f}"
            )
            lines.append(
                f"`#{c.rank}` **{c.final_score:+.3f}**{sel} "
                f"[{title_short}]({c.webpage_url})\n"
                f"└ `{dur_label}` · {detail}"
            )

        embed.description = "\n\n".join(lines) if lines else "No data."
        embed.set_footer(text="Press the button for a full per-component DM breakdown.")
        view = ScoreDebugView(author_id=context.author.id, record=record)
        await context.send(embed=embed, view=view)

    @commands.hybrid_command(name="skipto")
    @commands.guild_only()
    async def skipto(self, context: commands.Context[Any], position: int) -> None:
        """Skip ahead to a specific queue position, dropping everything before it."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is empty.")
            return
        self._remember_channel(player, context.channel)
        size = len(player.queue)
        if position < 1 or position > size:
            await context.send(f"Position `{position}` is out of range (queue has {size} tracks).")
            return
        if position == 1:
            await context.send("That is already the next track. Use `!skip` to skip the current one.")
            return
        queue_list = list(player.queue)
        dropped = position - 1
        player.queue = deque(queue_list[position - 1:])
        self._persist_snapshot(context.guild.id)
        target = player.queue[0]
        # FIX #19: rich embed confirmation consistent with the rest of the bot.
        embed = discord.Embed(
            title="Jumped to Position",
            colour=EMBED_COLOUR,
        )
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
        if player.current and player.voice_client:
            player.skip()

    @commands.hybrid_command(name="replay")
    @commands.guild_only()
    async def replay(self, context: commands.Context[Any]) -> None:
        """Re-queue the current track so it plays again after the queue ends."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or not player.current:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        if len(player.queue) >= self.bot.settings.max_queue_size:
            await context.send("Queue is full.")
            return
        clone = dataclasses.replace(player.current, requester_id=context.author.id)
        clone.stream_url = ""
        clone.resolved_at = 0.0
        await player.enqueue(clone, front=True)
        self._persist_snapshot(context.guild.id)
        await self._refresh_now_playing_message(context.guild.id)
        await context.send(
            f"Re-queued **{discord.utils.escape_markdown(player.current.title)}** to play next."
        )

    @commands.hybrid_command(name="qsearch", aliases=["qs"])
    @commands.guild_only()
    async def qsearch(self, context: commands.Context[Any], *, keyword: str) -> None:
        """Search within the current queue. e.g. !qsearch arctic monkeys"""
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is empty.")
            return
        kw = keyword.strip().lower()
        matches: list[tuple[int, Track]] = [
            (i + 1, track)
            for i, track in enumerate(player.queue)
            if kw in track.title.lower() or kw in (track.uploader or "").lower()
        ]
        if not matches:
            await context.send(f"No tracks in the queue matching `{discord.utils.escape_markdown(keyword)}`.")
            return
        lines = [
            f"`{pos}.` [{discord.utils.escape_markdown(t.title)}]({t.webpage_url})"
            for pos, t in matches[:20]
        ]
        embed = discord.Embed(
            title=f"Queue Search: {discord.utils.escape_markdown(keyword)}",
            description="\n".join(lines),
            colour=EMBED_COLOUR,
        )
        if len(matches) > 20:
            embed.set_footer(text=f"Showing first 20 of {len(matches)} matches.")
        else:
            embed.set_footer(text=f"{len(matches)} match(es) found.")
        await context.send(embed=embed)

    @commands.hybrid_command(name="play", aliases=["p"])
    @commands.guild_only()
    async def play(self, context: commands.Context[Any], *, query: str) -> None:
        """Queue a URL, playlist, or search query. Searches go direct via YouTube Music."""
        player = await self._join_for_context(context)
        self._schedule_prefetch(context.guild.id)
        if len(player.queue) >= self.bot.settings.max_queue_size:
            await context.send("Queue is full. Clear or play tracks before adding more.")
            return

        query = self._normalize_query(query)

        # FIX #20: for URL/playlist queries send an immediate acknowledgement so
        # users don't see silence while yt-dlp resolves. Text queries already show
        # the search selector so they don't need an extra message.
        is_url = query.startswith(("http://", "https://"))
        fetch_msg: discord.Message | None = None
        if is_url:
            fetch_msg = await context.send("🔍 Fetching…")

        async with context.typing():
            tracks, skipped = await self._extract_tracks(
                query, requester_id=context.author.id,
                guild_id=context.guild.id,
            )

        if not tracks:
            msg = (
                f"No playable results found. Skipped `{skipped}` unavailable items."
                if skipped
                else "No playable results found. Try `!search <query>` to browse manually."
            )
            if fetch_msg:
                await fetch_msg.edit(content=msg)
            else:
                await context.send(msg)
            return

        added = 0
        for track in tracks:
            if len(player.queue) >= self.bot.settings.max_queue_size:
                break
            await player.enqueue(track)
            added += 1

        self._persist_snapshot(context.guild.id)
        self._schedule_prefetch(context.guild.id)
        await self._refresh_now_playing_message(context.guild.id)
        suffix = f" Skipped `{skipped}` unavailable items." if skipped else ""
        if added == 1:
            t = tracks[0]
            result = f"Queued [{discord.utils.escape_markdown(t.title)}]({t.webpage_url}).{suffix}"
        else:
            result = f"Queued `{added}` tracks from the playlist/search results.{suffix}"

        if fetch_msg:
            await fetch_msg.edit(content=result)
        else:
            await context.send(result)

    @commands.hybrid_command(name="playnext", aliases=["pn"])
    @commands.guild_only()
    async def playnext(self, context: commands.Context[Any], *, query: str) -> None:
        """Insert a track next in queue. Plain text uses YouTube Music direct."""
        await self._require_dj(context)
        player = await self._join_for_context(context)
        query = self._normalize_query(query)
        async with context.typing():
            tracks, _ = await self._extract_tracks(
                query, requester_id=context.author.id,
                guild_id=context.guild.id,
            )
        track = tracks[0] if tracks else None

        if track is None:
            await context.send("No playable result found.")
            return
        await player.enqueue(track, front=True)
        self._persist_snapshot(context.guild.id)
        self._schedule_prefetch(context.guild.id)
        await self._refresh_now_playing_message(context.guild.id)
        await context.send(
            f"Queued next: [{discord.utils.escape_markdown(track.title)}]({track.webpage_url})."
        )

    @commands.hybrid_command(name="repeat", aliases=["rp"])
    @commands.guild_only()
    async def repeat(self, context: commands.Context[Any]) -> None:
        """Toggle repeat for the current track. Shortcut for !loop one / !loop off."""
        player = self.players.get(context.guild.id)
        if not player or not player.current:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        if player.loop_mode == "one":
            player.loop_mode = "off"
            await context.send("🔁 Repeat **off**.")
        else:
            player.loop_mode = "one"
            await context.send(
                f"🔂 Repeating **{discord.utils.escape_markdown(player.current.title)}**."
            )
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="search", aliases=["find", "s"])
    @commands.guild_only()
    async def search(self, context: commands.Context[Any], *, query: str) -> None:
        """Browse search results and pick one to queue. Use when !play gets the wrong track."""
        player = await self._join_for_context(context)
        if len(player.queue) >= self.bot.settings.max_queue_size:
            await context.send("Queue is full.")
            return
        self._remember_channel(player, context.channel)
        # Always use regular ytsearch here so users see the full result list.
        search_query = f"ytsearch{self._search_result_count(query)}:{self._preprocess_query(query)}"
        async with context.typing():
            tracks, skipped = await self._extract_search_candidates(
                search_query, requester_id=context.author.id
            )
        selected = await self._prompt_for_search_selection(
            context, search_query, tracks, mode="play"
        )
        if selected is None:
            if not tracks:
                await context.send("No results found.")
            return
        await player.enqueue(selected)
        self._persist_snapshot(context.guild.id)
        self._schedule_prefetch(context.guild.id)
        await self._refresh_now_playing_message(context.guild.id)
        await context.send(
            f"Queued [{discord.utils.escape_markdown(selected.title)}]({selected.webpage_url})."
        )

    @commands.hybrid_command(name="skip", aliases=["next"])
    @commands.guild_only()
    async def skip(self, context: commands.Context[Any]) -> None:
        """Vote-skip or instantly skip if you have control."""
        player = self.players.get(context.guild.id)
        if not player:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        message = await self._skip_for_member(player, context.author)
        await context.send(message)
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="prev", aliases=["previous", "back"])
    @commands.guild_only()
    async def previous(self, context: commands.Context[Any]) -> None:
        """Jump back to the last completed track."""
        player = self.players.get(context.guild.id)
        if not player:
            await context.send("Nothing is playing.")
            return
        self._remember_channel(player, context.channel)
        message = await self._previous_for_member(player, context.author)
        await context.send(message)
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="forceskip", aliases=["fs"])
    @commands.guild_only()
    async def forceskip(self, context: commands.Context[Any]) -> None:
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

    @commands.hybrid_command(name="stop")
    @commands.guild_only()
    async def stop(self, context: commands.Context[Any]) -> None:
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

    @commands.hybrid_command(name="pause")
    @commands.guild_only()
    async def pause(self, context: commands.Context[Any]) -> None:
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

    @commands.hybrid_command(name="resume")
    @commands.guild_only()
    async def resume(self, context: commands.Context[Any]) -> None:
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

    @commands.hybrid_command(name="nowplaying", aliases=["np"])
    @commands.guild_only()
    async def nowplaying(self, context: commands.Context[Any]) -> None:
        """Open the live control panel with buttons."""
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

    @commands.hybrid_command(name="queue", aliases=["q"])
    @commands.guild_only()
    async def queue(self, context: commands.Context[Any]) -> None:
        """Inspect the current track stack."""
        # Intentionally NOT calling _get_player here — we don't want to create
        # a player, load a snapshot, and start the idle countdown just to say
        # "the queue is empty."
        player = self.players.get(context.guild.id)
        if not player or (not player.current and not player.queue):
            await context.send("Queue is empty.")
            return
        self._remember_channel(player, context.channel)
        view = self._build_queue_view(
            context.guild.id,
            player,
            author_id=context.author.id,
        )
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
        player.queue = deque(queue_list)
        self._persist_snapshot(context.guild.id)
        await context.send(f"Removed **{discord.utils.escape_markdown(removed.title)}** from the queue.")
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
        player.queue.clear()
        self._persist_snapshot(context.guild.id)
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
        player.queue = deque(shuffled)
        self._persist_snapshot(context.guild.id)
        await context.send("Shuffled the queue.")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_command(name="move")
    @commands.guild_only()
    async def move(
        self,
        context: commands.Context[Any],
        from_index: int,
        to_index: int,
    ) -> None:
        """Move a track from one queue position to another. e.g. !move 10 2"""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or not player.queue:
            await context.send("Queue is empty.")
            return
        self._remember_channel(player, context.channel)
        size = len(player.queue)
        if from_index < 1 or from_index > size:
            await context.send(f"Position `{from_index}` is out of range (queue has {size} tracks).")
            return
        if to_index < 1 or to_index > size:
            await context.send(f"Position `{to_index}` is out of range (queue has {size} tracks).")
            return
        if from_index == to_index:
            await context.send("Source and destination are the same position.")
            return
        queue_list = list(player.queue)
        track = queue_list.pop(from_index - 1)
        queue_list.insert(to_index - 1, track)
        player.queue = deque(queue_list)
        self._persist_snapshot(context.guild.id)
        await context.send(
            f"Moved **{discord.utils.escape_markdown(track.title)}** "
            f"from position `{from_index}` to `{to_index}`."
        )
        await self._refresh_now_playing_message(context.guild.id)


    @commands.hybrid_command(name="loop")
    @commands.guild_only()
    async def loop(self, context: commands.Context[Any]) -> None:
        """Cycle loop mode: off → single track → full queue → off."""
        await self._require_dj(context)
        player = self.players.get(context.guild.id)
        if not player or (not player.current and not player.queue):
            await context.send("Nothing is loaded.")
            return
        self._remember_channel(player, context.channel)
        # FIX #17: capture previous state before cycling so we can show the transition.
        prev_label = LOOP_LABELS.get(player.loop_mode, "Off")
        player.loop_mode = LOOP_CYCLE.get(player.loop_mode, "off")
        self._persist_snapshot(context.guild.id)
        label = LOOP_LABELS.get(player.loop_mode, "Off")
        icon = LOOP_ICONS.get(player.loop_mode, "→")
        await context.send(f"Loop changed: **{prev_label}** → {icon} **{label}**")
        await self._refresh_now_playing_message(context.guild.id)

    @commands.hybrid_group(name="playlist", invoke_without_command=True)
    @commands.guild_only()
    async def playlist(self, context: commands.Context[Any]) -> None:
        """Work with saved server playlists."""
        await context.send(
            "Use `playlist save`, `playlist load`, `playlist list`, "
            "`playlist show`, or `playlist delete`."
        )

    @playlist.command(name="save")
    @commands.guild_only()
    async def playlist_save(self, context: commands.Context[Any], name: str) -> None:
        """Save the current queue as a named playlist."""
        player = self.players.get(context.guild.id)
        if not player or (not player.current and not player.queue):
            await context.send("Nothing is loaded to save.")
            return
        entries = player.snapshot()
        await self.bot.database.save_playlist(
            context.guild.id, name.lower(), context.author.id, entries
        )
        await context.send(f"Saved `{len(entries)}` tracks to playlist `{name.lower()}`.")

    @playlist.command(name="list")
    @commands.guild_only()
    async def playlist_list(self, context: commands.Context[Any]) -> None:
        """List all saved playlists for this server."""
        rows = await self.bot.database.list_playlists(context.guild.id)
        if not rows:
            await context.send("No saved playlists for this server.")
            return
        # FIX #13: show all playlists — split into ≤25-entry pages if needed.
        PAGE = 25
        page_count = math.ceil(len(rows) / PAGE)
        embeds: list[discord.Embed] = []
        for page in range(page_count):
            chunk = rows[page * PAGE:(page + 1) * PAGE]
            lines = [
                f"`{row['name']}` — {row['track_count']} tracks — <@{row['created_by']}>"
                for row in chunk
            ]
            title = "Saved Playlists" if page_count == 1 else f"Saved Playlists (page {page+1}/{page_count})"
            embeds.append(discord.Embed(
                title=title,
                description="\n".join(lines),
                colour=EMBED_COLOUR,
            ).set_footer(text=f"{len(rows)} playlist(s) total"))
        for embed in embeds:
            await context.send(embed=embed)

    @playlist.command(name="show")
    @commands.guild_only()
    async def playlist_show(self, context: commands.Context[Any], name: str) -> None:
        """Show the tracks in a saved playlist."""
        rows = await self.bot.database.get_playlist_entries(context.guild.id, name.lower())
        if not rows:
            await context.send("Playlist not found.")
            return
        # FIX #14: show all tracks via paginated embeds instead of truncating at 10.
        PAGE = 15
        page_count = math.ceil(len(rows) / PAGE)
        embeds: list[discord.Embed] = []
        for page in range(page_count):
            chunk = rows[page * PAGE:(page + 1) * PAGE]
            lines = [
                f"`{index}.` {discord.utils.escape_markdown(row['title'])}"
                for index, row in enumerate(chunk, start=page * PAGE + 1)
            ]
            title = (
                f"Playlist: {name.lower()}"
                if page_count == 1
                else f"Playlist: {name.lower()} (page {page+1}/{page_count})"
            )
            embeds.append(discord.Embed(
                title=title,
                description="\n".join(lines),
                colour=EMBED_COLOUR,
            ).set_footer(text=f"{len(rows)} track(s) total"))
        for embed in embeds:
            await context.send(embed=embed)

    @playlist.command(name="load")
    @commands.guild_only()
    async def playlist_load(self, context: commands.Context[Any], name: str) -> None:
        """Load a saved playlist into the queue."""
        player = await self._join_for_context(context)
        rows = await self.bot.database.get_playlist_entries(context.guild.id, name.lower())
        if not rows:
            await context.send("Playlist not found.")
            return

        added = 0
        skipped = 0
        async with context.typing():
            for row in rows[: self.bot.settings.max_playlist_size]:
                if len(player.queue) >= self.bot.settings.max_queue_size:
                    break
                query = row["query"]
                webpage_url = row["webpage_url"] or query
                if not query or not webpage_url:
                    skipped += 1
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

        self._persist_snapshot(context.guild.id)
        self._schedule_prefetch(context.guild.id)
        suffix = f" Skipped `{skipped}` unavailable items." if skipped else ""
        await context.send(f"Loaded `{added}` tracks from playlist `{name.lower()}`.{suffix}")
        await self._refresh_now_playing_message(context.guild.id)

    @playlist.command(name="delete")
    @commands.guild_only()
    async def playlist_delete(self, context: commands.Context[Any], name: str) -> None:
        """Delete a saved playlist."""
        await self._require_dj(context)
        if not await self.bot.database.delete_playlist(context.guild.id, name.lower()):
            await context.send("Playlist not found.")
            return
        await context.send(f"Deleted playlist `{name.lower()}`.")
