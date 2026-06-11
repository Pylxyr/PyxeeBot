"""test_player.py — unit tests for GuildPlayer state transitions."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from musicbot.cogs.music.player import GuildPlayer
from musicbot.cogs.music.models import Track
from tests.conftest import make_bot, make_guild, make_track


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _player(**bot_kwargs) -> GuildPlayer:
    return GuildPlayer(
        make_bot(**bot_kwargs),
        make_guild(),
        AsyncMock(side_effect=lambda t: t),
        AsyncMock(return_value=MagicMock()),
        AsyncMock(return_value=True),
    )


def _vc(playing: bool = False, paused: bool = False) -> MagicMock:
    vc            = MagicMock()
    vc.is_playing = MagicMock(return_value=playing)
    vc.is_paused  = MagicMock(return_value=paused)
    vc.channel    = MagicMock()
    return vc


# ── enqueue ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enqueue_increments_total_duration():
    p = _player()
    t = make_track(duration=120)
    await p.enqueue(t)
    assert p._total_duration == 120


@pytest.mark.asyncio
async def test_enqueue_two_tracks_accumulates():
    p  = _player()
    t1 = make_track(title="A", duration=100)
    t2 = make_track(title="B", duration=80)
    await p.enqueue(t1)
    await p.enqueue(t2)
    assert p._total_duration == 180


@pytest.mark.asyncio
async def test_enqueue_front_places_track_first():
    p  = _player()
    t1 = make_track(title="First",  duration=60)
    t2 = make_track(title="Second", duration=60)
    await p.enqueue(t1)
    await p.enqueue(t2, front=True)
    assert p.queue[0].title == "Second"


@pytest.mark.asyncio
async def test_enqueue_at_capacity_evicts_last_when_appending():
    p  = _player(max_queue_size=2)
    t1 = make_track(title="A", duration=10)
    t2 = make_track(title="B", duration=20)
    t3 = make_track(title="C", duration=30)
    await p.enqueue(t1)
    await p.enqueue(t2)
    await p.enqueue(t3)
    titles = [t.title for t in p.queue]
    assert "C" in titles


@pytest.mark.asyncio
async def test_enqueue_at_capacity_evicts_last_when_front():
    p  = _player(max_queue_size=2)
    t1 = make_track(title="A", duration=10)
    t2 = make_track(title="B", duration=20)
    t3 = make_track(title="C", duration=30)
    await p.enqueue(t1)
    await p.enqueue(t2)
    await p.enqueue(t3, front=True)
    assert p.queue[0].title == "C"


# ── replace_queue ─────────────────────────────────────────────────────────────

def test_replace_queue_resets_total_duration():
    p  = _player()
    t1 = make_track(duration=100)
    t2 = make_track(duration=200)
    p.queue.append(t1)
    p._total_duration = 100
    p.replace_queue([t2])
    assert p._total_duration == 200


def test_replace_queue_with_empty_list_zeroes_duration():
    p = _player()
    p.queue.append(make_track(duration=99))
    p._total_duration = 99
    p.replace_queue([])
    assert p._total_duration == 0
    assert len(p.queue) == 0


def test_replace_queue_respects_maxlen():
    p  = _player(max_queue_size=2)
    tracks = [make_track(title=str(i), duration=10) for i in range(5)]
    p.replace_queue(tracks)
    assert len(p.queue) <= 2


def test_replace_queue_duration_matches_queued_tracks():
    p      = _player()
    tracks = [make_track(duration=d) for d in [30, 45, 60]]
    p.replace_queue(tracks)
    assert p._total_duration == 135


# ── snapshot ──────────────────────────────────────────────────────────────────

def test_snapshot_includes_current_track():
    p         = _player()
    p.current = make_track(title="Now", query="now")
    entries   = p.snapshot()
    assert entries[0]["title"] == "Now"


def test_snapshot_includes_queued_tracks():
    p  = _player()
    t1 = make_track(title="Q1", query="q1")
    t2 = make_track(title="Q2", query="q2")
    p.queue.append(t1)
    p.queue.append(t2)
    titles = [e["title"] for e in p.snapshot()]
    assert "Q1" in titles and "Q2" in titles


def test_snapshot_query_falls_back_to_webpage_url():
    p = _player()
    t = make_track(title="T", query="", webpage_url="https://yt.be/abc")
    p.queue.append(t)
    entry = p.snapshot()[0]
    assert entry["query"] == "https://yt.be/abc"


def test_snapshot_query_falls_back_to_title_when_both_empty():
    p = _player()
    t = Track(
        title="Fallback Title", webpage_url="", stream_url="",
        uploader="U", duration=0, requester_id=1, query="",
    )
    p.queue.append(t)
    entry = p.snapshot()[0]
    assert entry["query"] == "Fallback Title"


def test_snapshot_empty_player_returns_empty_list():
    p = _player()
    assert p.snapshot() == []


def test_snapshot_webpage_url_normalised_to_empty_string():
    p = _player()
    t = Track(
        title="T", webpage_url=None, stream_url="",  # type: ignore[arg-type]
        uploader="U", duration=0, requester_id=1, query="t",
    )
    p.queue.append(t)
    entry = p.snapshot()[0]
    assert entry["webpage_url"] == ""


# ── pause / resume ────────────────────────────────────────────────────────────

def test_pause_returns_false_when_not_playing():
    p            = _player()
    p.voice_client = _vc(playing=False)
    assert p.pause() is False


def test_pause_returns_true_when_playing():
    p              = _player()
    p.voice_client = _vc(playing=True)
    assert p.pause() is True
    assert p._pause_started > 0


def test_resume_returns_false_when_not_paused():
    p              = _player()
    p.voice_client = _vc(paused=False)
    assert p.resume() is False


def test_resume_accumulates_total_paused():
    p              = _player()
    p.voice_client = _vc(playing=True)
    p.pause()
    p.voice_client.is_paused = MagicMock(return_value=True)
    time.sleep(0.02)
    p.resume()
    assert p._total_paused > 0
    assert p._pause_started == 0.0


# ── elapsed_seconds ───────────────────────────────────────────────────────────

def test_elapsed_seconds_zero_before_start():
    p = _player()
    assert p.elapsed_seconds == 0.0


def test_elapsed_seconds_increases_after_started_at():
    p            = _player()
    p.started_at = time.monotonic() - 2.0
    assert p.elapsed_seconds >= 1.5


def test_elapsed_seconds_excludes_paused_time():
    p              = _player()
    p.started_at   = time.monotonic() - 5.0
    p._total_paused = 3.0
    assert p.elapsed_seconds < 3.0


# ── skip ─────────────────────────────────────────────────────────────────────

def test_skip_calls_voice_client_stop():
    p              = _player()
    p.voice_client = _vc(playing=True)
    result         = p.skip()
    assert result is True
    p.voice_client.stop.assert_called_once()


def test_skip_returns_false_when_no_voice_client():
    p = _player()
    assert p.skip() is False


# ── play_previous ─────────────────────────────────────────────────────────────

def test_play_previous_returns_false_when_history_empty():
    p = _player()
    assert p.play_previous() is False


def test_play_previous_prepends_track_to_queue():
    p                = _player()
    prev             = make_track(title="Previous")
    current          = make_track(title="Current")
    p.history.append(prev)
    p.current        = current
    p.voice_client   = _vc(playing=True)
    result           = p.play_previous()
    assert result is True
    titles = [t.title for t in list(p.queue)[:2]]
    assert "Previous" in titles
    assert "Current" in titles


def test_play_previous_sets_rewind_requested():
    p              = _player()
    p.history.append(make_track(title="Prev"))
    p.voice_client = _vc(playing=False)
    p.play_previous()
    assert p.rewind_requested is True


# ── Per-user queue count ──────────────────────────────────────────────────────

def test_user_queue_count_correct():
    p = _player()
    p.queue.extend([
        make_track(title="A", requester_id=111),
        make_track(title="B", requester_id=111),
        make_track(title="C", requester_id=222),
    ])
    count_111 = sum(1 for t in p.queue if t.requester_id == 111)
    count_222 = sum(1 for t in p.queue if t.requester_id == 222)
    assert count_111 == 2
    assert count_222 == 1


def test_user_queue_count_zero_for_unknown_user():
    p = _player()
    p.queue.extend([make_track(requester_id=111)])
    count = sum(1 for t in p.queue if t.requester_id == 999)
    assert count == 0


def test_user_queue_count_empty_queue():
    p = _player()
    assert sum(1 for t in p.queue if t.requester_id == 111) == 0
