"""_context.py — Shared context variable for the music subsystem.

Kept in its own module so that _extraction.py, _resolver.py, and cog.py
can all import it without creating circular dependencies.
"""

from __future__ import annotations

from contextvars import ContextVar

# Set to the current guild ID before any yt-dlp extraction call so that
# per-guild semaphores and debug records can find the right bucket.
_CURRENT_GUILD_ID: ContextVar[int | None] = ContextVar("_CURRENT_GUILD_ID", default=None)
