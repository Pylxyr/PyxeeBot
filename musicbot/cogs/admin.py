from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from musicbot.bot import MusicBot


async def _is_authorized_owner(context: commands.Context[Any]) -> bool:
    """Mirrors CommandHelpersMixin._is_bot_owner — checks settings.bot_owners
    (BOT_OWNERS env var) as well as the Discord application owner(s), rather
    than relying on commands.is_owner() which only knows about the latter."""
    bot = context.bot
    user = context.author
    if user.id in bot.settings.bot_owners:  # type: ignore[attr-defined]
        return True
    if bot.owner_id is not None and user.id == bot.owner_id:
        return True
    return bool(bot.owner_ids) and user.id in bot.owner_ids


def _bot_owner_check() -> Any:
    return commands.check(_is_authorized_owner)


class AdminCog(commands.Cog):
    def __init__(self, bot: "MusicBot") -> None:
        self.bot = bot

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
    async def ping(self, context: commands.Context[Any]) -> None:
        """Check gateway latency."""
        latency_ms = round(self.bot.latency * 1000)
        await context.send(f"Pong. `{latency_ms}ms`")

    @commands.hybrid_command(name="stay")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def stay(self, context: commands.Context[Any]) -> None:
        """Toggle 24/7 mode — bot stays connected when the queue empties."""
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
        state = "enabled" if new_value else "disabled"
        await context.send(f"24/7 mode {state}.")

    @commands.hybrid_command(name="autoplay")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def autoplay(self, context: commands.Context[Any]) -> None:
        """Toggle autoplay — queue a similar Last.fm track when the queue empties."""
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

    @commands.hybrid_command(name="stats")
    @_bot_owner_check()
    async def stats(self, context: commands.Context[Any]) -> None:
        """Show bot process stats (owner only)."""
        import platform
        import sys

        import discord as discord_module
        import yt_dlp

        music = self.bot.get_cog("MusicCog")
        active_players = len(music.players) if music else 0
        playing = sum(1 for p in music.players.values() if p.current is not None) if music else 0

        lines = [
            f"discord.py: `{discord_module.__version__}`",
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
            # ru_maxrss is KB on Linux, bytes on macOS — assume Linux (deploy target).
            lines.append(f"Peak RSS: `{rss_kb / 1024:.1f} MB`")
        except ImportError:
            pass

        embed = discord.Embed(
            title="PyxeeBot Stats",
            description="\n".join(lines),
            colour=discord.Colour.from_rgb(255, 170, 64),
        )
        await context.send(embed=embed)

    @commands.command(name="commands", aliases=["cmds"])
    async def commands_list(self, context: commands.Context[Any]) -> None:
        """Open the styled command atlas."""
        await context.send_help()

    @commands.hybrid_command(name="setprefix")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def setprefix(self, context: commands.Context[Any], prefix: str) -> None:
        """Change the bot command prefix for this server."""
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
    async def setdj(self, context: commands.Context[Any], role: discord.Role) -> None:
        """Assign the DJ role for protected controls."""
        await self.bot.database.set_dj_role_id(
            context.guild.id,
            role.id,
            default_prefix=self.bot.settings.default_prefix,
        )
        await context.send(f"DJ role set to {role.mention}.")

    @commands.hybrid_command(name="cleardj")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def cleardj(self, context: commands.Context[Any]) -> None:
        """Remove the configured DJ role."""
        await self.bot.database.set_dj_role_id(
            context.guild.id,
            None,
            default_prefix=self.bot.settings.default_prefix,
        )
        await context.send("DJ role cleared. Members with Manage Server still count as DJs.")

    @commands.hybrid_command(name="dj")
    @commands.guild_only()
    async def dj(self, context: commands.Context[Any]) -> None:
        """Show the current DJ role."""
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
