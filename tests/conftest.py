from __future__ import annotations

import types

import pytest

import musicbot.cogs.music._extraction as ext
from musicbot.cogs.music.models import Track


class FakeYDL:
    """Stands in for yt-dlp's YoutubeDL; behaviour can be swapped per test."""

    on_extract = staticmethod(
        lambda query: {
            "url": "http://8.8.8.8/stream",
            "webpage_url": query,
            "title": "t",
            "duration": 5,
        }
    )

    def __init__(self, opts: object) -> None:
        pass

    def extract_info(self, query: str, download: bool = False) -> dict[str, object]:
        return FakeYDL.on_extract(query)  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> type[FakeYDL]:
    monkeypatch.setattr(ext, "YoutubeDL", FakeYDL)
    original = FakeYDL.on_extract
    yield FakeYDL
    FakeYDL.on_extract = original


@pytest.fixture
def settings() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        ytdlp_concurrent_extracts=1,
        ytdlp_curation_concurrency=3,
        ytdlp_socket_timeout=15,
        max_playlist_size=25,
        max_queue_size=3,
        ytdlp_cookies_file=None,
        ytdlp_js_runtime_path=None,
        ytdlp_extract_timeout_seconds=1,
        ytdlp_resolve_cache_ttl_seconds=1800,
        ytdlp_resolve_cache_size=128,
        ytdlp_search_results=5,
        allow_private_urls=False,
    )


def make_track(n: int = 1, *, duration: int = 0, stream_url: str = "") -> Track:
    url = f"https://example.com/watch?v={n}"
    return Track(
        title=f"t{n}",
        webpage_url=url,
        stream_url=stream_url,
        uploader="u",
        duration=duration,
        requester_id=1,
        query=url,
    )
