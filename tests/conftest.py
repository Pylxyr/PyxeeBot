"""conftest.py — shared fixtures."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

from musicbot.cogs.music.models import Track
from musicbot.cogs.music.player import GuildPlayer


def make_track(
    title: str = "Test Track",
    uploader: str = "Test Artist",
    duration: int = 180,
    requester_id: int = 111,
    query: str | None = None,
    webpage_url: str | None = None,
    stream_url: str = "",
) -> Track:
    return Track(
        title=title,
        uploader=uploader,
        duration=duration,
        requester_id=requester_id,
        query=title if query is None else query,
        webpage_url=webpage_url or f"https://youtube.com/watch?v={title[:8].replace(' ', '')}",
        stream_url=stream_url,
    )


def make_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.max_queue_size = overrides.get("max_queue_size", 100)
    s.idle_timeout_seconds = overrides.get("idle_timeout_seconds", 180)
    s.empty_channel_timeout_seconds = overrides.get("empty_channel_timeout_seconds", 60)
    s.np_auto_refresh = False
    s.np_auto_refresh_interval = 30
    s.near_end_threshold_seconds = 20
    s.ytdlp_concurrent_extracts = overrides.get("ytdlp_concurrent_extracts", 1)
    s.ytdlp_curation_concurrency = overrides.get("ytdlp_curation_concurrency", 3)
    return s


def make_bot(**settings_overrides) -> MagicMock:
    bot = MagicMock()
    bot.settings = make_settings(**settings_overrides)
    bot.settings.restore_queue_on_restart = False
    bot.dispatch = MagicMock()
    bot.loop = None
    bot.user = MagicMock(id=999999)
    bot.database = MagicMock()
    bot.database.get_stay_connected = AsyncMock(return_value=False)
    bot.database.set_stay_connected = AsyncMock(return_value=None)
    bot.database.get_autoplay = AsyncMock(return_value=False)
    bot.database.set_autoplay = AsyncMock(return_value=None)
    bot.database.save_queue_snapshot = AsyncMock(return_value=None)
    bot.database.load_queue_snapshot = AsyncMock(return_value=[])
    bot.database.add_play_history = AsyncMock(return_value=None)
    return bot


def make_guild(guild_id: int = 99999) -> MagicMock:
    guild = MagicMock()
    guild.id = guild_id
    return MagicMock(id=guild_id)


@pytest.fixture
def track():
    return make_track()


@pytest.fixture
def player():
    bot = make_bot()
    guild = make_guild()
    resolver = AsyncMock(side_effect=lambda t: t)
    audio_factory = AsyncMock(return_value=MagicMock())
    validator = AsyncMock(return_value=True)
    p = GuildPlayer(bot, guild, resolver, audio_factory, validator)
    return p


@pytest.fixture
def player_small():
    bot = make_bot(max_queue_size=3)
    guild = make_guild()
    p = GuildPlayer(
        bot,
        guild,
        AsyncMock(side_effect=lambda t: t),
        AsyncMock(return_value=MagicMock()),
        AsyncMock(return_value=True),
    )
    return p
