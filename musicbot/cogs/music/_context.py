from __future__ import annotations

from contextvars import ContextVar
from typing import Any

import discord
from discord.ext import commands

_CURRENT_GUILD_ID: ContextVar[int | None] = ContextVar("_CURRENT_GUILD_ID", default=None)


class GuildContext(commands.Context[Any]):
    """Narrowed Context for guild-only commands."""

    guild: discord.Guild
    author: discord.Member
