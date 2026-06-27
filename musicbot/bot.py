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
from musicbot.cogs.music import MusicCog

from musicbot.cogs.curation import CurationCog
from musicbot.config import Settings, load_settings
from musicbot.database import Database

# Module-level set to keep strong references to fire-and-forget tasks (SIGTERM
# handler) so the event loop doesn't garbage-collect them mid-run.
_bg_tasks: set[asyncio.Task[Any]] = set()


class PyxeeHelpCommand(commands.HelpCommand):
    CATEGORY_STYLES = {
        "MusicCog": ("\N{MUSICAL NOTE}", "Playback Deck"),
        "AdminCog": ("\N{SATELLITE ANTENNA}", "Control Room"),
        "CurationCog": ("\N{SPARKLES}", "Playlist Curator"),
        None: ("\N{SPARKLES}", "Extras"),
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
        "qsearch": "Search for a keyword within the current queue.",
        "toptracks": "Show the most-played tracks for this server, all-time.",
        "toprequestors": "Show the top track requestors for this server, all-time.",
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
        return self.COMMAND_BLURBS.get(command.name, "No summary set yet.")

    def _base_embed(self, title: str, description: str) -> discord.Embed:
        bot_user = self.context.bot.user
        embed = discord.Embed(
            title=title,
            description=description,
            colour=discord.Colour.from_rgb(255, 170, 64),
        )
        if bot_user:
            embed.set_author(name="PyxeeBot Interface", icon_url=bot_user.display_avatar.url)
        if self.context.guild and self.context.guild.icon:
            embed.set_thumbnail(url=self.context.guild.icon.url)
        embed.set_footer(text="Use help <command> for focused details.")
        return embed

    def _style_for_cog(self, cog: commands.Cog | None) -> tuple[str, str]:
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
        description = (
            "Full command list. Use `help <command>` for details.\n"
            f"`{prefix}join` → `{prefix}play <song>` → `{prefix}nowplaying`"
        )
        base_embed = self._base_embed("PyxeeBot Command Atlas", description)

        FIELD_LIMIT = 1000
        EMBED_CHAR_LIMIT = 5800

        all_fields: list[tuple[str, str]] = []

        ordered_cogs = [cog for cog in self.context.bot.cogs.values() if cog in mapping]
        if None in mapping:
            ordered_cogs.append(None)

        for cog in ordered_cogs:
            commands_for_cog = await self.filter_commands(mapping.get(cog, []), sort=True)
            if not commands_for_cog:
                continue
            icon, title = self._style_for_cog(cog)
            field_name = f"{icon} {title}"
            chunk_lines: list[str] = []
            chunk_len = 0
            chunk_index = 0

            for command in commands_for_cog:
                line = self._format_command_compact(command)
                if chunk_lines and chunk_len + len(line) + 1 > FIELD_LIMIT:
                    suffix = " (cont.)" if chunk_index > 0 else ""
                    all_fields.append((f"{field_name}{suffix}", "\n".join(chunk_lines)))
                    chunk_lines = []
                    chunk_len = 0
                    chunk_index += 1
                chunk_lines.append(line)
                chunk_len += len(line) + 1

            if chunk_lines:
                suffix = " (cont.)" if chunk_index > 0 else ""
                all_fields.append((f"{field_name}{suffix}", "\n".join(chunk_lines)))

        embeds: list[discord.Embed] = [base_embed]
        current_embed = base_embed
        current_chars = len(description) + len("PyxeeBot Command Atlas")

        for field_name, field_value in all_fields:
            addition = len(field_name) + len(field_value)
            if current_chars + addition > EMBED_CHAR_LIMIT and current_embed.fields:
                current_embed = discord.Embed(colour=discord.Colour.from_rgb(255, 170, 64))
                current_embed.set_footer(text="Use help <command> for focused details.")
                embeds.append(current_embed)
                current_chars = 0
            current_embed.add_field(name=field_name, value=field_value, inline=False)
            current_chars += addition

        dest = self.get_destination()
        for embed in embeds:
            await dest.send(embed=embed)

    async def send_cog_help(self, cog: commands.Cog) -> None:
        commands_for_cog = await self.filter_commands(cog.get_commands(), sort=True)
        icon, title = self._style_for_cog(cog)
        embed = self._base_embed(
            f"{icon} {title}",
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
            f"\N{CARD INDEX DIVIDERS} {group.qualified_name}",
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
        icon, title = self._style_for_cog(cog)
        embed = self._base_embed(
            f"{icon} {command.qualified_name}",
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
        embed = self._base_embed("\N{WARNING SIGN} Help Error", error)
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

    async def setup_hook(self) -> None:
        self._shutting_down = False
        await self._populate_owner_ids()
        await self.add_cog(AdminCog(self))
        await self.add_cog(MusicCog(self))
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
                custom = self._prefix_cache[guild_id]
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
        activity = discord.Activity(
            type=discord.ActivityType.watching, name=self.settings.bot_activity_url
        )
        await self.change_presence(activity=activity)
        logging.getLogger(__name__).info(
            "Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown"
        )
        await self._maybe_announce_reconnects()

    async def _maybe_announce_reconnects(self) -> None:
        # A persisted queue snapshot existing for a guild is itself the signal
        # that we're coming back from a restart (crash, OOM, deploy) with that
        # guild's queue intact — fire on this process's first on_ready too, not
        # just later gateway reconnects, since systemd's Restart=on-failure is
        # exactly the case this is meant to surface. A per-guild cooldown stops
        # repeated on_ready calls from gateway flakiness from spamming the
        # channel, without permanently silencing a guild that had no snapshot
        # at startup but genuinely reconnects later.
        now = time.monotonic()
        for guild in self.guilds:
            # -inf sentinel, not 0.0: time.monotonic()'s absolute starting
            # point is undefined and can itself be a small number (e.g. on a
            # freshly booted container), which would make a guild that was
            # never announced to look like it's still within the cooldown.
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
            player = music_cog.players.get(guild.id) if music_cog else None  # type: ignore[union-attr]
            announce_id = player.announce_channel_id if player else None
            channel = guild.get_channel(announce_id) if isinstance(announce_id, int) else guild.system_channel
            if channel is None:
                continue
            with contextlib.suppress(discord.HTTPException):
                await channel.send(
                    "🔌 Reconnected — your queue has been preserved and will resume on `!join`."
                )

    async def on_command_error(self, context: commands.Context[Any], error: commands.CommandError) -> None:
        if hasattr(context.command, "on_error"):
            return

        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CommandOnCooldown):
            await context.send(f"Slow down — retry in `{error.retry_after:.1f}s`.", delete_after=6)
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await context.send(f"Missing argument: `{error.param.name}`.")
            return
        if isinstance(error, commands.BadArgument):
            await context.send(str(error))
            return
        if isinstance(error, commands.CheckFailure):
            await context.send("You do not have permission to use this command.")
            return

        logging.getLogger(__name__).exception("Unhandled command error", exc_info=error)
        await context.send("An unexpected error occurred. Check the logs for details.")

    async def close(self) -> None:
        self._shutting_down = True
        music_cog = self.cogs.get("MusicCog")
        if isinstance(music_cog, MusicCog):
            await music_cog.shutdown()
        await self.database.close()
        await super().close()


def configure_logging(settings: Settings) -> None:
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    handlers[0].setFormatter(formatter)

    if settings.log_to_file:
        # Use a plain FileHandler — logrotate (deploy/musicbot-logrotate) manages
        # rotation via copytruncate.  Using RotatingFileHandler here in addition
        # creates two independent rotators fighting over the same file.
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

        await bot.start(settings.token)


def run() -> None:
    asyncio.run(_async_run())
