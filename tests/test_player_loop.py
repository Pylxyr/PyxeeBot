from __future__ import annotations

import asyncio
import types
from typing import Any

from musicbot.cogs.music.models import Track
from musicbot.cogs.music.player import GuildPlayer
from tests.conftest import make_track


class FakeSource:
    def __init__(self) -> None:
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True


class FakeVoiceClient:
    """Plays a source instantly and fires `after` from a worker thread like discord.py does."""

    def __init__(self) -> None:
        self.played: list[Any] = []
        self.channel = types.SimpleNamespace(members=[])

    def is_connected(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return False

    def is_paused(self) -> bool:
        return False

    def play(self, source: Any, *, after: Any) -> None:
        self.played.append(source)
        after(None)

    async def disconnect(self, force: bool = False) -> None:
        return None


class Harness:
    def __init__(self) -> None:
        self.events: list[str] = []
        db = types.SimpleNamespace(add_play_history=self._history)
        settings = types.SimpleNamespace(
            max_queue_size=10,
            near_end_prefetch_seconds=30,
            np_auto_refresh=False,
            idle_timeout_seconds=3600,
        )
        self.bot = types.SimpleNamespace(
            settings=settings,
            database=db,
            dispatch=lambda name, *a: self.events.append(name),
        )
        self.sources: list[FakeSource] = []
        self.resolve_gate: asyncio.Event | None = None
        self.vc = FakeVoiceClient()

    async def _history(self, *_a: object) -> None:
        return None

    async def resolver(self, track: Track) -> Track | None:
        if self.resolve_gate is not None:
            await self.resolve_gate.wait()
        track.stream_url = "http://8.8.8.8/s"
        track.resolved_at = 10**12
        return track

    async def factory(self, _track: Track, _volume: int) -> FakeSource:
        src = FakeSource()
        self.sources.append(src)
        return src

    async def validate(self, _track: Track) -> bool:
        return True

    async def make_player(self) -> GuildPlayer:
        guild = types.SimpleNamespace(id=1)
        player = GuildPlayer(self.bot, guild, self.resolver, self.factory, self.validate)  # type: ignore[arg-type]
        player.voice_client = self.vc  # type: ignore[assignment]
        player.player_task = asyncio.create_task(player._player_loop())
        return player


async def _settle(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def test_track_plays_and_moves_to_history() -> None:
    async def main() -> None:
        h = Harness()
        player = await h.make_player()
        await player.enqueue(make_track(1))
        await _settle()
        assert len(h.vc.played) == 1
        assert [t.title for t in player.history] == ["t1"]
        assert player.current is None
        assert player.player_task is not None
        player.player_task.cancel()

    asyncio.run(main())


def test_stop_during_resolve_does_not_resurrect_track() -> None:
    async def main() -> None:
        h = Harness()
        h.resolve_gate = asyncio.Event()
        player = await h.make_player()
        await player.enqueue(make_track(1))
        await _settle()
        assert player.current is not None  # resolver is blocked on the gate
        await player.stop()
        h.resolve_gate.set()
        await _settle()
        assert h.vc.played == []
        assert h.sources == []
        assert player.current is None
        assert player.player_task is not None
        player.player_task.cancel()

    asyncio.run(main())


def test_skip_while_resolving_is_honoured() -> None:
    async def main() -> None:
        h = Harness()
        h.resolve_gate = asyncio.Event()
        player = await h.make_player()
        await player.enqueue(make_track(1))
        await player.enqueue(make_track(2))
        await _settle()
        assert player.current is not None and player.current.title == "t1"
        assert player.skip() is True
        h.resolve_gate.set()
        await _settle(40)
        assert [t.title for t in player.history] == ["t2"]
        assert player.player_task is not None
        player.player_task.cancel()

    asyncio.run(main())


def test_voice_client_gone_cleans_source_and_keeps_loop_alive() -> None:
    async def main() -> None:
        h = Harness()
        player = await h.make_player()
        orig_factory = h.factory

        async def factory(track: Track, volume: int) -> FakeSource:
            src = await orig_factory(track, volume)
            player.voice_client = None  # connection vanished while ffmpeg was spawning
            return src

        player.audio_source_factory = factory  # type: ignore[assignment]
        await player.enqueue(make_track(1))
        await _settle()
        assert h.sources[0].cleaned is True
        assert player.player_task is not None and not player.player_task.done()
        player.player_task.cancel()

    asyncio.run(main())
