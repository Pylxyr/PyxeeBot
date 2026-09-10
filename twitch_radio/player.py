from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from twitch_radio.models import Track

log = logging.getLogger(__name__)

AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
_CHUNK_DURATION = 0.1
_CHUNK_BYTES = int(AUDIO_RATE * AUDIO_CHANNELS * 2 * _CHUNK_DURATION)
_SILENCE_CHUNK = b"\x00" * _CHUNK_BYTES
_STREAM_CHUNK_BYTES = 8192
_SUBSCRIBER_QUEUE_SIZE = 50  # ~5-10s of MP3 at typical bitrates; a stalled listener gets dropped, not buffered forever

_MIN_BACKOFF = 5.0
_MAX_BACKOFF = 300.0
_STABLE_UPTIME_SECONDS = 30.0
_DECODER_START_TIMEOUT = 20.0


class TrackResolver(Protocol):
    async def __call__(self, query: str, requester_id: int) -> Track | None: ...


@dataclass(slots=True)
class QueuedRequest:
    webpage_url: str
    requester_id: int
    requester_name: str
    title: str = ""
    cancelled: bool = False
    on_start: Callable[[], None] | None = field(default=None, repr=False)


@dataclass(slots=True)
class NowPlaying:
    title: str
    uploader: str
    thumbnail_url: str | None
    requester_name: str
    requester_id: int
    webpage_url: str
    started_at: float
    duration: int


class RadioPlayer:
    """Owns one persistent ffmpeg encoder producing a continuous MP3 stream
    from resolved tracks + silence between them, fanned out to any number of
    HTTP subscribers (see subscribe()/unsubscribe()) — e.g. an OBS Media
    Source on the streamer's own machine. Nothing is pushed anywhere on its
    own; playback only happens where something is actually listening.
    """

    def __init__(
        self, *, resolver: TrackResolver, audio_bitrate_kbps: int, pause_when_no_listeners: bool = False
    ) -> None:
        self._resolver = resolver
        self._audio_bitrate_kbps = audio_bitrate_kbps
        self._pause_when_no_listeners = pause_when_no_listeners

        self._queue: asyncio.Queue[QueuedRequest] = asyncio.Queue()
        self._pending: list[QueuedRequest] = []
        self._task: asyncio.Task[None] | None = None
        self._encoder: asyncio.subprocess.Process | None = None
        self._encoder_spawned_at: float = 0.0
        self._backoff = _MIN_BACKOFF
        self._backoff_reset_done = False
        self._current_decoder: asyncio.subprocess.Process | None = None
        self._now_playing: NowPlaying | None = None
        # The request currently occupying the player's one "slot" — set the
        # instant it's dequeued, cleared when it's done playing (or fails).
        # now_playing alone isn't enough to identify it: it stays None for
        # the whole resolve/decoder-startup window (which the README notes
        # can be 15-20s cold, up to YTDLP_EXTRACT_TIMEOUT_SECONDS on a slow
        # one) — active_requester_id below covers that gap too, so a
        # chatter can !skip their own song before it's technically
        # "playing" yet, not just after.
        self._active_request: QueuedRequest | None = None
        self._stopping = False
        self._resolving = False
        self._skip_pending = False
        self._notify_failure: Callable[[str], Awaitable[None]] | None = None
        self._duration_limit_getter: Callable[[], Awaitable[int]] | None = None
        self._subscribers: set[asyncio.Queue[bytes]] = set()
        # Unique voter IDs for a !voteskip on whatever's currently active —
        # cleared every time a new request becomes active (see _play_one),
        # so votes never carry over from one song to the next.
        self._skip_votes: set[int] = set()
        # Pub-sub for "something about now-playing/queue changed" — mirrors
        # the _subscribers pattern above but carries no payload (just a
        # wakeup); consumers (the admin server's /ws/nowplaying) re-fetch
        # full current state themselves rather than this class trying to
        # track per-connection what's already been sent.
        self._state_subscribers: set[asyncio.Queue[None]] = set()

    # -- public interface used by the chat bot / admin server ------------

    @property
    def now_playing(self) -> NowPlaying | None:
        return self._now_playing

    @property
    def active_requester_id(self) -> int | None:
        """Who the player's current "slot" belongs to — playing or still
        resolving/loading — or None if it's idle. Lets the chat bot's !skip
        allow a chatter to skip their own song during the resolve window
        too, not just once now_playing is actually set."""
        if self._now_playing is not None:
            return self._now_playing.requester_id
        if self._active_request is not None:
            return self._active_request.requester_id
        return None

    def queue_size(self) -> int:
        return self._queue.qsize()

    def queued_items(self) -> list[QueuedRequest]:
        return list(self._pending)

    def positions_for(self, requester_id: int) -> list[int]:
        """1-indexed queue positions, in play order, for every one of
        requester_id's requests still waiting (not yet dequeued) — empty if
        they have none waiting (whether because they have nothing queued,
        or because their one request is the one currently active — check
        active_requester_id for that case)."""
        return [i + 1 for i, r in enumerate(self._pending) if r.requester_id == requester_id]

    def enqueue(self, request: QueuedRequest) -> None:
        self._queue.put_nowait(request)
        self._pending.append(request)
        self._notify_state_changed()

    def set_track_failure_notifier(self, notifier: Callable[[str], Awaitable[None]] | None) -> None:
        self._notify_failure = notifier

    def set_duration_limit_getter(self, getter: Callable[[], Awaitable[int]] | None) -> None:
        self._duration_limit_getter = getter

    def subscribe(self) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        self._subscribers.discard(q)

    def subscribe_state(self) -> asyncio.Queue[None]:
        """A queue that receives a wakeup (no payload — just call
        active_requester_id/now_playing/queued_items() again) every time
        now-playing or the queue changes. Small maxsize is fine: consumers
        only care that *something* changed, not how many times, and a full
        queue just means a wakeup is already pending."""
        q: asyncio.Queue[None] = asyncio.Queue(maxsize=4)
        self._state_subscribers.add(q)
        return q

    def unsubscribe_state(self, q: asyncio.Queue[None]) -> None:
        self._state_subscribers.discard(q)

    def _notify_state_changed(self) -> None:
        for q in list(self._state_subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)

    def cancel_pending_for(self, requester_id: int) -> QueuedRequest | None:
        for request in reversed(self._pending):
            if request.requester_id == requester_id and not request.cancelled:
                request.cancelled = True
                with contextlib.suppress(ValueError):
                    self._pending.remove(request)
                if request.on_start is not None:
                    with contextlib.suppress(Exception):
                        request.on_start()
                    request.on_start = None
                self._notify_state_changed()
                return request
        return None

    def skip_current(self) -> bool:
        if self._current_decoder is not None:
            with contextlib.suppress(ProcessLookupError):
                self._current_decoder.kill()
            return True
        if self._resolving:
            self._skip_pending = True
            return True
        return False

    def register_skip_vote(self, voter_id: int, threshold: int) -> tuple[bool, int, bool] | None:
        """Registers one vote to skip whatever's currently active (playing
        or still resolving). Returns (skipped, vote_count, is_new_vote), or
        None if nothing's currently active to vote on. `skipped` is True if
        this vote reached `threshold` and triggered an actual skip (votes
        are cleared immediately in that case — same-track revoting starts
        fresh). `is_new_vote` is False if this voter had already voted for
        this same track (still reports the current count either way, so a
        repeat !voteskip isn't a dead end for the caller)."""
        if self.active_requester_id is None:
            return None
        is_new = voter_id not in self._skip_votes
        self._skip_votes.add(voter_id)
        count = len(self._skip_votes)
        if count >= threshold:
            self.skip_current()
            self._skip_votes.clear()
            return True, count, is_new
        return False, count, is_new

    async def _notify(self, message: str) -> None:
        if self._notify_failure is None:
            return
        with contextlib.suppress(Exception):
            await self._notify_failure(message)

    def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found on PATH — required to run the radio player.")
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever(), name="radio-player")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._kill_encoder()

    # -- internals ---------------------------------------------------------

    async def _kill_encoder(self) -> None:
        if self._current_decoder is not None:
            with contextlib.suppress(ProcessLookupError):
                self._current_decoder.kill()
            self._current_decoder = None
        if self._encoder is not None:
            with contextlib.suppress(ProcessLookupError):
                self._encoder.kill()
            with contextlib.suppress(Exception):
                await self._encoder.wait()
            self._encoder = None

    async def _run_forever(self) -> None:
        self._backoff = _MIN_BACKOFF
        while not self._stopping:
            try:
                await self._run_one_session()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Audio encoder session ended — restarting in %.0fs", self._backoff)
            finally:
                await self._kill_encoder()
            if self._stopping:
                return
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, _MAX_BACKOFF)

    async def _run_one_session(self) -> None:
        await self._spawn_encoder()
        self._encoder_spawned_at = time.monotonic()
        self._backoff_reset_done = False
        feed_task = asyncio.create_task(self._feed_loop())
        pump_task = asyncio.create_task(self._pump_encoder_output())
        try:
            done, pending = await asyncio.wait({feed_task, pump_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()
        finally:
            feed_task.cancel()
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(feed_task, pump_task, return_exceptions=True)

    async def _spawn_encoder(self) -> None:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS), "-i", "-",
            "-c:a", "libmp3lame", "-b:a", f"{self._audio_bitrate_kbps}k",
            "-id3v2_version", "0", "-write_xing", "0",  # no tags/duration header on an infinite live stream
            "-f", "mp3", "-",
        ]
        self._encoder = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
        )
        log.info("Audio encoder started (%dkbps MP3).", self._audio_bitrate_kbps)

    async def _pump_encoder_output(self) -> None:
        assert self._encoder is not None and self._encoder.stdout is not None
        stdout = self._encoder.stdout
        while True:
            chunk = await stdout.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                # EOF on the encoder's own stdout — the ffmpeg process
                # itself has exited (crashed, OOM-killed, etc), not just a
                # normal graceful shutdown: during stop(), this task gets
                # cancelled directly by _run_one_session()'s finally block
                # (see below) before a read like this would ever return
                # empty, so reaching here at all means something killed the
                # encoder out from under us. Raising — instead of returning
                # cleanly — makes _run_one_session()/_run_forever() treat
                # this the same as any other encoder failure: logged with
                # exc_info and subject to the backoff/restart loop. Returning
                # cleanly here (the previous behavior) meant a crashed
                # encoder just silently respawned with no log line at all
                # explaining why — indistinguishable in the logs from
                # nothing having gone wrong.
                returncode = self._encoder.returncode if self._encoder is not None else None
                raise RuntimeError(f"Encoder stdout closed unexpectedly (exit code {returncode})")
            for q in list(self._subscribers):
                try:
                    q.put_nowait(chunk)
                except asyncio.QueueFull:
                    # Dropping this subscriber — but its handle_stream()
                    # HTTP handler is still `await queue.get()`-ing, and
                    # nothing will ever be pushed to this queue again once
                    # it's out of self._subscribers. Without waking it, that
                    # coroutine (and its socket) hangs forever instead of
                    # closing. Evict one old chunk to make room, then queue
                    # an empty-bytes sentinel — never produced by a real
                    # read here — that handle_stream() treats as "stop".
                    self._subscribers.discard(q)
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        q.put_nowait(b"")

    async def _feed_loop(self) -> None:
        assert self._encoder is not None and self._encoder.stdin is not None
        encoder_stdin = self._encoder.stdin
        while not self._stopping:
            if self._encoder.returncode is not None:
                raise RuntimeError(f"Encoder exited with code {self._encoder.returncode}")
            if not self._backoff_reset_done and time.monotonic() - self._encoder_spawned_at >= _STABLE_UPTIME_SECONDS:
                self._backoff = _MIN_BACKOFF
                self._backoff_reset_done = True
            try:
                if self._pause_when_no_listeners and not self._subscribers:
                    # Track-boundary pause only — deliberately not trying to
                    # pause mid-track (that would mean pausing the decoder
                    # subprocess and the real-time write pacing in
                    # _play_one_inner without leaving stream state
                    # inconsistent, which is a lot more moving parts for a
                    # narrow benefit). A track already playing when the last
                    # listener disconnects always finishes normally; this
                    # only holds off *starting the next one* until someone's
                    # listening again — checked fresh every loop tick, so it
                    # resumes on its own the moment a subscriber (re)connects,
                    # no separate signal needed.
                    await self._write_silence_chunk(encoder_stdin)
                    await asyncio.sleep(_CHUNK_DURATION)
                    continue
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                await self._write_silence_chunk(encoder_stdin)
                await asyncio.sleep(_CHUNK_DURATION)
                continue
            with contextlib.suppress(ValueError):
                self._pending.remove(request)
            if request.cancelled:
                continue
            if request.on_start is not None:
                with contextlib.suppress(Exception):
                    request.on_start()
            await self._play_one(request, encoder_stdin)

    async def _write_silence_chunk(self, encoder_stdin: asyncio.StreamWriter) -> None:
        encoder_stdin.write(_SILENCE_CHUNK)
        await encoder_stdin.drain()

    async def _trickle_silence_until_cancelled(self, encoder_stdin: asyncio.StreamWriter) -> None:
        try:
            while True:
                await self._write_silence_chunk(encoder_stdin)
                await asyncio.sleep(_CHUNK_DURATION)
        except asyncio.CancelledError:
            raise

    async def _play_one(self, request: QueuedRequest, encoder_stdin: asyncio.StreamWriter) -> None:
        self._active_request = request
        self._skip_votes.clear()
        self._notify_state_changed()
        try:
            await self._play_one_inner(request, encoder_stdin)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error playing queued request: %s", request.webpage_url)
        finally:
            self._active_request = None
            self._notify_state_changed()

    async def _current_duration_limit(self) -> int:
        if self._duration_limit_getter is None:
            return 0
        with contextlib.suppress(Exception):
            return await self._duration_limit_getter()
        return 0

    async def _play_one_inner(self, request: QueuedRequest, encoder_stdin: asyncio.StreamWriter) -> None:
        self._skip_pending = False
        silence_task = asyncio.create_task(
            self._trickle_silence_until_cancelled(encoder_stdin), name="radio-player-prefeed-silence"
        )
        decoder: asyncio.subprocess.Process | None = None
        first_chunk = b""
        self._resolving = True
        try:
            try:
                track = await self._resolver(request.webpage_url, request.requester_id)
            except Exception:
                log.exception("Failed to re-resolve queued request: %s", request.webpage_url)
                await self._notify(f"Couldn't load {request.requester_name}'s song — skipping it.")
                return
            if track is None:
                log.warning("Re-resolve returned nothing for %s — skipping", request.webpage_url)
                await self._notify(f"Couldn't load {request.requester_name}'s song — skipping it.")
                return
            if track.is_live:
                log.warning("Re-resolve found %s is now live — skipping", request.webpage_url)
                await self._notify(f"Skipped {request.requester_name}'s song — it's a livestream now.")
                return
            duration_limit = await self._current_duration_limit()
            if 0 < duration_limit < track.duration:
                log.warning("Re-resolve found %s now exceeds the duration cap — skipping", request.webpage_url)
                await self._notify(f"Skipped {request.requester_name}'s song — it's too long to play now.")
                return
            if self._skip_pending:
                self._skip_pending = False
                log.info("Skipped %s before it started playing (mid-resolve skip).", track.title)
                return

            self._now_playing = NowPlaying(
                title=track.title,
                uploader=track.uploader,
                thumbnail_url=track.thumbnail_url,
                requester_name=request.requester_name,
                requester_id=request.requester_id,
                webpage_url=track.webpage_url,
                started_at=time.monotonic(),
                duration=track.duration,
            )
            self._notify_state_changed()
            log.info("Now playing: %s (requested by %s)", track.title, request.requester_name)

            decoder = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-i", track.stream_url,
                "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS), "-",
                stdout=asyncio.subprocess.PIPE,
            )
            self._current_decoder = decoder
            if self._skip_pending:
                self._skip_pending = False
                log.info("Skipped %s right after its decoder started (mid-spawn skip).", track.title)
                with contextlib.suppress(ProcessLookupError):
                    decoder.kill()
                await decoder.wait()
                self._current_decoder = None
                self._now_playing = None
                decoder = None
                return
            assert decoder.stdout is not None
            stdout = decoder.stdout
            try:
                first_chunk = await asyncio.wait_for(stdout.read(_CHUNK_BYTES), timeout=_DECODER_START_TIMEOUT)
            except TimeoutError:
                log.warning("Timed out waiting for decoder output for %s — skipping.", request.webpage_url)
                await self._notify(f"Skipped {request.requester_name}'s song — it took too long to start.")
                with contextlib.suppress(ProcessLookupError):
                    decoder.kill()
                await decoder.wait()
                self._current_decoder = None
                self._now_playing = None
                decoder = None
                return
        finally:
            self._resolving = False
            silence_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await silence_task

        if decoder is None:
            return
        try:
            chunk = first_chunk
            while chunk:
                encoder_stdin.write(chunk)
                await encoder_stdin.drain()
                chunk = await stdout.read(_CHUNK_BYTES)
        finally:
            with contextlib.suppress(ProcessLookupError):
                decoder.kill()
            await decoder.wait()
            self._current_decoder = None
            self._now_playing = None
