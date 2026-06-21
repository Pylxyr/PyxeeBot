"""_lifecycle.py — LifecycleMixin: player creation/restore/cleanup and debounced snapshot persistence.

Mixed into MusicCog.  Depends on bot, players, now_playing_messages, _bg_task, _resolve_track,
_build_audio_source, _validate_stream_url, and the snapshot/pipeline/np-refresh task dicts.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import discord

from musicbot.cogs.music.constants import SNAPSHOT_DEBOUNCE_SECONDS
from musicbot.cogs.music.models import Track
from musicbot.cogs.music.player import GuildPlayer


class LifecycleMixin:
    """Player creation/restore/cleanup and debounced queue-snapshot persistence."""

    # ── Player lifecycle ────────────────────────────────────────────────────

    async def _get_player(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if player:
            return player
        lock = self._player_create_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            player = self.players.get(guild.id)
            if not player:
                player = await GuildPlayer.create(
                    self.bot,
                    guild,
                    self._resolve_track,
                    self._build_audio_source,
                    self._validate_stream_url,
                )
                self.players[guild.id] = player
                player.stay_connected = await self.bot.database.get_stay_connected(guild.id)
                await self._restore_snapshot(player)
            return player

    async def _restore_snapshot(self, player: GuildPlayer) -> None:
        if not self.bot.settings.restore_queue_on_restart:
            await self.bot.database.save_queue_snapshot(player.guild.id, [])
            return
        rows = await self.bot.database.load_queue_snapshot(player.guild.id)
        if player.queue:
            return
        restored = [
            Track(
                title=row["title"],
                webpage_url=row["webpage_url"],
                stream_url="",
                uploader="Restored queue",
                duration=0,
                requester_id=int(row["requester_id"]),
                query=row["query"],
            )
            for row in rows
        ]
        player.replace_queue(restored)
        if restored:
            self._restored_guilds.add(player.guild.id)
            self._bg_task(
                self._warmup_restore(list(restored[:3]), guild_id=player.guild.id),
                name="warmup-restore",
            )

    async def _warmup_restore(self, tracks: list[Track], *, guild_id: int) -> None:
        sem = self._guild_extract_semaphores.setdefault(guild_id, asyncio.Semaphore(1))
        for track in tracks:
            async with sem:
                with contextlib.suppress(Exception):
                    await self._resolve_track(track)

    async def _cleanup_guild(self, guild_id: int) -> None:
        player = self.players.pop(guild_id, None)
        if player:
            await player.destroy()
        await self._flush_snapshot(guild_id, entries=[])
        for task_dict in (self._snapshot_tasks, self._np_refresh_tasks, self._pipeline_tasks):
            task = task_dict.pop(guild_id, None)
            if task and not task.done():
                task.cancel()
        self._guild_extract_semaphores.pop(guild_id, None)
        self._curation_semaphores.pop(guild_id, None)
        self._player_create_locks.pop(guild_id, None)
        self.now_playing_messages.pop(guild_id, None)

    # ── Snapshot persistence ────────────────────────────────────────────────

    def _persist_snapshot(self, guild_id: int) -> None:
        self._snapshot_deadlines[guild_id] = time.monotonic() + SNAPSHOT_DEBOUNCE_SECONDS
        task = self._snapshot_tasks.get(guild_id)
        if task and not task.done():
            return
        self._snapshot_tasks[guild_id] = self._bg_task(
            self._snapshot_loop(guild_id), name=f"snapshot-{guild_id}"
        )

    async def _snapshot_loop(self, guild_id: int) -> None:
        try:
            while True:
                deadline = self._snapshot_deadlines.get(guild_id)
                if deadline is None:
                    break
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                self._snapshot_deadlines.pop(guild_id, None)
                await self._write_snapshot(guild_id)
        finally:
            self._snapshot_tasks.pop(guild_id, None)

    async def _flush_snapshot(self, guild_id: int, *, entries: list[dict[str, Any]] | None = None) -> None:
        task = self._snapshot_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._snapshot_deadlines.pop(guild_id, None)
        await self._write_snapshot(guild_id, entries=entries)

    def _snapshot_entries(self, guild_id: int) -> list[dict[str, Any]]:
        player = self.players.get(guild_id)
        return player.snapshot() if player is not None else []

    async def _write_snapshot(self, guild_id: int, *, entries: list[dict[str, Any]] | None = None) -> None:
        if not self.bot.database.is_open:
            return
        snapshot = self._snapshot_entries(guild_id) if entries is None else entries
        await self.bot.database.save_queue_snapshot(guild_id, snapshot)
