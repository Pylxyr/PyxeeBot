"""Twitch chat bot — handles !sr <query> and hands resolved requests to the relay.

Built against twitchio 3.x's EventSub-based Bot (verified against the actual
installed twitchio==3.3.2 API — this is NOT the old IRC-token pattern from
twitchio 2.x, which Twitch has been deprecating in favor of EventSub).

Auth model used here is the simplest supported one ("Installed Chatbot" style,
per Twitch's own chat bot guide): a single Twitch account (recommended: a
dedicated account named after the bot, made a moderator in your channel) with
a User Access Token carrying `user:read:chat` + `user:write:chat`. That
moderator status is what satisfies the ChatMessageSubscription requirement
without needing a separate broadcaster-side `channel:bot` grant — see the
owner requirements section for the full acquisition checklist.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from twitchio import eventsub
from twitchio.ext import commands

from musicbot.twitch.relay import QueuedRequest, TwitchRadioRelay

if TYPE_CHECKING:
    from musicbot.cogs.music.models import Track
    from musicbot.twitch.relay import TrackResolver

log = logging.getLogger(__name__)

MAX_REQUEST_DURATION_SECONDS = 600  # 10 minutes — keep one !sr from eating the whole queue
QUEUE_CAP = 50
MAX_PENDING_PER_CHATTER = 2  # stop one viewer from flooding the whole queue


class SongRequestComponent(commands.Component):
    def __init__(self, bot: "TwitchChatBot") -> None:
        self.bot = bot
        self._pending_by_chatter: dict[str, int] = {}

    @commands.command(name="sr", aliases=["songrequest"])
    async def song_request(self, ctx: commands.Context, *, query: str = "") -> None:
        if not query.strip():
            await ctx.reply("Usage: !sr <song name or URL>")
            return

        if self.bot.relay.queue_length >= QUEUE_CAP:
            await ctx.reply("Queue's full right now — try again in a bit.")
            return

        chatter_key = ctx.chatter.id
        if self._pending_by_chatter.get(chatter_key, 0) >= MAX_PENDING_PER_CHATTER:
            await ctx.reply(
                f"You've already got {MAX_PENDING_PER_CHATTER} queued — wait for one to play first."
            )
            return

        try:
            tracks, _ = await self.bot.resolver(query, requester_id=0, limit=1)
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
        if track.duration > MAX_REQUEST_DURATION_SECONDS:
            minutes = MAX_REQUEST_DURATION_SECONDS // 60
            await ctx.reply(f"That's over the {minutes}-minute limit — pick something shorter.")
            return

        requester_name = ctx.chatter.display_name or ctx.chatter.name
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
        self._owner_id = owner_id
        self._bot_id = bot_id

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
