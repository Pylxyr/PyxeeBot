from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from typing import Any

import discord
from discord.ext import commands

from musicbot.cogs.admin import AdminCog
from musicbot.cogs.curation import CurationCog
from musicbot.cogs.music import MusicCog
from musicbot.cogs.music.constants import EMBED_COLOUR
from musicbot.config import Settings, load_settings
from musicbot.database import Database

_bg_tasks: set[asyncio.Task[Any]] = set()

HELP_VIEW_TIMEOUT_SECONDS = 180


class HelpOverviewView(discord.ui.View):
    """Category picker for !commands / !help — one short overview, browse the rest on demand.

    Everything needed to render is precomputed once in send_bot_help and handed in,
    so picking a category is just an in-place embed swap on the same message —
    no re-filtering commands per interaction.
    """

    def __init__(
        self,
        *,
        categories: list[tuple[str, list[str]]],
        overview_embed: discord.Embed,
        colour: discord.Colour,
        total_commands: int,
    ) -> None:
        super().__init__(timeout=HELP_VIEW_TIMEOUT_SECONDS)
        self.categories = categories
        self.overview_embed = overview_embed
        self.colour = colour
        self.total_commands = total_commands
        self.message: discord.Message | None = None
        self._build_select()

    def _build_select(self) -> None:
        options = [
            discord.SelectOption(label="Overview", description="Back to the category list", value="overview")
        ]
        for i, (title, lines) in enumerate(self.categories):
            options.append(
                discord.SelectOption(
                    label=title,
                    description=f"{len(lines)} command{'s' if len(lines) != 1 else ''}",
                    value=str(i),
                )
            )
        select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder="Browse a category…",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    def _category_embed(self, index: int) -> discord.Embed:
        title, lines = self.categories[index]
        embed = discord.Embed(
            title=title,
            description="\n".join(lines)[:4000],
            colour=self.colour,
        )
        embed.set_footer(
            text=f"{len(lines)} command{'s' if len(lines) != 1 else ''} · "
            "Use help <command> for full details on any one"
        )
        return embed

    async def _on_select(self, interaction: discord.Interaction) -> None:
        value = interaction.data.get("values", ["overview"])[0]
        embed = self.overview_embed if value == "overview" else self._category_embed(int(value))
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)


class PyxeeHelpCommand(commands.HelpCommand):
    CATEGORY_STYLES = {
        "MusicCog": "Playback Deck",
        "AdminCog": "Control Room",
        "CurationCog": "Playlist Curator",
        None: "Extras",
    }
    COMMAND_BLURBS = {
        "setprefix": "Change the bot command prefix for this server.",
        "stay": "Toggle 24/7 mode — bot stays connected when the queue empties.",
        "autoplay": "Toggle autoplay — queue a similar Last.fm track when the queue empties.",
        "stats": "Show bot process stats (owner only).",
        "play": "Queue a URL, playlist, or search. Uses YouTube Music for best accuracy.",
        "playnext": "Insert a track next in queue. Plain text uses YouTube Music direct.",
        "search": "Browse results and pick one to queue. Use when !play gets the wrong track.",
        "join": "Dock into your current voice channel.",
        "leave": "Disconnect and wipe the active session.",
        "pause": "Freeze playback in place.",
        "resume": "Resume the paused track.",
        "skip": "Vote-skip or instantly skip if you have control.",
        "prev": "Jump back to the last completed track.",
        "stop": "Stop playback and drop loop mode.",
        "queue": "Inspect the current track stack.",
        "nowplaying": "Open the live control panel with buttons.",
        "remove": "Pull one queued track by index.",
        "clear": "Flush the queued tracks.",
        "shuffle": "Randomize the upcoming queue.",
        "loop": "Cycle loop: off → single track → full queue → off.",
        "playlist": "Work with saved server playlists.",
        "setdj": "Assign the DJ role for protected controls.",
        "cleardj": "Remove the configured DJ role.",
        "dj": "Show the current DJ role.",
        "ping": "Check gateway latency.",
        "commands": "Open the styled command atlas.",
        "help": "Show command details or category overviews.",
        "forceskip": "DJ-only immediate skip.",
        "move": "Move a track from one queue position to another.",
        "history": "Show the last tracks played this session.",
        "skipto": "Jump to a specific queue position, dropping tracks before it.",
        "replay": "Re-queue the current track to play again next.",
        "toptracks": "Show the most-played tracks for this server, all-time.",
        "toprequestors": "Show the top track requestors for this server, all-time.",
        "repeat": "Toggle single-track repeat for the current track.",
        "vibe": "Discover similar songs via Last.fm and curate a playlist.",
        "vibe-save": "Save the active curation session as a named playlist.",
        "vibe-load": "Load a saved curated playlist into the queue.",
        "playlist save": "Save the current queue as a named server playlist.",
        "playlist load": "Queue every track from a saved playlist.",
        "playlist list": "List all saved playlists for this server.",
        "playlist show": "Show the tracks in a saved playlist.",
        "playlist delete": "Delete a saved playlist.",
        "mentions": "Toggle whether requester tags ping the user or just show their name.",
        "linkpreviews": "Toggle Discord's native YouTube preview on queue confirmations.",
        "refreshcookies": "Owner-only: replace cookies.txt via DM upload, with auto-rollback.",
    }

    def get_command_signature(self, command: commands.Command[Any, ..., Any]) -> str:
        prefix = self.context.clean_prefix
        signature = f"{prefix}{command.qualified_name}"
        if command.signature:
            signature = f"{signature} {command.signature}"
        return signature

    def _blurb_for(self, command: commands.Command[Any, ..., Any]) -> str:
        if command.help:
            return command.help.strip().splitlines()[0]
        blurb = self.COMMAND_BLURBS.get(command.qualified_name) or self.COMMAND_BLURBS.get(command.name)
        return blurb or "No summary set yet."

    def _base_embed(self, title: str, description: str) -> discord.Embed:
        bot_user = self.context.bot.user
        embed = discord.Embed(
            title=title,
            description=description,
            colour=EMBED_COLOUR,
        )
        if bot_user:
            embed.set_author(name="PyxeeBot Interface", icon_url=bot_user.display_avatar.url)
        if self.context.guild and self.context.guild.icon:
            embed.set_thumbnail(url=self.context.guild.icon.url)
        embed.set_footer(text="Use help <command> for focused details.")
        return embed

    def _style_for_cog(self, cog: commands.Cog | None) -> str:
        key = cog.__class__.__name__ if cog else None
        return self.CATEGORY_STYLES.get(key, self.CATEGORY_STYLES[None])

    def _format_command_line(self, command: commands.Command[Any, ..., Any]) -> str:
        return f"`{self.get_command_signature(command)}`\n{self._blurb_for(command)}"

    def _format_command_compact(self, command: commands.Command[Any, ..., Any]) -> str:
        sig = self.get_command_signature(command)
        blurb = self._blurb_for(command)
        return f"`{sig}` — {blurb}"

    async def send_bot_help(
        self, mapping: dict[commands.Cog | None, list[commands.Command[Any, ..., Any]]]
    ) -> None:
        prefix = self.context.clean_prefix

        ordered_cogs: list[commands.Cog | None] = [
            cog for cog in self.context.bot.cogs.values() if cog in mapping
        ]
        if None in mapping:
            ordered_cogs.append(None)

        categories: list[tuple[str, list[str]]] = []
        for cog in ordered_cogs:
            commands_for_cog = await self.filter_commands(mapping.get(cog, []), sort=True)
            if not commands_for_cog:
                continue
            title = self._style_for_cog(cog)
            lines = [self._format_command_compact(command) for command in commands_for_cog]
            categories.append((title, lines))

        total_commands = sum(len(lines) for _, lines in categories)

        description = (
            "Everything I can do, sorted into categories below. Pick one from the "
            "dropdown to see its commands — this stays out of your way until you ask.\n\n"
            f"**Quick start**\n`{prefix}join` → `{prefix}play <song>` → `{prefix}nowplaying`"
        )
        overview_embed = self._base_embed("PyxeeBot Command Atlas", description)
        bot_user = self.context.bot.user
        if bot_user:
            overview_embed.set_thumbnail(url=bot_user.display_avatar.url)
        for title, lines in categories:
            overview_embed.add_field(
                name=title,
                value=f"{len(lines)} command{'s' if len(lines) != 1 else ''}",
                inline=True,
            )
        overview_embed.set_footer(
            text=f"{total_commands} commands across {len(categories)} categories · "
            "Use the dropdown to browse, or help <command> for full details"
        )

        view = HelpOverviewView(
            categories=categories,
            overview_embed=overview_embed,
            colour=EMBED_COLOUR,
            total_commands=total_commands,
        )
        message = await self.get_destination().send(embed=overview_embed, view=view)
        view.message = message

    async def send_cog_help(self, cog: commands.Cog) -> None:
        commands_for_cog = await self.filter_commands(cog.get_commands(), sort=True)
        title = self._style_for_cog(cog)
        embed = self._base_embed(
            title,
            f"Focused view for `{cog.qualified_name}` commands.",
        )
        for command in commands_for_cog:
            embed.add_field(
                name=command.qualified_name,
                value=self._format_command_line(command),
                inline=False,
            )
        await self.get_destination().send(embed=embed)

    async def send_group_help(self, group: commands.Group[Any, ..., Any]) -> None:
        embed = self._base_embed(
            group.qualified_name,
            self._blurb_for(group),
        )
        embed.add_field(name="Usage", value=f"`{self.get_command_signature(group)}`", inline=False)
        aliases = ", ".join(f"`{alias}`" for alias in group.aliases) if group.aliases else "None"
        embed.add_field(name="Aliases", value=aliases, inline=True)
        if group.commands:
            filtered = await self.filter_commands(group.commands, sort=True)
            lines = [self._format_command_line(command) for command in filtered]
            embed.add_field(name="Subcommands", value="\n\n".join(lines), inline=False)
        await self.get_destination().send(embed=embed)

    async def send_command_help(self, command: commands.Command[Any, ..., Any]) -> None:
        cog = command.cog
        title = self._style_for_cog(cog)
        embed = self._base_embed(
            command.qualified_name,
            self._blurb_for(command),
        )
        embed.add_field(name="Usage", value=f"`{self.get_command_signature(command)}`", inline=False)
        aliases = ", ".join(f"`{alias}`" for alias in command.aliases) if command.aliases else "None"
        embed.add_field(name="Aliases", value=aliases, inline=True)
        embed.add_field(name="Category", value=title, inline=True)
        if isinstance(command, commands.Group) and command.commands:
            filtered = await self.filter_commands(command.commands, sort=True)
            embed.add_field(
                name="Subcommands",
                value="\n".join(f"`{subcommand.name}`" for subcommand in filtered),
                inline=False,
            )
        await self.get_destination().send(embed=embed)

    async def send_error_message(self, error: str, /) -> None:
        embed = self._base_embed("Help Error", error)
        await self.get_destination().send(embed=embed)


class MusicBot(commands.Bot):
    def __init__(self, settings: Settings, database: Database) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.voice_states = True
        intents.members = False

        super().__init__(
            command_prefix=self._resolve_prefix,
            intents=intents,
            max_messages=256,
            help_command=PyxeeHelpCommand(),
            case_insensitive=True,
        )
        self.settings = settings
        self.database = database
        self._prefix_cache: dict[int, str] = {}
        self._reconnect_announced_at: dict[int, float] = {}
        self._shutting_down = False

    async def setup_hook(self) -> None:
        await self._populate_owner_ids()
        await self.add_cog(AdminCog(self))
        await self.add_cog(MusicCog(self))
        # !vibe / !vibe-save / !vibe-load only do anything with a Last.fm key configured
        # (curation.py no-ops every API call without one — see CurationCog._lastfm).
        # Skip registering the cog entirely when there's no key, so those commands don't
        # clutter !commands with things that can't work yet. Everything that touches this
        # cog elsewhere (bot.close(), _lifecycle._cancel_curation_activity) already treats
        # it as optional via get_cog(...) is None checks, so this is safe to skip.
        if self.settings.lastfm_api_key:
            await self.add_cog(CurationCog(self))

    async def _populate_owner_ids(self) -> None:
        if not self.owner_id and not self.owner_ids:
            try:
                app_info = await self.application_info()
            except discord.HTTPException as exc:
                logging.getLogger(__name__).warning(
                    "Could not fetch application info (%s); owner checks will rely on BOT_OWNERS only.", exc
                )
                return
            if app_info.team:
                self.owner_ids = {
                    member.id
                    for member in app_info.team.members
                    if member.role in (discord.TeamMemberRole.admin, discord.TeamMemberRole.developer)
                }
            else:
                self.owner_id = app_info.owner.id

    async def _resolve_prefix(self, _: commands.Bot, message: discord.Message) -> list[str]:
        prefixes = [self.settings.default_prefix]
        if message.guild:
            guild_id = message.guild.id
            if guild_id in self._prefix_cache:
                custom: str | None = self._prefix_cache[guild_id]
            else:
                custom = await self.database.get_prefix(guild_id)
                self._prefix_cache[guild_id] = custom or ""
            if custom and custom not in prefixes:
                prefixes.insert(0, custom)
        return commands.when_mentioned_or(*prefixes)(self, message)

    async def get_active_prefix(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return self.settings.default_prefix
        cached = self._prefix_cache.get(guild.id)
        if cached is not None:
            return cached or self.settings.default_prefix
        return await self.database.get_prefix(guild.id) or self.settings.default_prefix

    def invalidate_prefix_cache(self, guild_id: int) -> None:
        self._prefix_cache.pop(guild_id, None)

    async def on_ready(self) -> None:
        activity = discord.Activity(type=discord.ActivityType.watching, name=self.settings.bot_activity_url)
        await self.change_presence(activity=activity)
        logging.getLogger(__name__).info(
            "Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown"
        )
        await self._maybe_announce_reconnects()

    async def _maybe_announce_reconnects(self) -> None:
        now = time.monotonic()
        for guild in self.guilds:
            last = self._reconnect_announced_at.get(guild.id, float("-inf"))
            if now - last < 60.0:
                continue
            try:
                rows = await self.database.load_queue_snapshot(guild.id)
            except Exception:
                continue
            if not rows:
                continue
            self._reconnect_announced_at[guild.id] = now
            music_cog = self.cogs.get("MusicCog")
            player = music_cog.players.get(guild.id) if music_cog else None
            if player is not None and player.voice_client is not None and player.voice_client.is_connected():
                continue
            announce_id = player.announce_channel_id if player else None
            channel = guild.get_channel(announce_id) if isinstance(announce_id, int) else guild.system_channel
            if channel is None or not isinstance(channel, discord.abc.Messageable):
                continue
            with contextlib.suppress(discord.HTTPException):
                await channel.send(
                    "🔌 Reconnected — your queue has been preserved and will resume on `!join`."
                )

    async def on_command_error(self, context: commands.Context[Any], error: commands.CommandError) -> None:
        if hasattr(context.command, "on_error"):
            return
        if self._shutting_down:
            return

        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CommandOnCooldown):
            await context.send(f"Slow down — retry in `{error.retry_after:.1f}s`.", delete_after=6)
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await context.send(f"Missing argument: `{error.param.name}`.", delete_after=8)
            return
        if isinstance(error, commands.BadArgument):
            await context.send(str(error), delete_after=8)
            return
        if isinstance(error, commands.CheckFailure):
            await context.send("You do not have permission to use this command.", delete_after=8)
            return

        logging.getLogger(__name__).exception("Unhandled command error", exc_info=error)
        await context.send("An unexpected error occurred. Check the logs for details.")

    async def close(self) -> None:
        self._shutting_down = True
        music_cog = self.cogs.get("MusicCog")
        if isinstance(music_cog, MusicCog):
            await music_cog.shutdown()
        curation_cog = self.cogs.get("CurationCog")
        if isinstance(curation_cog, CurationCog):
            await curation_cog.shutdown()
        await self.database.close()
        await super().close()


def configure_logging(settings: Settings) -> None:
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    handlers[0].setFormatter(formatter)

    if settings.log_to_file:
        file_handler = logging.FileHandler(
            settings.log_dir / "musicbot.log",
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
    for handler in handlers:
        root_logger.addHandler(handler)

    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)


async def _run_twitch_integration(bot: MusicBot, settings: Settings) -> None:
    """Only scheduled at all when settings.twitch_enabled — see run() below. Waits
    for the Discord side to finish its own setup_hook (which is where MusicCog
    gets added) before reaching into it, since this needs the exact same
    extraction pipeline the Discord commands use rather than a second one."""
    from musicbot.twitch.admin_server import run_admin_server
    from musicbot.twitch.chatbot import TwitchChatBot
    from musicbot.twitch.relay import TwitchRadioRelay
    from musicbot.twitch.tunables import TwitchTunables

    await bot.wait_until_ready()
    music_cog = bot.get_cog("MusicCog")
    if music_cog is None:
        logging.getLogger(__name__).error(
            "Twitch integration enabled but MusicCog isn't loaded — skipping."
        )
        return

    tunables = TwitchTunables.from_dict(await bot.database.get_twitch_tunables())

    relay = TwitchRadioRelay(
        ingest_url=settings.twitch_ingest_url,
        stream_key=settings.twitch_stream_key,  # type: ignore[arg-type]  # guarded by twitch_enabled
        background_image=settings.twitch_background_image,
        resolver=music_cog._extract_tracks,
        video_bitrate_kbps=settings.twitch_video_bitrate_kbps,
        video_fps=settings.twitch_video_fps,
    )
    relay.start()

    admin_runner = await run_admin_server(
        relay=relay,
        tunables=tunables,
        database=bot.database,
        settings_password=settings.twitch_settings_password,
        broadcast_info={
            "Ingest URL": settings.twitch_ingest_url,
            "Video bitrate": f"{settings.twitch_video_bitrate_kbps} kbps",
            "Video framerate": f"{settings.twitch_video_fps} fps",
            "Background image": str(settings.twitch_background_image),
            "Chat command prefix": settings.twitch_prefix,
        },
        host=settings.twitch_nowplaying_host,
        port=settings.twitch_nowplaying_port,
    )

    twitch_bot = TwitchChatBot(
        client_id=settings.twitch_client_id,  # type: ignore[arg-type]
        client_secret=settings.twitch_client_secret,  # type: ignore[arg-type]
        bot_id=settings.twitch_bot_id,  # type: ignore[arg-type]
        owner_id=settings.twitch_owner_id,  # type: ignore[arg-type]
        prefix=settings.twitch_prefix,
        resolver=music_cog._extract_tracks,
        relay=relay,
        tunables=tunables,
    )
    try:
        async with twitch_bot:
            await twitch_bot.start()
    finally:
        await relay.stop()
        await admin_runner.cleanup()


async def _run_twitch_integration_guarded(bot: MusicBot, settings: Settings) -> None:
    """Wraps _run_twitch_integration with its own retry-with-backoff and, more
    importantly, makes sure nothing it raises ever reaches the asyncio.gather in
    _async_run. An unhandled exception in a gathered task cancels every other
    task in that gather by default — confirmed with a standalone repro before
    writing this fix — so without this wrapper, a bad Twitch credential or a
    transient Twitch API failure would silently take the Discord bot down too.
    That defeats the entire point of keeping Twitch's fault domain separate
    from Discord's despite sharing one process."""
    log = logging.getLogger(__name__)
    backoff = 5.0
    while True:
        try:
            await _run_twitch_integration(bot, settings)
            return  # clean return: either MusicCog was missing (already logged,
            # not transient, don't retry) or twitch_bot.start() ended on its own
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Twitch integration failed — retrying in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)


async def _async_run() -> None:
    settings = load_settings()
    configure_logging(settings)
    database = Database(settings.db_path)
    await database.initialize()
    async with MusicBot(settings=settings, database=database) as bot:
        loop = asyncio.get_running_loop()

        def _handle_sigterm() -> None:
            logging.getLogger(__name__).info("SIGTERM received — initiating graceful shutdown.")
            task = asyncio.create_task(bot.close())
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)

        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)

        tasks = [asyncio.create_task(bot.start(settings.token), name="discord-bot")]
        if settings.twitch_enabled:
            tasks.append(
                asyncio.create_task(
                    _run_twitch_integration_guarded(bot, settings), name="twitch-integration"
                )
            )
        else:
            logging.getLogger(__name__).info(
                "Twitch integration not configured (TWITCH_STREAM_KEY unset) — skipping."
            )
        await asyncio.gather(*tasks)


def run() -> None:
    asyncio.run(_async_run())
