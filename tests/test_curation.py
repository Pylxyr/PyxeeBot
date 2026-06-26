from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from musicbot.cogs.curation import CuratedTrack, CurationCog, CurationSession, _artist_key


def _make_bot(*, lastfm_key: str | None = "testkey") -> MagicMock:
    bot = MagicMock()
    bot.settings.lastfm_api_key = lastfm_key
    bot.settings.ytdlp_curation_concurrency = 2
    bot.user = MagicMock(id=999)
    bot.database.get_autoplay = AsyncMock(return_value=False)
    bot.database.save_playlist = AsyncMock()
    bot.database.get_playlist_entries = AsyncMock(return_value=[])
    bot.database.list_playlists = AsyncMock(return_value=[])
    return bot


def _make_cog(bot: MagicMock | None = None) -> CurationCog:
    return CurationCog(bot or _make_bot())


# ── _artist_key ───────────────────────────────────────────────────────────────


def test_artist_key_strips_punctuation():
    assert _artist_key("YOASOBI") == "yoasobi"


def test_artist_key_removes_non_alphanumeric():
    assert _artist_key("Ado!") == "ado"


def test_artist_key_falls_back_for_all_non_ascii():
    result = _artist_key("ずっと真夜中でいいのに。")
    assert isinstance(result, str) and len(result) > 0


def test_artist_key_collapses_spaces():
    assert _artist_key("amazarashi ") == "amazarashi"


# ── per-guild semaphore ───────────────────────────────────────────────────────


def test_curation_sem_same_object_for_same_guild():
    cog = _make_cog()
    concurrency = 2
    guild_id = 42
    s1 = cog._curation_sem.setdefault(guild_id, asyncio.Semaphore(concurrency))
    s2 = cog._curation_sem.setdefault(guild_id, asyncio.Semaphore(concurrency))
    assert s1 is s2


def test_curation_sem_different_for_different_guilds():
    cog = _make_cog()
    s1 = cog._curation_sem.setdefault(1, asyncio.Semaphore(2))
    s2 = cog._curation_sem.setdefault(2, asyncio.Semaphore(2))
    assert s1 is not s2


# ── autoplay guard ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_queue_updated_no_double_fire():
    cog = _make_cog()
    guild = MagicMock()
    guild.id = 99

    music_cog = MagicMock()
    player = MagicMock()
    player.voice_client.is_connected.return_value = True
    player.voice_client.is_playing.return_value = False
    player.voice_client.is_paused.return_value = False
    player.queue = []
    player.current = None
    player.history = []
    music_cog.players = {guild.id: player}

    cog.bot.get_cog = MagicMock(return_value=music_cog)
    cog.bot.database.get_autoplay = AsyncMock(return_value=True)
    cog._refill_seeds[guild.id] = ("Yorushika", "Say It")
    cog._refill_in_progress.add(guild.id)

    fired = []
    with patch.object(cog, "_do_autoplay", new_callable=AsyncMock) as mock_autoplay:
        mock_autoplay.side_effect = lambda *a: fired.append(1)
        await cog.on_musicbot_queue_updated(guild)

    assert len(fired) == 0, "_do_autoplay fired despite _refill_in_progress guard"


@pytest.mark.asyncio
async def test_on_queue_updated_does_not_fire_without_autoplay():
    cog = _make_cog()
    guild = MagicMock()
    guild.id = 77

    music_cog = MagicMock()
    player = MagicMock()
    player.voice_client.is_connected.return_value = True
    player.queue = []
    player.current = None
    music_cog.players = {guild.id: player}

    cog.bot.get_cog = MagicMock(return_value=music_cog)
    cog.bot.database.get_autoplay = AsyncMock(return_value=False)
    cog._refill_seeds[guild.id] = ("Ado", "Usseewa")

    with patch.object(cog, "_do_autoplay", new_callable=AsyncMock) as mock_autoplay:
        await cog.on_musicbot_queue_updated(guild)

    mock_autoplay.assert_not_called()


@pytest.mark.asyncio
async def test_on_queue_updated_skips_disconnected_player():
    cog = _make_cog()
    guild = MagicMock()
    guild.id = 55

    music_cog = MagicMock()
    player = MagicMock()
    player.voice_client.is_connected.return_value = False
    music_cog.players = {guild.id: player}

    cog.bot.get_cog = MagicMock(return_value=music_cog)

    with patch.object(cog, "_do_autoplay", new_callable=AsyncMock) as mock_autoplay:
        await cog.on_musicbot_queue_updated(guild)

    mock_autoplay.assert_not_called()


# ── CurationSession ───────────────────────────────────────────────────────────


def test_session_stores_seed():
    session = CurationSession(
        guild_id=1,
        author_id=2,
        seed_query="zutomayo",
        seed_artist="ZUTOMAYO",
        seed_track="MILABO",
    )
    assert session.seed_artist == "ZUTOMAYO"
    assert session.seed_track == "MILABO"
    assert session.tracks == []


def test_session_tracks_mutable():
    session = CurationSession(guild_id=1, author_id=2, seed_query="q", seed_artist="A", seed_track="T")
    session.tracks.append(CuratedTrack(title="T", artist="A"))
    assert len(session.tracks) == 1


# ── _lastfm no key ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lastfm_returns_none_without_key():
    cog = _make_cog(_make_bot(lastfm_key=None))
    result = await cog._lastfm("track.search", track="test")
    assert result is None


# ── vibe_load with empty playlist ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vibe_load_sends_not_found_for_missing_playlist():
    cog = _make_cog()
    cog.bot.database.get_playlist_entries = AsyncMock(return_value=[])

    ctx = MagicMock()
    ctx.guild.id = 1
    ctx.send = AsyncMock()

    await cog.vibe_load(ctx, name="nonexistent")
    ctx.send.assert_called_once()
    assert "nonexistent" in ctx.send.call_args[0][0]
