from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from musicbot.bot import MusicBot


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


    @commands.command(name="commands", aliases=["cmds"])
    async def commands_list(self, context: commands.Context[Any]) -> None:
        """Open the styled command atlas."""
        await context.send_help()

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
