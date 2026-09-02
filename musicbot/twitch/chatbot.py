"""Twitch chat bot — handles !sr <query> and hands resolved requests to the relay.

Built against twitchio 3.x's EventSub-based Bot (verified against the actual
installed twitchio==3.3.2 API — this is NOT the old IRC-token pattern from
twitchio 2.x, which Twitch has been deprecating in favor of EventSub).

Auth model used here is the simplest supported one ("Installed Chatbot" style,
per Twitch's own chat bot guide): a single Twitch account (recommended: a
dedicated account named after the bot, made a moderator in your channel) with
a User Access Token carrying `user:read:chat` + `user:write:chat`. That
moderator status is what satisfies the ChatMessageSubscription requirement
without needing a separate broadcaster-side `channel:bot` grant.

ONE-TIME SETUP — this part doesn't happen automatically and isn't optional:
    Neither `client_id`/`client_secret` below nor anything else in this repo
    can, by itself, obtain the User Access Token this bot needs — a
    client-credentials ("app") token has no user scopes and can't read or send
    chat as a specific account. TwitchIO 3.x handles the missing piece with a
    small built-in web server (twitchio.web.AiohttpAdapter), started
    automatically by commands.Bot when no custom adapter is supplied, that
    listens on http://localhost:4343 and persists whatever token you
    authorize through it (see load_tokens/save_tokens below for exactly
    where). To complete it:

      1. Start the bot once with valid TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
         / TWITCH_BOT_ID / TWITCH_OWNER_ID set (TWITCH_STREAM_KEY too, since
         that's what gates whether any of this runs at all — see
         Settings.twitch_enabled).
      2. The adapter binds to localhost only, so on a remote VPS you'll need
         an SSH tunnel to reach it: `ssh -L 4343:localhost:4343 <user>@<host>`
         from your own machine, kept open while you do steps 3-4.
      3. In a browser, logged in as the BOT's own Twitch account, visit:
         http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot
      4. In a browser, logged in as the BROADCASTER's account (i.e. the
         channel this bot will post in), visit:
         http://localhost:4343/oauth?scopes=channel:bot
         (Step 4 is optional if the bot account already has moderator status
         in that channel — see the auth-model paragraph above — but doing it
         anyway costs nothing and removes the "is it still a mod" dependency.)

    Once both are done, the tokens are saved to TWITCH_TOKEN_FILE (default:
    data/twitch_tokens.json — inside DATA_DIR specifically so it survives
    under the hardened systemd unit's ReadWritePaths, and is gitignored via
    the existing `data/` entry) and reloaded automatically on every future
    start. You will not need to repeat this unless that file is deleted or
    Twitch revokes the token.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from twitchio import eventsub
from twitchio.ext import commands

from musicbot.twitch.relay import QueuedRequest, TwitchRadioRelay
from musicbot.twitch.tunables import TwitchTunables

if TYPE_CHECKING:
    from musicbot.cogs.music.models import Track
    from musicbot.twitch.relay import TrackResolver

log = logging.getLogger(__name__)


class SongRequestComponent(commands.Component):
    def __init__(self, bot: "TwitchChatBot") -> None:
        self.bot = bot
        self._pending_by_chatter: dict[str, int] = {}
        self._last_request_at: dict[str, float] = {}

    @commands.command(name="sr", aliases=["songrequest"])
    async def song_request(self, ctx: commands.Context, *, query: str = "") -> None:
        if not query.strip():
            await ctx.reply("Usage: !sr <song name or URL>")
            return

        tunables = self.bot.tunables
        chatter_key = ctx.chatter.id

        if tunables.request_cooldown_seconds > 0:
            last = self._last_request_at.get(chatter_key)
            if last is not None:
                remaining = tunables.request_cooldown_seconds - (time.monotonic() - last)
                if remaining > 0:
                    await ctx.reply(f"Wait {round(remaining)}s before your next request.")
                    return

        if self.bot.relay.queue_length >= tunables.queue_cap:
            await ctx.reply("Queue's full right now — try again in a bit.")
            return

        if self._pending_by_chatter.get(chatter_key, 0) >= tunables.max_pending_per_chatter:
            await ctx.reply(
                f"You've already got {tunables.max_pending_per_chatter} queued — wait for one to play first."
            )
            return

        try:
            tracks, _ = await self.bot.resolver(query, requester_id=0, twitch_mode=True, limit=1)
        except Exception:
            log.exception("Twitch !sr resolve failed for query: %s", query)
            await ctx.reply("Couldn't find or resolve that — try a different search or link.")
            return

        if not tracks:
            await ctx.reply("No results for that.")
            return

        track: "Track" = tracks[0]
        if track.duration <= 0:
            # yt-dlp reports duration as None (-> 0 in this codebase's Track
            # construction) for live streams — there's no sane "duration limit"
            # check for something with no end, and letting one through would
            # hang the whole relay queue on it until the live stream itself
            # ends, with no way to recover short of !skip.
            await ctx.reply("Can't queue a live stream or anything without a fixed length.")
            return
        if track.duration > tunables.max_request_duration_seconds:
            minutes = tunables.max_request_duration_seconds // 60
            await ctx.reply(f"That's over the {minutes}-minute limit — pick something shorter.")
            return

        requester_name = ctx.chatter.display_name or ctx.chatter.name
        self._last_request_at[chatter_key] = time.monotonic()
        self._pending_by_chatter[chatter_key] = self._pending_by_chatter.get(chatter_key, 0) + 1
        position = self.bot.relay.enqueue(
            QueuedRequest(
                webpage_url=track.webpage_url,
                title=track.title,
                uploader=track.uploader,
                thumbnail_url=track.thumbnail_url,
                requester_name=requester_name,
                requester_id=0,
                on_finished=lambda: self._release_pending(chatter_key),
            )
        )
        await ctx.reply(f"Queued: {track.title} (#{position} in line)")

    def _release_pending(self, chatter_key: str) -> None:
        remaining = self._pending_by_chatter.get(chatter_key, 0) - 1
        if remaining <= 0:
            self._pending_by_chatter.pop(chatter_key, None)
        else:
            self._pending_by_chatter[chatter_key] = remaining

    @commands.command(name="nowplaying", aliases=["np"])
    async def now_playing(self, ctx: commands.Context) -> None:
        np = self.bot.relay.now_playing
        if np is None:
            await ctx.reply("Nothing's playing right now — !sr <song> to queue something.")
            return
        await ctx.reply(f"Now playing: {np.title} (requested by {np.requester_name})")

    @commands.command(name="queue")
    async def queue(self, ctx: commands.Context) -> None:
        await ctx.reply(f"{self.bot.relay.queue_length} request(s) queued.")

    @commands.is_elevated()
    @commands.command(name="skip")
    async def skip(self, ctx: commands.Context) -> None:
        np = self.bot.relay.now_playing
        if not self.bot.relay.skip_current():
            await ctx.reply("Nothing's playing right now.")
            return
        await ctx.reply(f"Skipped: {np.title}" if np else "Skipped.")


class TwitchChatBot(commands.Bot):
    """One Twitch bot account, subscribed to a single channel's chat via EventSub.

    `resolver` is MusicCog._extract_tracks, passed in directly rather than
    imported — this bot doesn't construct or own a MusicCog, it just calls into
    the one the Discord side already built, so there's exactly one yt-dlp
    thread pool / resolve cache / extraction semaphore in the whole process.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        bot_id: str,
        owner_id: str,
        prefix: str,
        resolver: "TrackResolver",
        relay: TwitchRadioRelay,
        tunables: TwitchTunables,
        token_storage_path: Path,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            bot_id=bot_id,
            owner_id=owner_id,
            prefix=prefix,
        )
        self.resolver = resolver
        self.relay = relay
        self.tunables = tunables
        self._owner_id = owner_id
        self._bot_id = bot_id
        self._token_storage_path = token_storage_path

    async def load_tokens(self, path: str | None = None, /) -> None:
        # Overridden to redirect TwitchIO's default token file (".tio.tokens.json"
        # in the process's working directory, which the hardened systemd unit's
        # ReadWritePaths doesn't cover — see deploy/musicbot.service) into
        # DATA_DIR instead, where it's both writable and already gitignored.
        # `path` is only non-None if a caller explicitly overrides it; there
        # isn't one here, so this always resolves to self._token_storage_path.
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        await super().load_tokens(path or str(self._token_storage_path))

    async def save_tokens(self, path: str | None = None, /) -> None:
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        await super().save_tokens(path or str(self._token_storage_path))

    async def setup_hook(self) -> None:
        await self.add_component(SongRequestComponent(self))
        subscription = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self._owner_id,
            user_id=self._bot_id,
        )
        await self.subscribe_websocket(payload=subscription)
        log.info("Twitch chat bot subscribed to channel %s chat", self._owner_id)

    async def event_ready(self) -> None:
        log.info("Twitch chat bot ready (bot_id=%s)", self._bot_id)
