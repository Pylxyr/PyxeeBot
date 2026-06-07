from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

class Database:

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._prefix_cache: dict[int, str | None] = {}
        self._dj_role_cache: dict[int, int | None] = {}
        self._conn: aiosqlite.Connection | None = None
        self._snapshot_hashes: dict[int, int] = {}

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await self._create_tables()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is closed.")
        return self._conn

    async def _create_tables(self) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id   INTEGER PRIMARY KEY,
                prefix     TEXT    NOT NULL,
                dj_role_id INTEGER
            )
            """
        )
        async with conn.execute("PRAGMA table_info(guild_settings)") as cursor:
            columns = {row["name"] async for row in cursor}
        if "dj_role_id" not in columns:
            await conn.execute(
                "ALTER TABLE guild_settings ADD COLUMN dj_role_id INTEGER"
            )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_playlists (
                guild_id   INTEGER NOT NULL,
                name       TEXT    NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, name)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_playlist_items (
                guild_id      INTEGER NOT NULL,
                playlist_name TEXT    NOT NULL,
                position      INTEGER NOT NULL,
                query         TEXT    NOT NULL,
                title         TEXT    NOT NULL,
                webpage_url   TEXT    NOT NULL,
                PRIMARY KEY (guild_id, playlist_name, position),
                FOREIGN KEY (guild_id, playlist_name)
                    REFERENCES saved_playlists(guild_id, name)
                    ON DELETE CASCADE
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue_snapshots (
                guild_id     INTEGER NOT NULL,
                position     INTEGER NOT NULL,
                query        TEXT    NOT NULL,
                title        TEXT    NOT NULL,
                webpage_url  TEXT    NOT NULL,
                requester_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, position)
            )
            """
        )
        await conn.commit()

    async def get_prefix(self, guild_id: int) -> str | None:
        if self._conn is None:
            return None
        if guild_id in self._prefix_cache:
            return self._prefix_cache[guild_id]
        async with self._conn.execute(
            "SELECT prefix FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        prefix = row["prefix"] if row else None
        self._prefix_cache[guild_id] = prefix
        return prefix

    async def set_prefix(self, guild_id: int, prefix: str) -> None:
        if self._conn is None:
            return
        await self._conn.execute(
            """
            INSERT INTO guild_settings (guild_id, prefix)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET prefix = excluded.prefix
            """,
            (guild_id, prefix),
        )
        await self._conn.commit()
        self._prefix_cache[guild_id] = prefix

    async def get_dj_role_id(self, guild_id: int) -> int | None:
        if self._conn is None:
            return None
        if guild_id in self._dj_role_cache:
            return self._dj_role_cache[guild_id]
        async with self._conn.execute(
            "SELECT dj_role_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        role_id = (
            int(row["dj_role_id"])
            if row and row["dj_role_id"] is not None
            else None
        )
        self._dj_role_cache[guild_id] = role_id
        return role_id

    async def set_dj_role_id(
        self,
        guild_id: int,
        role_id: int | None,
        default_prefix: str = "!",
    ) -> None:
        if self._conn is None:
            return
        async with self._conn.execute(
            "SELECT prefix FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            existing = await cursor.fetchone()
        if existing:
            await self._conn.execute(
                "UPDATE guild_settings SET dj_role_id = ? WHERE guild_id = ?",
                (role_id, guild_id),
            )
        else:
            await self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, prefix, dj_role_id)
                VALUES (?, ?, ?)
                """,
                (guild_id, default_prefix, role_id),
            )
        await self._conn.commit()
        self._dj_role_cache[guild_id] = role_id
        if not existing:
            self._prefix_cache[guild_id] = default_prefix

    async def save_playlist(
        self,
        guild_id: int,
        name: str,
        created_by: int,
        entries: list[dict[str, Any]],
    ) -> None:
        if self._conn is None:
            return
        await self._conn.execute(
            """
            INSERT INTO saved_playlists (guild_id, name, created_by)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, name) DO UPDATE SET created_by = excluded.created_by
            """,
            (guild_id, name, created_by),
        )
        await self._conn.execute(
            "DELETE FROM saved_playlist_items WHERE guild_id = ? AND playlist_name = ?",
            (guild_id, name),
        )
        await self._conn.executemany(
            """
            INSERT INTO saved_playlist_items (
                guild_id, playlist_name, position, query, title, webpage_url
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    guild_id,
                    name,
                    position,
                    entry["query"],
                    entry["title"],
                    entry["webpage_url"],
                )
                for position, entry in enumerate(entries)
            ],
        )
        await self._conn.commit()

    async def list_playlists(self, guild_id: int) -> list[sqlite3.Row]:
        if self._conn is None:
            return []
        async with self._conn.execute(
            """
            SELECT p.name, p.created_by, p.created_at, COUNT(i.position) AS track_count
            FROM saved_playlists AS p
            LEFT JOIN saved_playlist_items AS i
                ON p.guild_id = i.guild_id AND p.name = i.playlist_name
            WHERE p.guild_id = ?
            GROUP BY p.guild_id, p.name, p.created_by, p.created_at
            ORDER BY p.name COLLATE NOCASE
            """,
            (guild_id,),
        ) as cursor:
            return list(await cursor.fetchall())

    async def get_playlist_entries(self, guild_id: int, name: str) -> list[sqlite3.Row]:
        if self._conn is None:
            return []
        async with self._conn.execute(
            """
            SELECT query, title, webpage_url
            FROM saved_playlist_items
            WHERE guild_id = ? AND playlist_name = ?
            ORDER BY position ASC
            """,
            (guild_id, name),
        ) as cursor:
            return list(await cursor.fetchall())

    async def delete_playlist(self, guild_id: int, name: str) -> bool:
        if self._conn is None:
            return False
        async with self._conn.execute(
            "DELETE FROM saved_playlists WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        ) as cursor:
            deleted = cursor.rowcount
        await self._conn.commit()
        return deleted > 0

    def _snapshot_hash(self, guild_id: int, entries: list[dict[str, Any]]) -> int:
        """Cheap fingerprint of the snapshot payload for dirty-detection."""
        return hash((guild_id, tuple(
            (e.get("query", ""), e.get("title", ""), e.get("webpage_url", ""),
             e.get("requester_id", ""))
            for e in entries
        )))

    async def save_queue_snapshot(
        self, guild_id: int, entries: list[dict[str, Any]]
    ) -> None:
        if self._conn is None:
            return
        new_hash = self._snapshot_hash(guild_id, entries)
        if self._snapshot_hashes.get(guild_id) == new_hash:
            return

        await self._conn.execute(
            "DELETE FROM queue_snapshots WHERE guild_id = ?", (guild_id,)
        )
        if entries:
            await self._conn.executemany(
                """
                INSERT INTO queue_snapshots (
                    guild_id, position, query, title, webpage_url, requester_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        guild_id,
                        position,
                        entry["query"],
                        entry["title"],
                        entry["webpage_url"],
                        entry["requester_id"],
                    )
                    for position, entry in enumerate(entries)
                ],
            )
        await self._conn.commit()
        self._snapshot_hashes[guild_id] = new_hash

    async def load_queue_snapshot(self, guild_id: int) -> list[sqlite3.Row]:
        if self._conn is None:
            return []
        async with self._conn.execute(
            """
            SELECT query, title, webpage_url, requester_id
            FROM queue_snapshots
            WHERE guild_id = ?
            ORDER BY position ASC
            """,
            (guild_id,),
        ) as cursor:
            return list(await cursor.fetchall())
