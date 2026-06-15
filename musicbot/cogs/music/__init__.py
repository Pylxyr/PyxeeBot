"""musicbot.cogs.music — Public surface of the music subsystem.

Import MusicCog and EMBED_COLOUR from here; internal modules are private.
"""

from musicbot.cogs.music.cog import MusicCog
from musicbot.cogs.music.constants import EMBED_COLOUR

__all__ = ["MusicCog", "EMBED_COLOUR"]
