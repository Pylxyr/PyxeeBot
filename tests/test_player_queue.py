from __future__ import annotations

import asyncio
import types

from musicbot.cogs.music.player import GuildPlayer
from tests.conftest import make_track


async def _noop(*_a: object) -> None:
    return None


def _player(max_queue_size: int = 3) -> GuildPlayer:
    bot = types.SimpleNamespace(settings=types.SimpleNamespace(max_queue_size=max_queue_size))
    guild = types.SimpleNamespace(id=1)
    return GuildPlayer(bot, guild, _noop, _noop, _noop)  # type: ignore[arg-type]


def test_enqueue_refuses_when_full_and_keeps_existing_tracks() -> None:
    async def main() -> None:
        player = _player(2)
        assert await player.enqueue(make_track(1, duration=10))
        assert await player.enqueue(make_track(2, duration=10))
        assert not await player.enqueue(make_track(3, duration=10))
        assert [t.title for t in player.queue] == ["t1", "t2"]
        assert player._total_duration == 20

    asyncio.run(main())


def test_internal_requeue_never_evicts_a_queued_track() -> None:
    """Regression: front-inserting into a full maxlen deque dropped the tail track."""

    async def main() -> None:
        player = _player(2)
        await player.enqueue(make_track(1, duration=10))
        await player.enqueue(make_track(2, duration=10))
        player._insert_track(make_track(9, duration=5), front=True)  # seek/volume/prev path
        assert [t.title for t in player.queue] == ["t9", "t1", "t2"]
        assert player._total_duration == 25

    asyncio.run(main())


def test_replace_queue_respects_cap() -> None:
    player = _player(2)
    player.replace_queue([make_track(i, duration=1) for i in range(5)])
    assert len(player.queue) == 2
    assert player._total_duration == 2
