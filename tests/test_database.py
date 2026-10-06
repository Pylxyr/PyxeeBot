from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from musicbot.database import Database

_OLD_V6_GUILD_SETTINGS = """
CREATE TABLE guild_settings (
    guild_id INTEGER PRIMARY KEY, prefix TEXT NOT NULL, dj_role_id INTEGER,
    stay_connected INTEGER NOT NULL DEFAULT 0, autoplay INTEGER NOT NULL DEFAULT 0,
    show_requester_mentions INTEGER NOT NULL DEFAULT 0, show_link_previews INTEGER NOT NULL DEFAULT 1,
    volume INTEGER NOT NULL DEFAULT 100)
"""


def test_changing_a_setting_does_not_pin_the_prefix(tmp_path: Path) -> None:
    async def main() -> tuple[str | None, int]:
        db = Database(tmp_path / "a.sqlite3")
        await db.initialize()
        try:
            await db.set_volume(1, 80)
            await db.set_stay_connected(1, True)
            return await db.get_prefix(1), await db.get_volume(1)
        finally:
            await db.close()

    prefix, volume = asyncio.run(main())
    assert prefix is None  # still follows DEFAULT_PREFIX
    assert volume == 80


def test_v6_database_is_migrated_and_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute(_OLD_V6_GUILD_SETTINGS)
    raw.execute("INSERT INTO guild_settings (guild_id, prefix, volume) VALUES (5, '?', 70)")
    raw.commit()
    raw.close()

    async def main() -> tuple[str | None, int]:
        db = Database(path)
        await db.initialize()
        try:
            await db.set_autoplay(6, True)  # would have failed on the old NOT NULL column
            return await db.get_prefix(5), await db.get_volume(5)
        finally:
            await db.close()

    assert asyncio.run(main()) == ("?", 70)
    check = sqlite3.connect(path)
    assert check.execute("SELECT version FROM schema_version").fetchone()[0] == 7
    check.close()


def test_playlist_owner_and_count(tmp_path: Path) -> None:
    async def main() -> tuple[int | None, int | None, int]:
        db = Database(tmp_path / "p.sqlite3")
        await db.initialize()
        try:
            await db.save_playlist(1, "mix", 111, [])
            return (
                await db.get_playlist_owner(1, "mix"),
                await db.get_playlist_owner(1, "nope"),
                await db.count_playlists(1),
            )
        finally:
            await db.close()

    assert asyncio.run(main()) == (111, None, 1)
