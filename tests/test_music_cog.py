from __future__ import annotations

import asyncio
import threading
import types

import pytest
from discord.ext import commands

from musicbot.cogs.music._context import _CURRENT_GUILD_ID
from musicbot.cogs.music.cog import MusicCog
from tests.conftest import FakeYDL, make_track


def _cog(settings: types.SimpleNamespace) -> MusicCog:
    return MusicCog(types.SimpleNamespace(settings=settings))  # type: ignore[arg-type]


def test_warmup_restore_does_not_deadlock_the_guild(settings: types.SimpleNamespace) -> None:
    """Regression: warmup used to hold the per-guild Semaphore(1) while _extract_info
    tried to take the same one, wedging every later lookup in that guild."""

    async def main() -> None:
        cog = _cog(settings)
        try:
            await asyncio.wait_for(cog._warmup_restore([make_track(1)], guild_id=42), 5)
            token = _CURRENT_GUILD_ID.set(42)
            try:
                resolved = await asyncio.wait_for(cog._resolve_track(make_track(2)), 5)
            finally:
                _CURRENT_GUILD_ID.reset(token)
            assert resolved is not None
        finally:
            cog._ytdl_executor.shutdown(wait=False)

    asyncio.run(main())


def test_extract_slot_is_held_until_the_worker_thread_finishes(settings: types.SimpleNamespace) -> None:
    release = threading.Event()
    FakeYDL.on_extract = staticmethod(lambda q: (release.wait(10), {"url": "x", "title": "t"})[1])  # type: ignore[assignment]
    settings.ytdlp_extract_timeout_seconds = 0.2

    async def main() -> None:
        cog = _cog(settings)
        token = _CURRENT_GUILD_ID.set(7)
        try:
            with pytest.raises(commands.BadArgument, match="timed out"):
                await cog._extract_info("https://example.com/a")
            # The caller gave up, but the thread is still running: slots must stay taken.
            assert cog._guild_extract_semaphores[7].locked()
            assert cog.extract_semaphore.locked()
            release.set()
            for _ in range(50):
                if not cog.extract_semaphore.locked():
                    break
                await asyncio.sleep(0.05)
            assert not cog._guild_extract_semaphores[7].locked()
            assert not cog.extract_semaphore.locked()
        finally:
            _CURRENT_GUILD_ID.reset(token)
            release.set()
            cog._ytdl_executor.shutdown(wait=False)

    asyncio.run(main())


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://10.0.0.5/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://2130706433/",
        "http://[::1]/x",
    ],
)
def test_private_urls_are_rejected_before_extraction(settings: types.SimpleNamespace, url: str) -> None:
    calls: list[str] = []
    FakeYDL.on_extract = staticmethod(lambda q: calls.append(q) or {"url": "x"})  # type: ignore[assignment]

    async def main() -> None:
        cog = _cog(settings)
        try:
            with pytest.raises(commands.BadArgument, match="private or local"):
                await cog._extract_info(url)
        finally:
            cog._ytdl_executor.shutdown(wait=False)

    asyncio.run(main())
    assert calls == []


def test_private_urls_can_be_enabled(settings: types.SimpleNamespace) -> None:
    settings.allow_private_urls = True

    async def main() -> dict[str, object]:
        cog = _cog(settings)
        try:
            return await cog._extract_info("http://127.0.0.1/x")
        finally:
            cog._ytdl_executor.shutdown(wait=False)

    assert asyncio.run(main())["title"] == "t"


def test_public_urls_and_non_http_queries_pass(settings: types.SimpleNamespace) -> None:
    from musicbot.cogs.music._urlsafety import is_public_http_url

    async def main() -> list[bool]:
        return [
            await is_public_http_url("https://8.8.8.8/x"),
            await is_public_http_url("ytsearch5:some song"),
        ]

    assert asyncio.run(main()) == [True, True]


def _leave_ctx(guild_id: int = 5) -> types.SimpleNamespace:
    sent: list[str] = []

    async def send(content: str, **_: object) -> None:
        sent.append(content)

    ctx = types.SimpleNamespace(
        guild=types.SimpleNamespace(id=guild_id),
        author=types.SimpleNamespace(id=1),
        send=send,
        sent=sent,
    )
    return ctx


@pytest.mark.parametrize(
    ("stay", "is_dj", "allowed"),
    [(True, False, False), (True, True, True), (False, False, True), (False, True, True)],
)
def test_leave_is_restricted_to_djs_only_while_24_7_is_on(
    settings: types.SimpleNamespace, stay: bool, is_dj: bool, allowed: bool
) -> None:
    async def get_stay_connected(_guild_id: int) -> bool:
        return stay

    async def main() -> None:
        cog = MusicCog(
            types.SimpleNamespace(  # type: ignore[arg-type]
                settings=settings,
                database=types.SimpleNamespace(get_stay_connected=get_stay_connected),
            )
        )
        try:

            async def fake_is_dj(_member: object) -> bool:
                return is_dj

            async def fake_cancel(_guild_id: int) -> None:
                return None

            cog._is_dj = fake_is_dj  # type: ignore[method-assign]
            cog._cancel_curation_activity = fake_cancel  # type: ignore[method-assign]
            ctx = _leave_ctx()
            if allowed:
                await cog.leave.callback(cog, ctx)  # type: ignore[misc]
                assert ctx.sent == ["I am not connected."]
            else:
                with pytest.raises(commands.CheckFailure):
                    await cog.leave.callback(cog, ctx)  # type: ignore[misc]
        finally:
            cog._ytdl_executor.shutdown(wait=False)

    asyncio.run(main())
