from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from musicbot.cogs.music._lifecycle import LifecycleMixin
from musicbot.cogs.music.models import Track

from tests.conftest import make_bot, make_guild, make_track


class _FakeCog(LifecycleMixin):
    def __init__(self, *, restore: bool = True) -> None:
        self.bot = make_bot()
        self.bot.settings.restore_queue_on_restart = restore
        self.players: dict[int, MagicMock] = {}
        self._restored_guilds: set[int] = set()
        self._player_create_locks: dict = {}
        self._guild_extract_semaphores: dict = {}
        self._curation_semaphores: dict = {}
        self._snapshot_tasks: dict = {}
        self._np_refresh_tasks: dict = {}
        self._pipeline_tasks: dict = {}
        self._snapshot_deadlines: dict = {}
        self.now_playing_messages: dict = {}

    def _bg_task(self, coro, *, name: str = "") -> MagicMock:
        import asyncio

        task = asyncio.create_task(coro, name=name)
        return task

    def _resolve_track(self, track: Track) -> Track:
        return track

    async def _build_audio_source(self, track: Track) -> MagicMock:
        return MagicMock()

    async def _validate_stream_url(self, track: Track) -> bool:
        return True


def _make_player(guild_id: int = 1) -> MagicMock:
    player = MagicMock()
    player.guild = make_guild(guild_id)
    player.queue = []
    player.stay_connected = False
    player.replace_queue = MagicMock(side_effect=lambda tracks: player.queue.__init__())
    return player


def _row(title: str = "T", url: str = "https://yt.be/x", query: str = "q", requester: int = 1) -> MagicMock:
    r = MagicMock()
    r.__getitem__ = lambda self, k: {
        "title": title,
        "webpage_url": url,
        "query": query,
        "requester_id": str(requester),
    }[k]
    return r


@pytest.mark.asyncio
async def test_restore_snapshot_empty_rows_queues_nothing():
    cog = _FakeCog(restore=True)
    player = _make_player()
    cog.bot.database.load_queue_snapshot = AsyncMock(return_value=[])

    await cog._restore_snapshot(player)

    player.replace_queue.assert_not_called()
    assert player.guild.id not in cog._restored_guilds


@pytest.mark.asyncio
async def test_restore_snapshot_disabled_clears_and_returns():
    cog = _FakeCog(restore=False)
    player = _make_player()
    cog.bot.database.save_queue_snapshot = AsyncMock()
    cog.bot.database.load_queue_snapshot = AsyncMock(return_value=[_row()])

    await cog._restore_snapshot(player)

    cog.bot.database.save_queue_snapshot.assert_called_once_with(player.guild.id, [])
    player.replace_queue.assert_not_called()


@pytest.mark.asyncio
async def test_restore_snapshot_rows_populates_queue():
    cog = _FakeCog(restore=True)
    player = _make_player()
    rows = [_row("Track A"), _row("Track B")]
    cog.bot.database.load_queue_snapshot = AsyncMock(return_value=rows)

    with patch.object(cog, "_warmup_restore", new_callable=AsyncMock):
        await cog._restore_snapshot(player)

    player.replace_queue.assert_called_once()
    restored_tracks = player.replace_queue.call_args[0][0]
    assert len(restored_tracks) == 2
    assert restored_tracks[0].title == "Track A"
    assert player.guild.id in cog._restored_guilds


@pytest.mark.asyncio
async def test_restore_snapshot_skips_when_queue_already_populated():
    cog = _FakeCog(restore=True)
    player = _make_player()
    player.queue = [make_track()]
    cog.bot.database.load_queue_snapshot = AsyncMock(return_value=[_row()])

    await cog._restore_snapshot(player)

    player.replace_queue.assert_not_called()


@pytest.mark.asyncio
async def test_restore_snapshot_restored_tracks_have_empty_stream_url():
    cog = _FakeCog(restore=True)
    player = _make_player()
    cog.bot.database.load_queue_snapshot = AsyncMock(return_value=[_row("Song", "https://yt.be/abc")])

    with patch.object(cog, "_warmup_restore", new_callable=AsyncMock):
        await cog._restore_snapshot(player)

    track = player.replace_queue.call_args[0][0][0]
    assert track.stream_url == ""
    assert track.webpage_url == "https://yt.be/abc"
