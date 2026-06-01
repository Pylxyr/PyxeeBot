"""
curation.py — Playlist Curation Cog for PyxeeBot
Searches Last.fm for similar tracks, lets the user curate a 25-track list,
saves it as a playlist, and auto-refills the queue when it drops to ≤ 10 tracks.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord.ext import commands

if TYPE_CHECKING:
    from musicbot.bot import MusicBot
    from musicbot.cogs.music import MusicCog

log = logging.getLogger(__name__)

LASTFM_API   = "https://ws.audioscrobbler.com/2.0/"
MAX_PLAYLIST = 25
REFILL_AT    = 10   # trigger auto-refill when queue drops to this many tracks
REFILL_MAX   = 15   # how many new tracks to suggest at refill time


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CuratedTrack:
    title:   str
    artist:  str
    selected: bool = True          # True = will be added to queue


@dataclass
class CurationSession:
    guild_id:    int
    author_id:   int
    seed_query:  str                # original user query
    seed_artist: str                # resolved artist for auto-refill
    seed_track:  str                # resolved track for auto-refill
    tracks:      list[CuratedTrack] = field(default_factory=list)
    panel_msg:   discord.Message | None = None
    channel_id:  int | None = None


# ---------------------------------------------------------------------------
# UI: curation panel view
# ---------------------------------------------------------------------------

class CurationView(discord.ui.View):
    """Panel attached to a curation embed.
    Offers a multi-select dropdown to remove tracks, plus action buttons.
    """

    def __init__(self, cog: "CurationCog", session: CurationSession) -> None:
        super().__init__(timeout=300)
        self.cog     = cog
        self.session = session
        self._build_select()

    def _build_select(self) -> None:
        """(Re)build the remove-tracks dropdown from current session tracks."""
        # Clear old select if present
        for item in list(self.children):
            if isinstance(item, discord.ui.Select):
                self.remove_item(item)

        tracks = self.session.tracks
        if not tracks:
            return

        options = [
            discord.SelectOption(
                label=f"{i+1}. {t.artist} – {t.title}"[:100],
                value=str(i),
                default=False,
            )
            for i, t in enumerate(tracks)
        ]

        select = discord.ui.Select(
            placeholder="Select tracks to remove…",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        select.callback = self._on_remove_select
        self.add_item(select)

    async def _on_remove_select(self, interaction: discord.Interaction) -> None:
        # FIX #18: apply the removal immediately on dropdown confirm — no separate
        # "Remove Selected" button press needed. The old two-step flow was confusing.
        to_remove = set(int(v) for v in interaction.data.get("values", []))  # type: ignore[arg-type]
        if not to_remove:
            await interaction.response.defer()
            return
        for idx in to_remove:
            self.session.tracks[idx].selected = False
        self.session.tracks = [t for t in self.session.tracks if t.selected]
        self._build_select()
        embed = self.cog._build_session_embed(self.session)
        await interaction.response.edit_message(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.session.author_id:
            return True
        await interaction.response.send_message(
            "Only the person who started this curation can use these controls.",
            ephemeral=True,
        )
        return False

    # ── Buttons ────────────────────────────────────────────────────────────

    @discord.ui.button(label="Queue All", style=discord.ButtonStyle.success, row=1)
    async def queue_all(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        # Disable the entire view immediately so the button can't be double-clicked
        # while the (potentially long) yt-dlp resolution is running.
        self._disable_all()
        await interaction.response.edit_message(
            content="Resolving tracks and adding to queue…",
            view=self,
        )

        # FIX #18: removals are applied immediately by _on_remove_select,
        # so we just read .selected directly.
        selected = [t for t in self.session.tracks if t.selected]
        if not selected:
            await interaction.followup.send("No tracks selected.", ephemeral=True)
            return

        queued, failed = await self.cog._resolve_and_queue(
            self.session.guild_id, self.session.author_id, selected, interaction
        )
        result_msg = (
            f"Queued {queued} track(s)."
            + (f" ({failed} could not be resolved.)" if failed else "")
        )
        await interaction.edit_original_response(content=result_msg, embed=None, view=None)
        del self.cog._sessions[self.session.guild_id]

    @discord.ui.button(label="Save Playlist", style=discord.ButtonStyle.primary, row=1)
    async def save_playlist(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = SavePlaylistModal(self.cog, self.session)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._disable_all()
        await interaction.response.edit_message(
            content="Curation cancelled.", embed=None, view=self
        )
        self.cog._sessions.pop(self.session.guild_id, None)

    def _disable_all(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

    async def on_timeout(self) -> None:
        self._disable_all()
        self.cog._sessions.pop(self.session.guild_id, None)
        if self.session.panel_msg:
            with contextlib.suppress(discord.HTTPException):
                await self.session.panel_msg.edit(view=self)


class SavePlaylistModal(discord.ui.Modal, title="Save Curated Playlist"):
    name: discord.ui.TextInput = discord.ui.TextInput(
        label="Playlist name",
        placeholder="e.g. my jpop vibes",
        max_length=50,
        required=True,
    )

    def __init__(self, cog: "CurationCog", session: CurationSession) -> None:
        super().__init__()
        self.cog     = cog
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        pl_name = self.name.value.strip()
        selected = [t for t in self.session.tracks if t.selected]
        entries = [
            {"query": f"{t.artist} - {t.title}", "title": f"{t.artist} – {t.title}", "webpage_url": ""}
            for t in selected
        ]
        await self.cog.bot.database.save_playlist(
            self.session.guild_id, pl_name, interaction.user.id, entries
        )
        await interaction.followup.send(
            f"Saved {len(selected)} tracks as **{discord.utils.escape_markdown(pl_name)}**.\n"
            f"Use `!vibe-load {pl_name}` to queue it later."
        )


# ---------------------------------------------------------------------------
# UI: auto-refill approval view
# ---------------------------------------------------------------------------

class RefillView(discord.ui.View):

    def __init__(
        self,
        cog: "CurationCog",
        guild_id: int,
        author_id: int,
        tracks: list[CuratedTrack],
    ) -> None:
        super().__init__(timeout=180)
        self.cog      = cog
        self.guild_id = guild_id
        self.author_id = author_id
        self.tracks   = tracks
        self._build_select()
        self.message: discord.Message | None = None

    def _build_select(self) -> None:
        for item in list(self.children):
            if isinstance(item, discord.ui.Select):
                self.remove_item(item)
        options = [
            discord.SelectOption(
                label=f"{i+1}. {t.artist} – {t.title}"[:100],
                value=str(i),
                default=False,
            )
            for i, t in enumerate(self.tracks)
        ]
        select = discord.ui.Select(
            placeholder="Select tracks to remove before adding…",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        select.callback = self._noop
        self.add_item(select)

    async def _noop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id == 0 or interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("Not your refill prompt.", ephemeral=True)
        return False

    @discord.ui.button(label="Add All", style=discord.ButtonStyle.success, row=1)
    async def add_all(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._disable()
        await interaction.response.edit_message(
            content="Resolving tracks and adding to queue…",
            view=self,
        )
        for item in self.children:
            if isinstance(item, discord.ui.Select) and item.values:
                for idx_str in item.values:
                    self.tracks[int(idx_str)].selected = False

        selected = [t for t in self.tracks if t.selected]
        queued, failed = await self.cog._resolve_and_queue(
            self.guild_id, self.author_id, selected, interaction
        )
        result_msg = (
            f"Refilled queue with {queued} track(s)."
            + (f" ({failed} failed.)" if failed else "")
        )
        await interaction.edit_original_response(content=result_msg, embed=None, view=None)

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.danger, row=1)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self._disable()
        await interaction.response.edit_message(content="Refill skipped.", embed=None, view=self)

    def _disable(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True

    async def on_timeout(self) -> None:
        self._disable()
        if self.message:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)


# ---------------------------------------------------------------------------
# Curation Cog
# ---------------------------------------------------------------------------

class CurationCog(commands.Cog, name="CurationCog"):
    """Discover and curate playlists via Last.fm similar-track recommendations."""

    def __init__(self, bot: "MusicBot") -> None:
        self.bot      = bot
        self._key     = getattr(bot.settings, "lastfm_api_key", None)
        self._session: aiohttp.ClientSession | None = None
        # Active per-guild curation sessions (ephemeral, in-memory only)
        self._sessions:       dict[int, CurationSession] = {}
        # Last known queue length per guild (for refill detection)
        self._last_queue_len: dict[int, int] = {}
        # Refill seed per guild: (artist, track) for Last.fm lookups
        self._refill_seeds:   dict[int, tuple[str, str]] = {}

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()
        if not self._key:
            log.warning("LASTFM_API_KEY not set — CurationCog will not work.")
        else:
            log.info("CurationCog ready (Last.fm key configured).")

    async def cog_unload(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Internal Last.fm helpers ────────────────────────────────────────────

    async def _lastfm(self, method: str, **params: Any) -> dict[str, Any] | None:
        """Call the Last.fm API and return the parsed JSON, or None on error."""
        if not self._session or not self._key:
            return None
        try:
            async with self._session.get(
                LASTFM_API,
                params={"method": method, "api_key": self._key,
                        "format": "json", **params},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except Exception:
            return None

    async def _search_track(self, query: str) -> tuple[str, str] | None:
        """Return (artist, track) for a free-text query via Last.fm track.search.

        Fetches up to 5 results and prefers whichever result's artist name
        fuzzy-matches the query (handles artist-only queries like "yorushika").
        """
        data = await self._lastfm("track.search", track=query, limit=5)
        if not data:
            return None
        results = data.get("results", {}).get("trackmatches", {}).get("track", [])
        if not results:
            return None
        query_lower = query.strip().lower()
        # Prefer a result where the artist name matches what the user typed.
        for result in results:
            artist_name = str(result.get("artist", "")).strip()
            if (
                query_lower in artist_name.lower()
                or artist_name.lower() in query_lower
            ):
                return artist_name, str(result.get("name", ""))
        # Fall back to the top result.
        top = results[0]
        return str(top.get("artist", "")), str(top.get("name", ""))

    async def _get_similar_tracks(
        self, artist: str, track: str, limit: int = MAX_PLAYLIST
    ) -> list[CuratedTrack]:
        """Return up to `limit` tracks from SIMILAR ARTISTS (not the same artist).

        Strategy:
          1. artist.getSimilar  → up to 15 similar artists (genre/vibe based)
          2. artist.getTopTracks → 3 top tracks per similar artist
          3. Mix + deduplicate  → genuine cross-artist genre discovery

        Artist dedup uses a normalized key that strips non-ASCII so that
        romanized variants like "Yorushika" and "ヨルシカ" map to the same slot.
        """
        def _artist_key(name: str) -> str:
            """Normalize artist name for dedup: strip non-ASCII then lowercase.
            Falls back to full lowercase for pure-CJK names so they still dedup
            against themselves (e.g. two entries of ヨルシカ stay deduplicated).
            Also strips spaces/punctuation so 'Ryokuoushoku Shakai' ≡ 'ryokuoushokushakai'.
            """
            import re as _re
            ascii_only = _re.sub(r'[^a-z0-9]', '', name.lower())
            return ascii_only if ascii_only else name.lower().strip()

        seed_key = _artist_key(artist)
        seen_titles:  set[str]        = {track.lower()}
        artist_counts: dict[str, int] = {seed_key: 0}  # seed gets 0 so its bonus tracks count
        result: list[CuratedTrack]    = []

        # ── 1. Get 15 similar artists ──────────────────────────────────────
        data = await self._lastfm("artist.getsimilar", artist=artist, limit=15)
        similar_artists: list[str] = []
        if data:
            for item in data.get("similarartists", {}).get("artist", []):
                name = str(item.get("name", "")).strip()
                if name and _artist_key(name) != seed_key:
                    similar_artists.append(name)

        # ── 2. Fetch top tracks from each similar artist (parallel) ────────
        tasks = [
            self._lastfm("artist.gettoptracks", artist=a, limit=3)
            for a in similar_artists
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for a, resp in zip(similar_artists, responses):
            if not isinstance(resp, dict):
                continue
            akey = _artist_key(a)
            for item in resp.get("toptracks", {}).get("track", []):
                t = str(item.get("name", "")).strip()
                if not t or t.lower() in seen_titles:
                    continue
                if artist_counts.get(akey, 0) >= 2:
                    continue           # max 2 tracks per artist (normalized)
                seen_titles.add(t.lower())
                artist_counts[akey] = artist_counts.get(akey, 0) + 1
                result.append(CuratedTrack(title=t, artist=a))

        # ── 3. Seed artist gets guaranteed slots (up to 5) placed first ────
        seed_data = await self._lastfm("artist.gettoptracks", artist=artist, limit=6)
        seed_tracks: list[CuratedTrack] = []
        if seed_data:
            for item in seed_data.get("toptracks", {}).get("track", []):
                t = str(item.get("name", "")).strip()
                if t and t.lower() not in seen_titles:
                    seen_titles.add(t.lower())
                    seed_tracks.append(CuratedTrack(title=t, artist=artist))
                if len(seed_tracks) >= 5:
                    break

        # Shuffle only the similar-artist tracks, then put seed tracks first
        # so they are never cut by the [:limit] slice.
        random.shuffle(result)
        combined = seed_tracks + result
        return combined[:limit]

    async def _get_artist_top_tracks(
        self, artist: str, limit: int = MAX_PLAYLIST
    ) -> list[CuratedTrack]:
        """Fallback: return top tracks for an artist when getSimilar gives nothing."""
        data = await self._lastfm("artist.gettoptracks", artist=artist, limit=limit)
        if not data:
            return []
        raw = data.get("toptracks", {}).get("track", [])
        return [
            CuratedTrack(
                title=str(item.get("name", "")),
                artist=str(item.get("artist", {}).get("name", artist)),
            )
            for item in raw
            if item.get("name")
        ][:limit]

    # ── Session helpers ─────────────────────────────────────────────────────

    def _build_session_embed(self, session: CurationSession) -> discord.Embed:
        tracks = [t for t in session.tracks if t.selected]
        lines = [
            f"`{i+1:02d}.` **{discord.utils.escape_markdown(t.artist)}** — "
            f"{discord.utils.escape_markdown(t.title)}"
            for i, t in enumerate(tracks)
        ]
        embed = discord.Embed(
            title=f"Curated Playlist — {discord.utils.escape_markdown(session.seed_query)}",
            description="\n".join(lines) if lines else "*No tracks remaining.*",
            colour=discord.Colour.from_rgb(255, 170, 64),
        )
        embed.set_footer(
            text=f"{len(tracks)}/{MAX_PLAYLIST} tracks selected  ·  use the dropdown to remove tracks"
        )
        return embed

    def _build_refill_embed(
        self, tracks: list[CuratedTrack], seed: str
    ) -> discord.Embed:
        lines = [
            f"`{i+1:02d}.` **{discord.utils.escape_markdown(t.artist)}** — "
            f"{discord.utils.escape_markdown(t.title)}"
            for i, t in enumerate(tracks)
        ]
        embed = discord.Embed(
            title="Queue Refill",
            description="\n".join(lines) or "*No tracks found.*",
            colour=discord.Colour.from_rgb(255, 170, 64),
        )
        embed.set_footer(
            text=f"Based on: {seed}"
        )
        return embed

    async def _resolve_and_queue(
        self,
        guild_id: int,
        requester_id: int,
        tracks: list[CuratedTrack],
        interaction: discord.Interaction,
    ) -> tuple[int, int]:
        """Resolve each CuratedTrack via yt-dlp and add to MusicCog queue.
        Returns (queued_count, failed_count).
        """
        music: MusicCog | None = self.bot.get_cog("MusicCog")  # type: ignore
        if music is None:
            await interaction.followup.send("MusicCog is not loaded.", ephemeral=True)
            return 0, len(tracks)

        player = music.players.get(guild_id)

        # Auto-join if the requester is in a voice channel
        if player is None:
            guild  = self.bot.get_guild(guild_id)
            member = guild.get_member(requester_id) if guild else None
            vc     = member.voice.channel if member and member.voice else None
            if vc is None:
                await interaction.followup.send(
                    "Join a voice channel first, then try again.", ephemeral=True
                )
                return 0, len(tracks)
            try:
                player = await music._get_player(guild)
                await player.connect(vc)
            except Exception as exc:
                log.exception("Auto-join failed: %s", exc)
                await interaction.followup.send(
                    f"Couldn't join your voice channel: `{exc}`", ephemeral=True
                )
                return 0, len(tracks)

        queued  = 0
        failed  = 0
        total   = len(tracks)
        added:  list[str] = []

        async def _resolve_one(ct: CuratedTrack) -> None:
            nonlocal queued, failed
            query = f"ytsearch5:{ct.artist} - {ct.title}"
            try:
                resolved, _ = await music._extract_tracks(
                    query,
                    requester_id=requester_id,
                    guild_id=guild_id,
                )
                if resolved:
                    await player.enqueue(resolved[0])
                    added.append(f"**{discord.utils.escape_markdown(ct.artist)}** — {discord.utils.escape_markdown(ct.title)}")
                    # Only pin the seed to the first track we successfully queue —
                    # drifting to the last track loses the original vibe.
                    if queued == 0:
                        self._refill_seeds[guild_id] = (ct.artist, ct.title)
                    queued += 1
                else:
                    log.debug("No yt-dlp result for: %s - %s", ct.artist, ct.title)
                    failed += 1
            except Exception as exc:
                log.warning("Failed to resolve %s - %s: %s", ct.artist, ct.title, exc)
                failed += 1

        # Process one at a time — the guild/global extract semaphores already
        # serialize yt-dlp calls. Running concurrently only starves the player.
        batch_size = 5  # keep for progress update cadence only
        for batch_start in range(0, total, batch_size):
            batch = tracks[batch_start: batch_start + batch_size]
            for ct in batch:          # sequential, not gathered
                await _resolve_one(ct)

            progress_lines = "\n".join(f"· {t}" for t in added[-10:])
            pct  = int(queued / total * 100) if total else 100
            bar  = "█" * (pct // 10) + "░" * (10 - pct // 10)
            text = (
                f"`{bar}` {queued}/{total} added\n\n"
                + (progress_lines or "*resolving…*")
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.edit_original_response(content=f"{text}")

        music._persist_snapshot(guild_id)
        return queued, failed

    # ── Commands ────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="vibe", aliases=["vb"])
    @commands.guild_only()
    @commands.cooldown(1, 15, commands.BucketType.guild)
    async def vibe(self, context: commands.Context[Any], *, query: str) -> None:
        """Discover similar songs via Last.fm and curate a playlist. Max 25 tracks."""
        if not self._key:
            await context.send("Last.fm API key is not configured.")
            return

        await context.send(f"Searching Last.fm for tracks similar to `{query}`…")

        # 1. Resolve seed track
        seed = await self._search_track(query)
        if seed is None:
            await context.send("Couldn't find that track on Last.fm. Try `artist - title` format.")
            return
        seed_artist, seed_track = seed

        # 2. Fetch similar tracks
        tracks = await self._get_similar_tracks(seed_artist, seed_track, limit=MAX_PLAYLIST)
        if not tracks:
            # Fallback: top tracks by same artist
            tracks = await self._get_artist_top_tracks(seed_artist, limit=MAX_PLAYLIST)
        if not tracks:
            await context.send("No similar tracks found on Last.fm.")
            return

        # 3. Create session
        session = CurationSession(
            guild_id=context.guild.id,
            author_id=context.author.id,
            seed_query=query,
            seed_artist=seed_artist,
            seed_track=seed_track,
            tracks=tracks,
            channel_id=context.channel.id,
        )
        self._sessions[context.guild.id] = session
        self._refill_seeds[context.guild.id] = (seed_artist, seed_track)

        # 4. Send panel
        embed = self._build_session_embed(session)
        view  = CurationView(self, session)
        msg   = await context.send(embed=embed, view=view)
        session.panel_msg = msg

    @commands.hybrid_command(name="vibe-save", aliases=["vsave"])
    @commands.guild_only()
    async def vibe_save(self, context: commands.Context[Any], *, name: str) -> None:
        """Save the active curation session as a named playlist."""
        session = self._sessions.get(context.guild.id)
        if session is None:
            await context.send("No active curation session. Run `!vibe <query>` first.")
            return
        selected = [t for t in session.tracks if t.selected]
        if not selected:
            await context.send("No tracks in the current session to save.")
            return
        entries = [
            {"query": f"{t.artist} - {t.title}",
             "title": f"{t.artist} – {t.title}", "webpage_url": ""}
            for t in selected
        ]
        await self.bot.database.save_playlist(
            context.guild.id, name.strip(), context.author.id, entries
        )
        await context.send(
            f"Saved {len(selected)} tracks as **{discord.utils.escape_markdown(name.strip())}**."
        )

    @commands.hybrid_command(name="vibe-load", aliases=["vload"])
    @commands.guild_only()
    async def vibe_load(self, context: commands.Context[Any], *, name: str) -> None:
        """Load a saved curated playlist into the queue."""
        entries = await self.bot.database.get_playlist_entries(
            context.guild.id, name.strip()
        )
        if not entries:
            await context.send(f"No saved playlist named **{discord.utils.escape_markdown(name.strip())}**.")
            return

        music: MusicCog | None = self.bot.get_cog("MusicCog")  # type: ignore
        if music is None:
            return

        player = music.players.get(context.guild.id)
        if player is None:
            vc = context.author.voice.channel if context.author.voice else None
            if vc is None:
                await context.send("Join a voice channel first.")
                return
            try:
                player = await music._get_player(context.guild)
                await player.connect(vc)
            except Exception as exc:
                await context.send(f"Couldn't join your voice channel: `{exc}`")
                return

        msg = await context.send(
            f"Loading **{discord.utils.escape_markdown(name.strip())}** — {len(entries)} tracks…"
        )

        queued = 0
        failed = 0

        # FIX #5: resolve all entries concurrently, bounded by the existing guild
        # extract semaphore inside MusicCog._extract_tracks so we don't overload yt-dlp.
        async def _load_one(entry: dict[str, Any]) -> None:
            nonlocal queued, failed
            query = str(entry["query"])
            try:
                tracks, _ = await music._extract_tracks(
                    f"ytsearch1:{query}",
                    requester_id=context.author.id,
                    guild_id=context.guild.id,
                )
                if tracks:
                    await player.enqueue(tracks[0])
                    queued += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        await asyncio.gather(*(_load_one(e) for e in entries))
        music._persist_snapshot(context.guild.id)
        await msg.edit(
            content=f"Loaded {queued} track(s) from **{discord.utils.escape_markdown(name.strip())}**."
            + (f" ({failed} failed.)" if failed else "")
        )

    @commands.hybrid_command(name="vibe-list", aliases=["vlist"])
    @commands.guild_only()
    async def vibe_list(self, context: commands.Context[Any]) -> None:
        """List all saved curated playlists for this server."""
        rows = await self.bot.database.list_playlists(context.guild.id)
        if not rows:
            await context.send("No saved playlists yet. Use `!vibe <query>` to create one.")
            return
        lines = [
            f"`{r['name']}` — {r['track_count']} tracks"
            for r in rows
        ]
        embed = discord.Embed(
            title="Saved Playlists",
            description="\n".join(lines),
            colour=discord.Colour.from_rgb(255, 170, 64),
        )
        await context.send(embed=embed)

    # ── Auto-refill ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_musicbot_queue_updated(self, guild: discord.Guild) -> None:
        """Trigger refill suggestion when queue drops to ≤ REFILL_AT tracks."""
        if not self._key:
            return
        music: MusicCog | None = self.bot.get_cog("MusicCog")  # type: ignore
        if music is None:
            return
        player = music.players.get(guild.id)
        if player is None:
            return

        # Don't refill if bot has already left the voice channel
        if not player.voice_client or not player.voice_client.is_connected():
            self._last_queue_len.pop(guild.id, None)
            return

        # Don't refill while the bot isn't actively playing (e.g. during
        # snapshot restore on startup — this event fires once per track added).
        if not player.voice_client.is_playing() and not player.voice_client.is_paused():
            return

        current_len = len(player.queue) + (1 if player.current else 0)
        prev_len    = self._last_queue_len.get(guild.id, current_len + 1)
        self._last_queue_len[guild.id] = current_len

        # Only trigger on the transition from >REFILL_AT to ≤REFILL_AT
        if not (prev_len > REFILL_AT >= current_len):
            return

        seed = self._refill_seeds.get(guild.id)
        if seed is None:
            return

        seed_artist, seed_track = seed
        task = asyncio.create_task(
            self._do_refill(guild, seed_artist, seed_track),
            name=f"refill-{guild.id}",
        )
        task.add_done_callback(
            lambda t: log.exception("_do_refill failed", exc_info=t.exception())
            if not t.cancelled() and t.exception() is not None
            else None
        )

    async def _do_refill(
        self, guild: discord.Guild, artist: str, track: str
    ) -> None:
        """Fetch REFILL_MAX new similar tracks and post a refill approval prompt."""
        music: MusicCog | None = self.bot.get_cog("MusicCog")  # type: ignore
        if music is None:
            return

        # Find the channel to post in
        channel_id = None
        session    = self._sessions.get(guild.id)
        if session:
            channel_id = session.channel_id
        else:
            player = music.players.get(guild.id)
            if player and player.announce_channel_id:
                channel_id = player.announce_channel_id

        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return

        # Fetch similar tracks (avoid repeats in current queue)
        tracks = await self._get_similar_tracks(artist, track, limit=REFILL_MAX + 10)
        if not tracks:
            return

        # Deduplicate against what's already in the queue
        player = music.players.get(guild.id)
        if player:
            queued_titles = {t.title.lower() for t in player.queue}
            if player.current:
                queued_titles.add(player.current.title.lower())
            tracks = [t for t in tracks if t.title.lower() not in queued_titles]

        tracks = tracks[:REFILL_MAX]
        if not tracks:
            return

        # Find the original requester for the refill session
        author_id = session.author_id if session else (
            player.current.requester_id if player and player.current else 0
        )

        embed = self._build_refill_embed(tracks, f"{artist} – {track}")
        view  = RefillView(self, guild.id, author_id, tracks)
        msg   = await channel.send(
            f"Queue is running low — here are {len(tracks)} more similar tracks:",
            embed=embed,
            view=view,
        )
        view.message = msg


async def setup(bot: "MusicBot") -> None:
    await bot.add_cog(CurationCog(bot))