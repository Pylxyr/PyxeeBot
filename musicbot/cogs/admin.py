from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
import yt_dlp
from discord.ext import commands

from musicbot.cogs.music._context import GuildContext

if TYPE_CHECKING:
    from musicbot.bot import MusicBot

COOKIES_TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
COOKIES_MAX_BYTES = 1_048_576


async def _is_authorized_owner(context: commands.Context[Any]) -> bool:
    bot = context.bot
    user = context.author
    if user.id in bot.settings.bot_owners:
        return True
    if bot.owner_id is not None and user.id == bot.owner_id:
        return True
    return bool(bot.owner_ids) and user.id in bot.owner_ids


def _bot_owner_check() -> Any:
    return commands.check(_is_authorized_owner)


def _looks_like_netscape_cookiefile(text: str) -> bool:
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line.split("\t")) >= 7:
            return True
    return False


def _swap_cookies_file(target: Path, backup: Path, new_content: str) -> None:
    if target.exists():
        shutil.copy2(target, backup)
        with contextlib.suppress(OSError):
            backup.chmod(0o600)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, target)


def _restore_cookies_backup(target: Path, backup: Path) -> None:
    if backup.exists():
        shutil.copy2(backup, target)


class AdminCog(commands.Cog):
    def __init__(self, bot: "MusicBot") -> None:
        self.bot = bot
        self._refreshcookies_lock = asyncio.Lock()

    async def _send(
        self,
        context: commands.Context[Any],
        content: str,
        *,
        ephemeral: bool = False,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if ephemeral and context.interaction is not None:
            kwargs["ephemeral"] = True
        await context.send(content, **kwargs)

    @commands.hybrid_command(name="ping")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def ping(self, context: GuildContext) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await context.send(f"Pong. `{latency_ms}ms`")

    @commands.hybrid_command(name="stay")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def stay(self, context: GuildContext) -> None:
        guild_id = context.guild.id
        current = await self.bot.database.get_stay_connected(guild_id)
        new_value = not current
        await self.bot.database.set_stay_connected(
            guild_id, new_value, default_prefix=self.bot.settings.default_prefix
        )
        music = self.bot.get_cog("MusicCog")
        player = music.players.get(guild_id) if music else None
        if player is not None:
            player.stay_connected = new_value
            if not new_value and not player.current and not player.queue:
                player.rearm_idle_timer()
        state = "enabled" if new_value else "disabled"
        await context.send(f"24/7 mode {state}.")

    @commands.hybrid_command(name="autoplay")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def autoplay(self, context: GuildContext) -> None:
        guild_id = context.guild.id
        current = await self.bot.database.get_autoplay(guild_id)
        new_value = not current
        await self.bot.database.set_autoplay(
            guild_id, new_value, default_prefix=self.bot.settings.default_prefix
        )
        state = "enabled" if new_value else "disabled"
        message = f"Autoplay {state}."
        if new_value and not self.bot.settings.lastfm_api_key:
            message += " Note: LASTFM_API_KEY isn't set, so autoplay won't find any tracks yet."
        await context.send(message)

    @commands.hybrid_command(name="mentions")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def mentions(self, context: GuildContext) -> None:
        guild_id = context.guild.id
        current = await self.bot.database.get_show_requester_mentions(guild_id)
        new_value = not current
        await self.bot.database.set_show_requester_mentions(
            guild_id, new_value, default_prefix=self.bot.settings.default_prefix
        )
        music = self.bot.get_cog("MusicCog")
        player = music.players.get(guild_id) if music else None
        if player is not None:
            player.show_mentions = new_value
        state = "on" if new_value else "off"
        detail = (
            "requester tags will now ping the user."
            if new_value
            else "requester tags now show a display name instead of pinging."
        )
        await context.send(f"Requester mentions turned {state} — {detail}")

    @commands.hybrid_command(name="linkpreviews", aliases=["preview", "previews"])
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def linkpreviews(self, context: GuildContext) -> None:
        guild_id = context.guild.id
        current = await self.bot.database.get_show_link_previews(guild_id)
        new_value = not current
        await self.bot.database.set_show_link_previews(
            guild_id, new_value, default_prefix=self.bot.settings.default_prefix
        )
        music = self.bot.get_cog("MusicCog")
        player = music.players.get(guild_id) if music else None
        if player is not None:
            player.show_link_previews = new_value
        state = "on" if new_value else "off"
        detail = (
            "queue confirmations will show Discord's native YouTube preview."
            if new_value
            else "queue confirmations will no longer show Discord's native YouTube preview."
        )
        await context.send(f"Link previews turned {state} — {detail}")

    @commands.hybrid_command(name="stats")
    @_bot_owner_check()
    async def stats(self, context: GuildContext) -> None:
        music = self.bot.get_cog("MusicCog")
        active_players = len(music.players) if music else 0
        playing = sum(1 for p in music.players.values() if p.current is not None) if music else 0

        lines = [
            f"discord.py: `{discord.__version__}`",
            f"yt-dlp: `{yt_dlp.version.__version__}`",
            f"Python: `{platform.python_version()}` ({sys.platform})",
            f"Guilds: `{len(self.bot.guilds)}`",
            f"Active voice connections: `{active_players}`",
            f"Currently playing: `{playing}`",
            f"Gateway latency: `{round(self.bot.latency * 1000)}ms`",
        ]
        try:
            import resource

            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            lines.append(f"Peak RSS: `{rss_kb / 1024:.1f} MB`")
        except ImportError:
            pass

        embed = discord.Embed(
            title="PyxeeBot Stats",
            description="\n".join(lines),
            colour=discord.Colour.from_rgb(255, 170, 64),
        )
        await context.send(embed=embed)

    @commands.hybrid_command(name="refreshcookies")
    @commands.guild_only()
    @_bot_owner_check()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def refreshcookies(self, context: GuildContext) -> None:
        if self._refreshcookies_lock.locked():
            await self._send(
                context, "A cookie refresh is already in progress — check your DMs.", ephemeral=True
            )
            return
        async with self._refreshcookies_lock:
            await self._run_refreshcookies(context)

    async def _run_refreshcookies(self, context: GuildContext) -> None:
        cookies_path = self.bot.settings.ytdlp_cookies_file
        if cookies_path is None:
            await self._send(context, "YTDLP_COOKIES_FILE isn't configured.", ephemeral=True)
            return

        try:
            await context.author.send(
                "Reply here with the new `cookies.txt` within 5 minutes, or reply `cancel`."
            )
        except discord.Forbidden:
            await self._send(context, "I can't DM you — check your privacy settings.", ephemeral=True)
            return

        await self._send(context, "📬 Check your DMs.", ephemeral=True)

        def check(m: discord.Message) -> bool:
            return m.author.id == context.author.id and isinstance(m.channel, discord.DMChannel)

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=300)
        except TimeoutError:
            with contextlib.suppress(discord.HTTPException):
                await context.author.send("⌛ Timed out waiting for the file.")
            return

        if reply.content.strip().lower() == "cancel":
            await context.author.send("Cancelled.")
            return

        if not reply.attachments:
            await context.author.send("No file attached — run `!refreshcookies` again.")
            return

        attachment = reply.attachments[0]
        if attachment.size > COOKIES_MAX_BYTES:
            await context.author.send(
                f"That's `{attachment.size / 1024:.0f} KB` — too large to be a cookies.txt. Aborted."
            )
            return

        try:
            raw = await attachment.read()
            text = raw.decode("utf-8")
        except (discord.HTTPException, UnicodeDecodeError):
            await context.author.send("Couldn't read that file as text. Aborted.")
            return

        if not _looks_like_netscape_cookiefile(text):
            await context.author.send(
                "That doesn't look like a Netscape-format cookies.txt. Aborted, nothing changed."
            )
            return

        music = self.bot.get_cog("MusicCog")
        if music is None:
            await context.author.send("Music cog isn't loaded — aborted.")
            return

        backup_path = cookies_path.with_suffix(cookies_path.suffix + ".bak")
        try:
            await asyncio.to_thread(_swap_cookies_file, cookies_path, backup_path, text)
        except OSError as exc:
            await context.author.send(f"Failed to write the new file: `{exc}`. Nothing changed.")
            return

        music._reset_ytdl_options()
        await context.author.send("🔄 Swapped — running a live test...")

        error = ""
        try:
            tracks, _ = await music._extract_tracks(
                COOKIES_TEST_URL, requester_id=context.author.id, guild_id=None, limit=1
            )
            ok = bool(tracks and tracks[0].stream_url)
            if not ok:
                error = "extraction returned no playable stream."
        except Exception as exc:
            ok = False
            error = str(exc)

        if ok:
            await context.author.send("✅ Cookies replaced and verified working.")
            return

        with contextlib.suppress(OSError):
            await asyncio.to_thread(_restore_cookies_backup, cookies_path, backup_path)
        music._reset_ytdl_options()
        await context.author.send(
            f"❌ New cookies failed a live test (`{error}`) — rolled back to the previous file."
        )

    @commands.command(name="commands", aliases=["cmds"])
    async def commands_list(self, context: GuildContext) -> None:
        await context.send_help()

    @commands.hybrid_command(name="setprefix")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def setprefix(self, context: GuildContext, prefix: str) -> None:
        prefix = prefix.strip()
        if not prefix or " " in prefix:
            await context.send("Prefix must be a single token with no spaces.")
            return
        if len(prefix) > 5:
            await context.send("Prefix must be 5 characters or fewer.")
            return
        await self.bot.database.set_prefix(context.guild.id, prefix)
        self.bot.invalidate_prefix_cache(context.guild.id)
        await context.send(f"Prefix set to `{prefix}` for this server.")

    @commands.hybrid_command(name="setdj")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def setdj(self, context: GuildContext, role: discord.Role) -> None:
        await self.bot.database.set_dj_role_id(
            context.guild.id,
            role.id,
            default_prefix=self.bot.settings.default_prefix,
        )
        await context.send(f"DJ role set to {role.mention}.")

    @commands.hybrid_command(name="cleardj")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def cleardj(self, context: GuildContext) -> None:
        await self.bot.database.set_dj_role_id(
            context.guild.id,
            None,
            default_prefix=self.bot.settings.default_prefix,
        )
        await context.send("DJ role cleared. Members with Manage Server still count as DJs.")

    @commands.hybrid_command(name="dj")
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def dj(self, context: GuildContext) -> None:
        role_id = await self.bot.database.get_dj_role_id(context.guild.id)
        if not role_id:
            await self._send(
                context,
                "No DJ role is configured. Members with Manage Server are treated as DJs.",
                ephemeral=True,
            )
            return
        role = context.guild.get_role(role_id)
        if role is None:
            await self._send(
                context,
                "The saved DJ role no longer exists. Run `setdj` again.",
                ephemeral=True,
            )
            return
        await self._send(context, f"Current DJ role: {role.mention}", ephemeral=True)
