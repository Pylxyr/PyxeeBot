"""Twitch chat bot — handles !sr <query> and hands resolved requests to the radio player.

Built against twitchio 3.x's EventSub-based Bot (verified against the actual
installed twitchio==3.3.2 API by inspecting the library directly, not just
its docs — this is NOT the old IRC-token pattern from twitchio 2.x).

Auth model used here is the simplest supported one ("Installed Chatbot" style,
per Twitch's own chat bot guide): a single Twitch account (recommended: a
dedicated account named after the bot, made a moderator in your channel) with
a User Access Token carrying `user:read:chat` + `user:write:chat`. That
moderator status is what satisfies the ChatMessageSubscription requirement
without needing a separate broadcaster-side `channel:bot` grant (confirmed
against Twitch's own EventSub docs: a user-token subscription only needs
`user:read:chat` from the chatting user; `channel:bot`-or-moderator is only
required when using an app access token instead).

ONE-TIME SETUP — this part doesn't happen automatically and isn't optional:
    Neither `client_id`/`client_secret` below nor anything else in this repo
    can, by itself, obtain the User Access Token this bot needs — a
    client-credentials ("app") token has no user scopes and can't read or send
    chat as a specific account. TwitchIO 3.x handles the missing piece with a
    small built-in web server (twitchio.web.AiohttpAdapter), started
    automatically by commands.Bot when no custom adapter is supplied, that
    listens on http://localhost:4343 and persists whatever token you
    authorize through it (see load_tokens/save_tokens below for exactly
    where — verified against the real Client.load_tokens/save_tokens source,
    which take an optional `path` and default to ".tio.tokens.json" in the
    process's working directory if you don't override them, which is exactly
    why this class does). To complete it:

      1. Start the bot once with valid TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
         / TWITCH_BOT_ID / TWITCH_OWNER_ID set.
      2. The adapter binds to localhost only, so on a remote VPS you'll need
         an SSH tunnel to reach it: `ssh -L 4343:localhost:4343 <user>@<host>`
         from your own machine, kept open while you do steps 3-4.
      3. In a browser, logged in as the BOT's own Twitch account, visit:
         http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true
      4. In a browser, logged in as the BROADCASTER's account (i.e. the
         channel this bot will post in), visit:
         http://localhost:4343/oauth?scopes=channel:bot&force_verify=true
         (Optional if the bot account is already a moderator in that channel
         — see the auth-model paragraph above — but costs nothing to do
         anyway and removes the "is it still a mod" dependency.)

      Use two SEPARATE browser sessions for steps 3 and 4 (e.g. a normal
      window + a private/incognito one) — reusing the same already-logged-in
      session for both is the most common way this goes wrong: Twitch just
      authorizes whichever account is currently logged in, `force_verify`
      or not, so it's easy to end up with both tokens saved under the same
      (wrong) account without any error at all. Chat subscription then
      fails with no token on file for the *other* ID; see
      `_log_token_diagnostics` below, which checks for exactly this at
      startup.

    Once both are done, the tokens are saved to TWITCH_TOKEN_FILE (default:
    data/twitch_tokens.json) and reloaded automatically on every future
    start. You will not need to repeat this unless that file is deleted or
    Twitch revokes the token.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from twitchio import Chatter, eventsub
from twitchio.ext import commands

from twitch_radio.blocklist import (
    add_track_block,
    add_uploader_block,
    blocklist_reason,
    counts as blocklist_counts,
    normalize_track_key,
    remove_track_block,
    remove_uploader_block,
)
from twitch_radio.extraction import UnsupportedSourceError
from twitch_radio.player import QueuedRequest, RadioPlayer
from twitch_radio.store import JsonStore
from twitch_radio.tunables import TUNABLE_BOUNDS, TwitchTunables

if TYPE_CHECKING:
    from twitch_radio.extraction import Resolver

log = logging.getLogger(__name__)

# Per-command usage strings for event_command_error's MissingRequiredArgument
# handler below — !sr, !setlimit, and !block/!unblock are the commands with
# a required argument today. Keyed by command name (not alias) since
# ctx.command.name always resolves to the canonical name even when invoked
# via an alias like !songrequest.
_USAGE = {
    "sr": "Usage: !sr <song name or URL>",
    "setlimit": "Usage: !setlimit <key> <value> — keys: " + ", ".join(TUNABLE_BOUNDS),
    "block": "Usage: !block <YouTube/SoundCloud URL, or an uploader name>",
    "unblock": "Usage: !unblock <YouTube/SoundCloud URL, or an uploader name>",
}


class SongRequestComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.command(name="sr", aliases=["songrequest"])
    async def song_request(self, ctx: commands.Context, *, query: str) -> None:
        query = query.strip()
        if not query:
            await ctx.reply("Usage: !sr <song name or URL>")
            return

        chatter_key = str(ctx.chatter.id)
        tunables = TwitchTunables.from_dict(await self.bot.tunables_store.read())
        now = time.monotonic()

        # No `await` between checking limits and reserving the slot below —
        # keeps check-and-reserve atomic so rapid-fire !sr can't race past
        # the cooldown/pending/queue caps before the resolver's network call.
        last = self.bot.last_request_at.get(chatter_key, 0.0)
        if tunables.request_cooldown_seconds > 0 and (now - last) < tunables.request_cooldown_seconds:
            remaining = tunables.request_cooldown_seconds - (now - last)
            await ctx.reply(f"Slow down — try again in {remaining:.0f}s.")
            return

        pending = self.bot.pending_by_chatter.get(chatter_key, 0)
        if pending >= tunables.max_pending_per_chatter:
            await ctx.reply(f"You already have {pending} request(s) queued — wait for one to play first.")
            return

        if self.bot.player.queue_size() >= tunables.queue_cap:
            await ctx.reply("Queue's full right now — try again in a bit.")
            return

        self.bot.last_request_at[chatter_key] = now
        self.bot.pending_by_chatter[chatter_key] = pending + 1
        reserved = True

        try:
            try:
                requester_id = int(ctx.chatter.id)
            except (TypeError, ValueError):
                requester_id = 0

            # Cheap pre-resolve check for a direct link to something already
            # blocked — skips the network round-trip entirely for the common
            # case of someone re-pasting a link a mod just blocked. Doesn't
            # replace the full post-resolve check below: a search query or
            # an uploader-name block can't be caught until we know what it
            # actually resolved to.
            if normalize_track_key(query) is not None:
                blocklist_data = await self.bot.blocklist_store.read()
                reason = blocklist_reason(query, "", blocklist_data)
                if reason:
                    await ctx.reply(f"That's blocked by a moderator ({reason}).")
                    return

            try:
                track = await self.bot.resolver(query, requester_id)
            except UnsupportedSourceError as exc:
                await ctx.reply(str(exc))
                return
            except Exception:
                log.exception("Failed to resolve Twitch song request: %s", query)
                await ctx.reply("Couldn't fetch that — try a different search or link.")
                return

            if track is None:
                await ctx.reply("No results for that.")
                return

            if track.is_live:
                await ctx.reply("Can't queue a livestream — sorry!")
                return

            if 0 < tunables.max_request_duration_seconds < track.duration:
                minutes = tunables.max_request_duration_seconds // 60
                await ctx.reply(f"That's too long to queue — max is {minutes} minute(s).")
                return

            blocklist_data = await self.bot.blocklist_store.read()
            reason = blocklist_reason(track.webpage_url, track.uploader, blocklist_data)
            if reason:
                await ctx.reply(f"That's blocked by a moderator ({reason}).")
                return

            # Re-check the cap right before enqueuing (no await between this
            # check and enqueue() below, so this half is race-free too) — the
            # resolve above may have taken long enough for the queue to have
            # filled up in the meantime.
            if self.bot.player.queue_size() >= tunables.queue_cap:
                await ctx.reply("Queue's full right now — try again in a bit.")
                return

            def _on_start(key: str = chatter_key) -> None:
                remaining_pending = self.bot.pending_by_chatter.get(key, 1) - 1
                if remaining_pending <= 0:
                    self.bot.pending_by_chatter.pop(key, None)
                else:
                    self.bot.pending_by_chatter[key] = remaining_pending

            self.bot.player.enqueue(
                QueuedRequest(
                    webpage_url=track.webpage_url,
                    requester_id=requester_id,
                    requester_name=ctx.chatter.display_name or ctx.chatter.name or "a viewer",
                    title=track.title,
                    on_start=_on_start,
                )
            )
            reserved = False  # ownership of the reservation now belongs to on_start's eventual decrement
            await ctx.reply(f"Queued: {track.title} (#{self.bot.player.queue_size()} in queue)")
        finally:
            if reserved:
                remaining_pending = self.bot.pending_by_chatter.get(chatter_key, 1) - 1
                if remaining_pending <= 0:
                    self.bot.pending_by_chatter.pop(chatter_key, None)
                else:
                    self.bot.pending_by_chatter[chatter_key] = remaining_pending

    @commands.command(name="skip")
    # No @commands.is_moderator() guard — mods/broadcaster can always skip
    # (checked manually below), but a chatter can also skip their own
    # currently-playing request without mod status.
    async def skip(self, ctx: commands.Context) -> None:
        chatter = ctx.chatter
        # ctx.chatter is Chatter | PartialUser; only Chatter has .moderator.
        is_mod = isinstance(chatter, Chatter) and chatter.moderator
        if not is_mod:
            # active_requester_id (not now_playing) so a chatter can skip
            # their own song during the resolve/load window too — that can
            # take 15-20s+ (see README), and now_playing stays None the
            # whole time, so checking it alone would leave a non-mod unable
            # to skip their own request until it actually starts audibly
            # playing.
            active_id = self.bot.player.active_requester_id
            if active_id is None:
                await ctx.reply("Nothing's playing right now.")
                return
            try:
                requester_id = int(ctx.chatter.id)
            except (TypeError, ValueError):
                requester_id = -1
            if requester_id != active_id:
                await ctx.reply("You can only skip your own song — mods can skip anything.")
                return
        if self.bot.player.skip_current():
            await ctx.reply("Skipped.")
        else:
            await ctx.reply("Nothing's playing right now.")

    @commands.command(name="voteskip", aliases=["vs"])
    async def vote_skip(self, ctx: commands.Context) -> None:
        """Anyone can vote to skip whatever's currently playing (or still
        loading) — once enough unique chatters have voted (vote_skip_threshold,
        adjustable via /settings or !setlimit), it's skipped automatically.
        Votes are per-track and don't carry over to the next one."""
        if self.bot.player.active_requester_id is None:
            await ctx.reply("Nothing's playing right now.")
            return
        try:
            voter_id = int(ctx.chatter.id)
        except (TypeError, ValueError):
            await ctx.reply("Couldn't identify you — try again.")
            return
        tunables = TwitchTunables.from_dict(await self.bot.tunables_store.read())
        result = self.bot.player.register_skip_vote(voter_id, tunables.vote_skip_threshold)
        if result is None:
            await ctx.reply("Nothing's playing right now.")
            return
        skipped, count, is_new = result
        if skipped:
            await ctx.reply("Vote-skipped!")
        elif not is_new:
            await ctx.reply(f"You've already voted to skip this one ({count}/{tunables.vote_skip_threshold}).")
        else:
            needed = tunables.vote_skip_threshold - count
            await ctx.reply(f"Skip vote registered ({count}/{tunables.vote_skip_threshold}) — {needed} more needed.")

    @commands.command(name="remove", aliases=["cancel", "unqueue"])
    async def remove(self, ctx: commands.Context) -> None:
        """Lets a chatter pull their own most-recently-queued request back
        out — for requests still waiting in the queue, not the one currently
        playing (that's what !skip is for)."""
        try:
            requester_id = int(ctx.chatter.id)
        except (TypeError, ValueError):
            await ctx.reply("Couldn't identify you — try again.")
            return
        removed = self.bot.player.cancel_pending_for(requester_id)
        if removed is None:
            await ctx.reply("You don't have anything waiting in the queue.")
            return
        title = removed.title or "your request"
        await ctx.reply(f"Removed: {title}")

    @commands.command(name="position", aliases=["pos"])
    async def position(self, ctx: commands.Context) -> None:
        """Shows where the chatter's own request(s) sit in the queue."""
        try:
            requester_id = int(ctx.chatter.id)
        except (TypeError, ValueError):
            await ctx.reply("Couldn't identify you — try again.")
            return
        positions = self.bot.player.positions_for(requester_id)
        if not positions:
            if self.bot.player.active_requester_id == requester_id:
                await ctx.reply("Your song is up now!")
            else:
                await ctx.reply("You don't have anything queued.")
            return
        if len(positions) == 1:
            await ctx.reply(f"You're #{positions[0]} in the queue.")
        else:
            spots = ", ".join(f"#{p}" for p in positions)
            await ctx.reply(f"You're at {spots} in the queue.")

    @commands.command(name="queue")
    async def queue_cmd(self, ctx: commands.Context) -> None:
        size = self.bot.player.queue_size()
        await ctx.reply("Queue is empty." if size == 0 else f"{size} request(s) queued.")

    @commands.command(name="nowplaying", aliases=["np"])
    async def now_playing(self, ctx: commands.Context) -> None:
        np = self.bot.player.now_playing
        if np is None:
            await ctx.reply("Nothing's playing right now.")
            return
        elapsed = max(0, int(time.monotonic() - np.started_at))
        await ctx.reply(f"Now playing: {np.title} — requested by {np.requester_name} ({elapsed}s in)")

    @commands.command(name="commands", aliases=["help"])
    async def commands_list(self, ctx: commands.Context) -> None:
        p = self.bot.prefix
        await ctx.reply(
            f"{p}sr <song/URL> — queue a song (YouTube/SoundCloud)  |  {p}skip / {p}voteskip — "
            f"skip it  |  {p}remove — pull back your request  |  {p}position — where you are in "
            f"line  |  {p}queue  |  {p}nowplaying  |  mods: {p}setlimit, {p}block/{p}unblock"
        )

    @commands.is_moderator()
    @commands.command(name="setlimit")
    async def set_limit(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: adjust one request-limit tunable live, without needing
        the /settings page — e.g. !setlimit queue_cap 100. Same keys and
        ranges as /settings; takes effect on the very next command, no
        restart needed."""
        args = args.strip()
        parts = args.split(maxsplit=1)
        if len(parts) != 2:
            keys = ", ".join(TUNABLE_BOUNDS)
            await ctx.reply(f"Usage: !setlimit <key> <value> — keys: {keys}")
            return
        key, raw_value = parts[0].strip(), parts[1].strip()
        bounds = TUNABLE_BOUNDS.get(key)
        if bounds is None:
            keys = ", ".join(TUNABLE_BOUNDS)
            await ctx.reply(f"Unknown key {key!r} — keys: {keys}")
            return
        lo, hi = bounds
        try:
            value = int(raw_value)
        except ValueError:
            await ctx.reply(f"{key}: not a number.")
            return
        if not (lo <= value <= hi):
            await ctx.reply(f"{key}: must be between {lo} and {hi}.")
            return

        def _mutate(current: dict[str, object]) -> dict[str, object]:
            updated: dict[str, object] = dict(TwitchTunables.from_dict(current).to_dict())
            updated[key] = value
            return updated

        await self.bot.tunables_store.update(_mutate)
        log.info("%s = %s set via chat by %s (%s)", key, value, ctx.chatter.display_name, ctx.chatter.id)
        await ctx.reply(f"{key} = {value}")

    @commands.is_moderator()
    @commands.command(name="block")
    async def block(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: blocks a track (by URL) or an uploader (by name) from
        being requested again. Checked on every future !sr."""
        target = args.strip()
        if not target:
            await ctx.reply(_USAGE["block"])
            return
        key = normalize_track_key(target)

        def _mutate(current: dict[str, object]) -> dict[str, object]:
            return add_track_block(current, key) if key else add_uploader_block(current, target)

        result = await self.bot.blocklist_store.update(_mutate)
        log.info("Blocked %r via chat by %s (%s)", target, ctx.chatter.display_name, ctx.chatter.id)
        tracks, uploaders = blocklist_counts(result)
        kind = "track" if key else f"uploader {target!r}"
        await ctx.reply(f"Blocked that {kind}. ({tracks} tracks, {uploaders} uploaders blocked)")

    @commands.is_moderator()
    @commands.command(name="unblock")
    async def unblock(self, ctx: commands.Context, *, args: str) -> None:
        """Mod-only: reverses !block for a track (by URL) or uploader (by
        name)."""
        target = args.strip()
        if not target:
            await ctx.reply(_USAGE["unblock"])
            return
        key = normalize_track_key(target)

        def _mutate(current: dict[str, object]) -> dict[str, object]:
            return remove_track_block(current, key) if key else remove_uploader_block(current, target)

        result = await self.bot.blocklist_store.update(_mutate)
        log.info("Unblocked %r via chat by %s (%s)", target, ctx.chatter.display_name, ctx.chatter.id)
        tracks, uploaders = blocklist_counts(result)
        await ctx.reply(f"Unblocked. ({tracks} tracks, {uploaders} uploaders still blocked)")

    @commands.is_moderator()
    @commands.command(name="blocklist")
    async def blocklist_cmd(self, ctx: commands.Context) -> None:
        """Mod-only: shows how many tracks/uploaders are currently blocked
        (not the full list — that can get long for chat)."""
        tracks, uploaders = blocklist_counts(await self.bot.blocklist_store.read())
        await ctx.reply(f"{tracks} track(s) and {uploaders} uploader(s) currently blocked.")


class TwitchChatBot(commands.Bot):
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        bot_id: str,
        owner_id: str,
        prefix: str,
        resolver: Resolver,
        player: RadioPlayer,
        tunables_store: JsonStore,
        blocklist_store: JsonStore,
        token_storage_path: Path,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            bot_id=bot_id,
            owner_id=owner_id,
            prefix=prefix,
        )
        self.resolver = resolver.resolve
        self.player = player
        self.tunables_store = tunables_store
        self.blocklist_store = blocklist_store
        self.prefix = prefix
        self._owner_id = owner_id
        self._bot_id = bot_id
        self._token_storage_path = token_storage_path
        # Per-chatter state — deliberately in-memory only (not persisted):
        # losing cooldown/pending tracking across a restart is harmless (worst
        # case someone gets one extra request right after a restart), and
        # persisting it would add complexity for no real benefit.
        self.last_request_at: dict[str, float] = {}
        self.pending_by_chatter: Counter[str] = Counter()

    async def load_tokens(self, path: str | None = None, /) -> None:
        # Redirects TwitchIO's default token file into DATA_DIR instead.
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        await super().load_tokens(path or str(self._token_storage_path))

    async def save_tokens(self, path: str | None = None, /) -> None:
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        target = path or str(self._token_storage_path)
        await super().save_tokens(target)
        # twitchio's own save() (Client.save() -> ... -> open(name, "w+"))
        # writes this file with no explicit mode, so it inherits whatever
        # the process umask gives it — commonly 644 (world-readable) on a
        # default Ubuntu install. This file holds live OAuth access +
        # refresh tokens for both the bot and (optionally) the broadcaster
        # account, so lock it down the same way setup.sh already does for
        # .env (chmod 600) — anyone else with a login on a shared box
        # shouldn't be able to read it straight off disk. Re-applied after
        # every save since a fresh write can reset permissions depending on
        # the umask at the time.
        with contextlib.suppress(OSError):
            Path(target).chmod(0o600)

    async def setup_hook(self) -> None:
        await self.add_component(SongRequestComponent(self))
        self._log_token_diagnostics()
        subscription = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self._owner_id,
            user_id=self._bot_id,
        )
        try:
            await self.subscribe_websocket(payload=subscription)
            log.info("Subscribed to chat messages for broadcaster=%s bot=%s", self._owner_id, self._bot_id)
        except Exception as e:
            if self._token_storage_path.exists():
                log.exception(
                    "Chat subscription failed even though %s exists — chat commands won't work "
                    "until this is fixed. See the token diagnostics logged above, or redo the "
                    "OAuth steps in README.md with &force_verify=true if a token was revoked or "
                    "scopes changed. Error: %s",
                    self._token_storage_path,
                    e,
                )
            else:
                log.warning(
                    "Skipping chat subscription — no token file yet at %s (expected before the "
                    "one-time OAuth steps in README.md are done): %s",
                    self._token_storage_path,
                    e,
                )

    def _log_token_diagnostics(self) -> None:
        # Catches the single most common cause of "OAuth said success but
        # chat still doesn't work": the saved token belongs to a different
        # Twitch account than TWITCH_BOT_ID/TWITCH_OWNER_ID — easy to do by
        # accident if the same already-logged-in browser was used for both
        # the bot and broadcaster authorization steps. The OAuth flow has
        # no way to know that happened; it saves whatever account was
        # actually logged in and reports success regardless. Checked
        # directly against the token file's on-disk keys (verified against
        # twitchio's actual save/load implementation —
        # {"<user_id>": {"token": ..., "refresh": ...}, ...}) rather than
        # any private in-memory attribute.
        if not self._token_storage_path.exists():
            return
        try:
            saved_ids = set(json.loads(self._token_storage_path.read_text(encoding="utf-8")))
        except Exception as e:
            log.warning("Couldn't read %s to check saved tokens: %s", self._token_storage_path, e)
            return
        if self._bot_id not in saved_ids:
            log.error(
                "No saved token for TWITCH_BOT_ID=%s in %s (tokens on file: %s). Chat commands "
                "won't work. Redo the bot-account OAuth step — make sure the browser is actually "
                "logged into THAT account, not your broadcaster account (a private/incognito "
                "window avoids reusing whatever session is already active).",
                self._bot_id,
                self._token_storage_path,
                sorted(saved_ids) or "none",
            )
        if self._owner_id not in saved_ids:
            log.info(
                "No saved token for TWITCH_OWNER_ID=%s — fine if the bot account is already a "
                "moderator in your channel (that alone satisfies the chat subscription), "
                "otherwise redo the broadcaster-account OAuth step.",
                self._owner_id,
            )

    async def event_ready(self) -> None:
        log.info("Twitch chat bot ready (bot_id=%s).", self._bot_id)

    async def announce(self, message: str) -> None:
        """Sends a message to the broadcaster's channel — used by RadioPlayer
        to tell chat about a track it had to drop. Not tied to a command
        Context, so this goes through PartialUser.send_message directly."""
        # commands.Bot types _owner_id/_bot_id as `str | None` since the base
        # class allows constructing without them — this subclass's __init__
        # requires both (they come from load_settings()'s _required()), so
        # they're never actually None here; just narrowing for mypy.
        assert self._owner_id is not None
        assert self._bot_id is not None
        channel = self.create_partialuser(user_id=self._owner_id)
        await channel.send_message(sender=self._bot_id, message=message)

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        exc = payload.exception
        ctx = payload.context
        if isinstance(exc, commands.CommandNotFound):
            # Fires for *every* chat message that starts with our prefix but
            # isn't one of ours — which, in a channel running Nightbot/
            # StreamElements/Moobot alongside this bot (all "!"-prefixed
            # too), is most of them. Not an error worth logging at all,
            # let alone at ERROR with a traceback on every occurrence.
            return
        if isinstance(exc, commands.GuardFailure):
            with contextlib.suppress(Exception):
                await ctx.reply("You don't have permission to use that command.")
            return
        if isinstance(exc, commands.MissingRequiredArgument):
            # ctx.command.name is the canonical name even when invoked via
            # an alias (e.g. !songrequest resolves to "sr") — falls back to
            # the !sr message if for some reason ctx.command is unset,
            # since that's the far more common command to hit this.
            name = ctx.command.name if ctx.command is not None else "sr"
            usage = _USAGE.get(name, _USAGE["sr"])
            with contextlib.suppress(Exception):
                await ctx.reply(usage)
            return
        log.error("Command error in %r: %r", getattr(ctx, "content", "<unknown>"), exc, exc_info=exc)
