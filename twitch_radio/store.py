from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class JsonStore:
    """Tiny atomic JSON key-value file, guarded by an in-process lock.

    This replaces the Discord bot's single-row aiosqlite table for the same
    data — that made sense when it lived inside a process that already had a
    shared-write-lock aiosqlite connection open for a dozen other tables;
    pulling in aiosqlite here just for one row of settings would be a lot of
    dependency for very little. Reads/writes are serialized by asyncio.Lock
    (this data is only ever touched from the chat bot's commands and the
    /settings HTTP handler, both in the same event loop), and writes are
    write-temp-then-rename so a crash mid-write can never leave a corrupt or
    half-written file behind.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def read(self) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._read_sync)

    async def write(self, data: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_sync, data)

    async def update(
        self, mutator: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> dict[str, Any]:
        """Read-modify-write while holding the lock across all three steps.

        Plain `read()` then `write()` from a caller is two separate lock
        acquisitions with an await in between — two concurrent callers (e.g.
        two /settings submissions) can each read the same starting state and
        the second write silently clobbers the first. `mutator` receives the
        current dict and returns the dict to persist, or None to leave the
        file untouched (e.g. the caller's own validation failed) — either
        way the lock isn't released until the write (or no-op) is done.
        """
        async with self._lock:
            current = await asyncio.to_thread(self._read_sync)
            updated = mutator(current)
            if updated is None:
                return current
            await asyncio.to_thread(self._write_sync, updated)
            return updated

    def _read_sync(self) -> dict[str, Any]:
        try:
            with self._path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            # Falls back to defaults either way, but silently is the wrong
            # failure mode for "someone's saved tunables just vanished" —
            # this should show up in the logs even though it's non-fatal.
            log.warning("Couldn't read %s — falling back to defaults.", self._path, exc_info=True)
            return {}
        if not isinstance(loaded, dict):
            log.warning("%s did not contain a JSON object — falling back to defaults.", self._path)
            return {}
        return loaded

    def _write_sync(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_path)
            raise
