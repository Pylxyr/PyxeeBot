"""test_concurrency.py — regression tests for the three concurrency bugs found in review:

1. _get_player TOCTOU race (duplicate GuildPlayer per guild)
2. Database multi-statement transactions racing on the shared connection
3. _resolve_track_data swallowing CancelledError instead of propagating it
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from musicbot.cogs.music.cog import MusicCog
from musicbot.cogs.music._resolver import ResolverMixin
from musicbot.cogs.music.player import GuildPlayer
from musicbot.database import Database
from tests.conftest import make_bot, make_guild, make_track


# ── _get_player race condition ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_player_race_condition_returns_same_instance():
    bot = make_bot()
    cog = MusicCog(bot)
    guild = make_guild()

    call_count = 0

    async def slow_create(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # Yield control here to open the same race window the original bug hit.
        await asyncio.sleep(0)
        return MagicMock(name=f"player-{call_count}")

    with patch.object(GuildPlayer, "create", AsyncMock(side_effect=slow_create)):
        first, second = await asyncio.gather(
            cog._get_player(guild),
            cog._get_player(guild),
        )

    assert call_count == 1, "GuildPlayer.create should only run once per guild"
    assert first is second
    assert cog.players[guild.id] is first


@pytest.mark.asyncio
async def test_get_player_reuses_existing_player_without_lock():
    bot = make_bot()
    cog = MusicCog(bot)
    guild = make_guild()
    existing = MagicMock()
    cog.players[guild.id] = existing

    with patch.object(GuildPlayer, "create", AsyncMock()) as create_mock:
        result = await cog._get_player(guild)

    create_mock.assert_not_awaited()
    assert result is existing


@pytest.mark.asyncio
async def test_single_statement_write_blocks_behind_open_transaction(tmp_path):
    """A single-statement write (e.g. add_play_history) must not be able to land
    inside, and prematurely commit, another guild's still-open multi-statement
    transaction on the shared connection."""
    db = Database(tmp_path / "test3.db")
    await db.initialize()
    try:
        async with db._write_lock:
            await db._conn.execute("BEGIN IMMEDIATE")
            await db._conn.execute(
                "INSERT INTO queue_snapshots "
                "(guild_id, position, query, title, webpage_url, requester_id) "
                "VALUES (1, 0, 'q', 't', 'https://x', 1)"
            )
            write_task = asyncio.create_task(db.add_play_history(2, "T", "https://y", 2))
            await asyncio.sleep(0.05)
            assert not write_task.done(), (
                "add_play_history must block behind the held write lock, "
                "not interleave into guild A's open transaction"
            )
            # Guild A aborts — nothing it wrote, and nothing that leaked in
            # from elsewhere, should persist.
            await db._conn.rollback()

        await write_task

        assert await db.load_queue_snapshot(1) == []
        rows = await db.get_top_played(2)
        assert len(rows) == 1, "guild B's write should commit cleanly once the lock releases"
    finally:
        await db.close()


# ── Database concurrent transactional writes ────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_queue_snapshot_writes_do_not_race(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.initialize()
    try:
        entries_by_guild = {
            guild_id: [
                {
                    "query": f"track {i}",
                    "title": f"Track {i}",
                    "webpage_url": f"https://youtube.com/watch?v={guild_id}-{i}",
                    "requester_id": 1,
                }
                for i in range(5)
            ]
            for guild_id in range(20)
        }

        # 20 guilds writing concurrently used to be enough to trigger
        # "cannot start a transaction within a transaction" on the shared connection.
        await asyncio.gather(
            *(db.save_queue_snapshot(guild_id, entries) for guild_id, entries in entries_by_guild.items())
        )

        for guild_id, entries in entries_by_guild.items():
            rows = await db.load_queue_snapshot(guild_id)
            assert len(rows) == len(entries)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_playlist_saves_do_not_race(tmp_path):
    db = Database(tmp_path / "test2.db")
    await db.initialize()
    try:
        entries = [{"query": "q", "title": "t", "webpage_url": "https://youtube.com/watch?v=x"}]
        await asyncio.gather(*(db.save_playlist(1, f"playlist-{i}", 1, entries) for i in range(10)))
    finally:
        await db.close()


# ── Resolver CancelledError propagation ─────────────────────────────────────


class _ResolverHarness(ResolverMixin):
    """Minimal stand-in exposing only what _resolve_track_data needs."""

    def __init__(self, extractor):
        self.resolve_cache: OrderedDict = OrderedDict()
        self.resolve_tasks: dict = {}
        self.logger = logging.getLogger("test")
        self.bot = MagicMock()
        self.bot.settings.ytdlp_resolve_cache_ttl_seconds = 1800
        self.bot.settings.ytdlp_resolve_cache_size = 128
        self._extract_full_tracks = extractor


@pytest.mark.asyncio
async def test_resolve_track_data_propagates_cancellation():
    async def slow_extract(*args, **kwargs):
        await asyncio.sleep(10)
        return ([], None)  # never reached

    harness = _ResolverHarness(AsyncMock(side_effect=slow_extract))
    track = make_track()

    outer = asyncio.create_task(harness._resolve_track_data(track))
    await asyncio.sleep(0)  # let it register the pending resolve task
    outer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await outer


@pytest.mark.asyncio
async def test_resolve_track_data_cancellation_does_not_kill_shared_task():
    """A second concurrent awaiter on the same pending resolve must still get
    its result even if the first awaiter is cancelled (asyncio.shield contract)."""

    async def slow_extract(*args, **kwargs):
        await asyncio.sleep(0.05)
        resolved = make_track(title="Resolved")
        resolved.stream_url = "https://stream.example/x"
        return ([resolved], None)

    harness = _ResolverHarness(AsyncMock(side_effect=slow_extract))
    track = make_track()

    first = asyncio.create_task(harness._resolve_track_data(track))
    await asyncio.sleep(0)
    second = asyncio.create_task(harness._resolve_track_data(track))
    await asyncio.sleep(0)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    result = await second
    assert result is not None
    assert result.title == "Resolved"


# ── Admin owner-check correctness ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_owner_check_honours_bot_owners_setting():
    """!stats must authorize anyone in settings.bot_owners (BOT_OWNERS env var),
    matching the rest of the codebase's convention (CommandHelpersMixin._is_bot_owner) —
    not just discord.py's commands.is_owner(), which only knows the application owner."""
    from musicbot.cogs.admin import _is_authorized_owner

    bot = MagicMock()
    bot.settings.bot_owners = (12345,)
    bot.owner_id = None  # not the Discord application owner

    listed_owner_ctx = MagicMock()
    listed_owner_ctx.bot = bot
    listed_owner_ctx.author = MagicMock(id=12345)

    stranger_ctx = MagicMock()
    stranger_ctx.bot = bot
    stranger_ctx.author = MagicMock(id=99999)

    assert await _is_authorized_owner(listed_owner_ctx) is True
    assert await _is_authorized_owner(stranger_ctx) is False


@pytest.mark.asyncio
async def test_stats_owner_check_falls_back_to_application_owner():
    from musicbot.cogs.admin import _is_authorized_owner

    bot = MagicMock()
    bot.settings.bot_owners = ()
    bot.owner_id = 55555

    app_owner_ctx = MagicMock()
    app_owner_ctx.bot = bot
    app_owner_ctx.author = MagicMock(id=55555)

    assert await _is_authorized_owner(app_owner_ctx) is True


# ── Reconnect announcement ───────────────────────────────────────────────────


def _make_fake_guild(guild_id: int, has_channel: bool = True):
    guild = MagicMock()
    guild.id = guild_id
    guild.system_channel = AsyncMock() if has_channel else None
    return guild


@pytest.mark.asyncio
async def test_reconnect_announces_on_first_on_ready_when_snapshot_exists():
    """The motivating case: process restarts fresh (e.g. after an OOM kill via
    systemd Restart=on-failure) — this must fire on the very first on_ready,
    not be suppressed as a 'startup', since a snapshot existing already proves
    this isn't a brand new install. time.monotonic() is patched to a small
    value to simulate a freshly-booted container, where its absolute value can
    itself be under the cooldown window."""
    from musicbot.bot import MusicBot

    fake_bot = MagicMock(spec=MusicBot)
    fake_bot._reconnect_announced_at = {}
    fake_bot.database = MagicMock()
    fake_bot.database.load_queue_snapshot = AsyncMock(return_value=[{"title": "x"}])
    guild = _make_fake_guild(1)
    fake_bot.guilds = [guild]

    with patch("musicbot.bot.time.monotonic", return_value=10.0):
        await MusicBot._maybe_announce_reconnects(fake_bot)

    guild.system_channel.send.assert_awaited_once()
    assert 1 in fake_bot._reconnect_announced_at


@pytest.mark.asyncio
async def test_reconnect_skips_guild_with_no_snapshot():
    from musicbot.bot import MusicBot

    fake_bot = MagicMock(spec=MusicBot)
    fake_bot._reconnect_announced_at = {}
    fake_bot.database = MagicMock()
    fake_bot.database.load_queue_snapshot = AsyncMock(return_value=[])
    guild = _make_fake_guild(1)
    fake_bot.guilds = [guild]

    with patch("musicbot.bot.time.monotonic", return_value=10.0):
        await MusicBot._maybe_announce_reconnects(fake_bot)

    guild.system_channel.send.assert_not_awaited()
    assert 1 not in fake_bot._reconnect_announced_at


@pytest.mark.asyncio
async def test_reconnect_does_not_spam_within_cooldown():
    """Repeated on_ready calls from gateway flapping shouldn't repeat the
    announcement within the cooldown window."""
    from musicbot.bot import MusicBot

    fake_bot = MagicMock(spec=MusicBot)
    fake_bot._reconnect_announced_at = {}
    fake_bot.database = MagicMock()
    fake_bot.database.load_queue_snapshot = AsyncMock(return_value=[{"title": "x"}])
    guild = _make_fake_guild(1)
    fake_bot.guilds = [guild]

    with patch("musicbot.bot.time.monotonic", side_effect=[10.0, 15.0]):
        await MusicBot._maybe_announce_reconnects(fake_bot)
        await MusicBot._maybe_announce_reconnects(fake_bot)

    guild.system_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconnect_later_announces_guild_that_had_no_snapshot_at_startup():
    """A guild with no snapshot at the first on_ready must NOT be permanently
    excluded — if it later (outside the cooldown window) gets a snapshot and
    reconnects again, it should still be announced to."""
    from musicbot.bot import MusicBot

    fake_bot = MagicMock(spec=MusicBot)
    fake_bot._reconnect_announced_at = {}
    fake_bot.database = MagicMock()
    fake_bot.database.load_queue_snapshot = AsyncMock(return_value=[])
    guild = _make_fake_guild(1)
    fake_bot.guilds = [guild]

    with patch("musicbot.bot.time.monotonic", return_value=10.0):
        await MusicBot._maybe_announce_reconnects(fake_bot)
    guild.system_channel.send.assert_not_awaited()

    # Now a snapshot exists and enough time has passed for the cooldown to clear.
    fake_bot.database.load_queue_snapshot = AsyncMock(return_value=[{"title": "x"}])

    with patch("musicbot.bot.time.monotonic", return_value=10.0 + 1000.0):
        await MusicBot._maybe_announce_reconnects(fake_bot)
    guild.system_channel.send.assert_awaited_once()
