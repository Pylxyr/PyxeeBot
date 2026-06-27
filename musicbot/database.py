from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

_CURRENT_SCHEMA_VERSION = 3
_HISTORY_MAX_ROWS = 5000


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._prefix_cache: dict[int, str | None] = {}
        self._dj_role_cache: dict[int, int | None] = {}
        self._stay_connected_cache: dict[int, bool] = {}
        self._autoplay_cache: dict[int, bool] = {}
        self._conn: aiosqlite.Connection | None = None
        self._snapshot_hashes: dict[int, str] = {}
        # Serialises every write (not just multi-statement transactions) — a
        # single-statement commit() from one guild can otherwise land inside
        # another guild's open BEGIN IMMEDIATE on this same shared connection
        # and force-commit it early, since SQLite transactions are connection-
        # scoped rather than statement-scoped.
        self._write_lock = asyncio.Lock()

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
                guild_id       INTEGER PRIMARY KEY,
                prefix         TEXT    NOT NULL,
                dj_role_id     INTEGER,
                stay_connected INTEGER NOT NULL DEFAULT 0,
                autoplay       INTEGER NOT NULL DEFAULT 0
            )
            """
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
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS play_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id     INTEGER NOT NULL,
                title        TEXT    NOT NULL,
                webpage_url  TEXT    NOT NULL,
                requester_id INTEGER NOT NULL,
                played_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_play_history_guild ON play_history(guild_id, played_at)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_play_history_url ON play_history(guild_id, webpage_url, played_at)"
        )

        await self._run_migrations(conn)
        await conn.commit()

    async def _run_migrations(self, conn: aiosqlite.Connection) -> None:
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL PRIMARY KEY)")
        async with conn.execute("PRAGMA table_info(schema_version)") as cur:
            rows = await cur.fetchall()
        if rows and not rows[0]["pk"]:
            await conn.execute("CREATE TABLE _sv_tmp (version INTEGER NOT NULL PRIMARY KEY)")
            await conn.execute("INSERT OR IGNORE INTO _sv_tmp SELECT version FROM schema_version LIMIT 1")
            await conn.execute("DROP TABLE schema_version")
            await conn.execute("ALTER TABLE _sv_tmp RENAME TO schema_version")
        async with conn.execute("SELECT version FROM schema_version") as cur:
            row = await cur.fetchone()

        if row is None:
            # First run under this migration system — detect current column
            # state so we don't try to ADD columns that already exist.
            async with conn.execute("PRAGMA table_info(guild_settings)") as cursor:
                existing = {r["name"] async for r in cursor}
            if "autoplay" in existing:
                current = 3
            elif "stay_connected" in existing:
                current = 2
            elif "dj_role_id" in existing:
                current = 1
            else:
                current = 0
            await conn.execute("INSERT INTO schema_version (version) VALUES (?)", (current,))
        else:
            current = row["version"]

        if current < 1:
            await conn.execute("ALTER TABLE guild_settings ADD COLUMN dj_role_id INTEGER")
            current = 1
            await conn.execute("UPDATE schema_version SET version = ?", (current,))

        if current < 2:
            await conn.execute(
                "ALTER TABLE guild_settings ADD COLUMN stay_connected INTEGER NOT NULL DEFAULT 0"
            )
            current = 2
            await conn.execute("UPDATE schema_version SET version = ?", (current,))

        if current < 3:
            await conn.execute("ALTER TABLE guild_settings ADD COLUMN autoplay INTEGER NOT NULL DEFAULT 0")
            current = 3
            await conn.execute("UPDATE schema_version SET version = ?", (current,))

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
        async with self._write_lock:
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
        role_id = int(row["dj_role_id"]) if row and row["dj_role_id"] is not None else None
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
        async with self._write_lock:
            await self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, prefix, dj_role_id)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET dj_role_id = excluded.dj_role_id
                """,
                (guild_id, default_prefix, role_id),
            )
            await self._conn.commit()
        self._dj_role_cache[guild_id] = role_id
        self._prefix_cache.setdefault(guild_id, default_prefix)

    async def save_playlist(
        self,
        guild_id: int,
        name: str,
        created_by: int,
        entries: list[dict[str, Any]],
    ) -> None:
        if self._conn is None:
            return
        async with self._write_lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
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
            except Exception:
                await self._conn.rollback()
                raise

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
        async with self._write_lock:
            async with self._conn.execute(
                "DELETE FROM saved_playlists WHERE guild_id = ? AND name = ?",
                (guild_id, name),
            ) as cursor:
                deleted = cursor.rowcount
            await self._conn.commit()
        return deleted > 0

    def _snapshot_hash(self, guild_id: int, entries: list[dict[str, Any]]) -> str:
        payload = json.dumps(
            [
                (e.get("query", ""), e.get("title", ""), e.get("webpage_url", ""), e.get("requester_id", 0))
                for e in entries
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(f"{guild_id}:{payload}".encode()).hexdigest()[:16]

    async def save_queue_snapshot(self, guild_id: int, entries: list[dict[str, Any]]) -> None:
        if self._conn is None:
            return
        new_hash = self._snapshot_hash(guild_id, entries)
        if self._snapshot_hashes.get(guild_id) == new_hash:
            return

        async with self._write_lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._conn.execute("DELETE FROM queue_snapshots WHERE guild_id = ?", (guild_id,))
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
            except Exception:
                await self._conn.rollback()
                raise
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

    async def get_stay_connected(self, guild_id: int) -> bool:
        if self._conn is None:
            return False
        if guild_id in self._stay_connected_cache:
            return self._stay_connected_cache[guild_id]
        async with self._conn.execute(
            "SELECT stay_connected FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        value = bool(row["stay_connected"]) if row else False
        self._stay_connected_cache[guild_id] = value
        return value

    async def set_stay_connected(self, guild_id: int, enabled: bool, default_prefix: str = "!") -> None:
        if self._conn is None:
            return
        async with self._write_lock:
            await self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, prefix, stay_connected)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET stay_connected = excluded.stay_connected
                """,
                (guild_id, default_prefix, int(enabled)),
            )
            await self._conn.commit()
        self._stay_connected_cache[guild_id] = enabled
        self._prefix_cache.setdefault(guild_id, default_prefix)

    async def get_autoplay(self, guild_id: int) -> bool:
        if self._conn is None:
            return False
        if guild_id in self._autoplay_cache:
            return self._autoplay_cache[guild_id]
        async with self._conn.execute(
            "SELECT autoplay FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        value = bool(row["autoplay"]) if row else False
        self._autoplay_cache[guild_id] = value
        return value

    async def set_autoplay(self, guild_id: int, enabled: bool, default_prefix: str = "!") -> None:
        if self._conn is None:
            return
        async with self._write_lock:
            await self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, prefix, autoplay)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET autoplay = excluded.autoplay
                """,
                (guild_id, default_prefix, int(enabled)),
            )
            await self._conn.commit()
        self._autoplay_cache[guild_id] = enabled
        self._prefix_cache.setdefault(guild_id, default_prefix)

    async def add_play_history(self, guild_id: int, title: str, webpage_url: str, requester_id: int) -> None:
        if self._conn is None:
            return
        async with self._write_lock:
            await self._conn.execute(
                """
                INSERT INTO play_history (guild_id, title, webpage_url, requester_id)
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, title, webpage_url, requester_id),
            )
            await self._conn.execute(
                """
                DELETE FROM play_history
                WHERE guild_id = ? AND id NOT IN (
                    SELECT id FROM play_history WHERE guild_id = ?
                    ORDER BY played_at DESC LIMIT ?
                )
                """,
                (guild_id, guild_id, _HISTORY_MAX_ROWS),
            )
            await self._conn.commit()

    async def get_top_played(self, guild_id: int, limit: int = 10) -> list[sqlite3.Row]:
        if self._conn is None:
            return []
        async with self._conn.execute(
            """
            SELECT title, webpage_url, COUNT(*) AS play_count
            FROM play_history
            WHERE guild_id = ?
            GROUP BY webpage_url
            ORDER BY play_count DESC, MAX(played_at) DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ) as cursor:
            return list(await cursor.fetchall())

    async def get_top_requesters(self, guild_id: int, limit: int = 10) -> list[sqlite3.Row]:
        if self._conn is None:
            return []
        async with self._conn.execute(
            """
            SELECT requester_id, COUNT(*) AS request_count
            FROM play_history
            WHERE guild_id = ?
            GROUP BY requester_id
            ORDER BY request_count DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ) as cursor:
            return list(await cursor.fetchall())
