"""Persistent RTMP relay for Twitch song requests.

Design summary (see the conversation / architecture writeup for the full reasoning):

- ONE ffmpeg muxer process is started and stays alive for the life of the stream.
  It reads video from a single looping static image (never changes — no per-track
  thumbnail is baked into the broadcast) and audio from its own stdin, and pushes
  H.264 + AAC over RTMP to Twitch. Track changes never restart this process, so the
  RTMP connection to Twitch is never interrupted between songs.
- A separate short-lived ffmpeg *decoder* process is spawned per track, purely to
  turn the resolved stream URL into raw PCM. Its stdout is piped straight into the
  muxer's still-open stdin. When one decoder hits EOF, the next one starts — from
  the muxer's point of view, its stdin just kept receiving bytes.
- When the queue is empty, digital silence is trickled into the muxer at the same
  pace real audio would arrive, so the stream never goes dark or times out.
- Requests are re-resolved (fresh yt-dlp extraction) right before they actually
  play, not when they're queued — a resolved stream URL can expire if it sits
  behind a long queue, and this mirrors how the existing Discord playback path
  already treats staleness (see STREAM_URL_REFRESH_AGE_SECONDS in constants.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from musicbot.cogs.music.models import Track

log = logging.getLogger(__name__)

AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
_CHUNK_SAMPLES = 4800  # 100ms per chunk at 48kHz
_CHUNK_BYTES = _CHUNK_SAMPLES * AUDIO_CHANNELS * 2  # 2 bytes/sample (s16le)
_CHUNK_DURATION = _CHUNK_SAMPLES / AUDIO_RATE  # 0.1s
_SILENCE_CHUNK = b"\x00" * _CHUNK_BYTES
_IDLE_POLL_INTERVAL = 5.0  # how often to log/refresh while genuinely idle long-term


class TrackResolver(Protocol):
    """Matches ExtractionMixin._extract_tracks's shape — the relay doesn't need
    to know anything about MusicCog beyond this one method."""

    async def __call__(
        self, query: str, requester_id: int, *, guild_id: int | None = None, limit: int = 1
    ) -> tuple[list["Track"], int]: ...


@dataclass(slots=True)
class QueuedRequest:
    """What actually sits in the relay's queue — deliberately NOT a resolved Track.
    Just enough to re-resolve fresh audio at play time and to show something in
    chat / the now-playing overlay immediately after !sr, before that re-resolve
    happens."""

    webpage_url: str
    title: str
    uploader: str
    thumbnail_url: str
    requester_name: str
    requester_id: int
    on_finished: Callable[[], None] | None = None
    """Called exactly once, whenever this request stops being "pending" for
    whatever reason — played to completion, skipped, or failed to resolve.
    Lets a caller (e.g. a per-chatter request cap) track in-flight requests
    without the relay needing to know anything about chatters or Discord
    users."""


@dataclass(slots=True)
class NowPlaying:
    title: str
    uploader: str
    thumbnail_url: str
    requester_name: str
    webpage_url: str
    started_at: float
    duration: int

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)


class TwitchRadioRelay:
    def __init__(
        self,
        *,
        ingest_url: str,
        stream_key: str,
        background_image: Path,
        resolver: TrackResolver,
        video_bitrate_kbps: int = 800,
        video_fps: int = 2,
    ) -> None:
        self._rtmp_url = f"{ingest_url.rstrip('/')}/{stream_key}"
        self._background_image = background_image
        self._resolver = resolver
        self._video_bitrate_kbps = video_bitrate_kbps
        self._video_fps = video_fps

        self._queue: asyncio.Queue[QueuedRequest] = asyncio.Queue()
        self._muxer: asyncio.subprocess.Process | None = None
        self._current_decoder: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._now_playing: NowPlaying | None = None
        self._stopping = False

    @property
    def now_playing(self) -> NowPlaying | None:
        return self._now_playing

    @property
    def queue_length(self) -> int:
        return self._queue.qsize()

    def skip_current(self) -> bool:
        """Kill the currently-playing track's decoder. _play_one's own read loop
        sees this as a normal EOF and moves on to the next queued request — it
        doesn't touch the muxer at all, so this never risks the RTMP connection
        itself. Returns False if nothing is currently playing."""
        if self._current_decoder is None:
            return False
        with contextlib.suppress(ProcessLookupError):
            self._current_decoder.kill()
        return True

    def enqueue(self, request: QueuedRequest) -> int:
        """Add a request to the queue. Returns its position (1 = next up)."""
        self._queue.put_nowait(request)
        return self._queue.qsize()

    def start(self) -> None:
        if self._task is not None:
            return
        if not self._background_image.is_file():
            raise FileNotFoundError(
                f"Twitch relay background image not found: {self._background_image}"
            )
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever(), name="twitch-radio-relay")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._kill_muxer()

    async def _run_forever(self) -> None:
        # Same "recycle on failure rather than take the whole bot down" approach
        # already used for the yt-dlp thread pool elsewhere in this codebase — a
        # crashed encoder or a dropped RTMP connection shouldn't need a bot restart.
        backoff = 1.0
        while not self._stopping:
            try:
                await self._spawn_muxer()
                await self._feed_loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Twitch relay muxer died unexpectedly — restarting")
            finally:
                await self._kill_muxer()
            if self._stopping:
                break
            log.info("Restarting Twitch relay muxer in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _spawn_muxer(self) -> None:
        gop = max(1, self._video_fps * 2)  # Twitch requires a 2-second keyframe interval
        self._muxer = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-loop", "1", "-i", str(self._background_image),
            "-thread_queue_size", "4096",
            "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS), "-i", "-",
            "-r", str(self._video_fps), "-g", str(gop), "-keyint_min", str(gop),
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
            "-threads", "1", "-pix_fmt", "yuv420p", "-b:v", f"{self._video_bitrate_kbps}k",
            "-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE),
            "-f", "flv", self._rtmp_url,
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log.info("Twitch relay muxer started (pid=%s)", self._muxer.pid)
        task = asyncio.create_task(self._drain_stderr(self._muxer), name="twitch-relay-stderr")
        task.add_done_callback(lambda _: None)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        with contextlib.suppress(Exception):
            async for line in proc.stderr:
                log.warning("ffmpeg[twitch-relay]: %s", line.decode(errors="replace").rstrip())

    async def _kill_muxer(self) -> None:
        proc, self._muxer = self._muxer, None
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
            proc.terminate()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    async def _feed_loop(self) -> None:
        assert self._muxer is not None and self._muxer.stdin is not None
        muxer_stdin = self._muxer.stdin
        while not self._stopping:
            try:
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                # Nothing requested — trickle silence at the same pace real audio
                # would arrive, so the muxer's stdin never starves and the live
                # mux keeps flowing instead of stalling between songs.
                muxer_stdin.write(_SILENCE_CHUNK)
                await muxer_stdin.drain()
                await asyncio.sleep(_CHUNK_DURATION)
                continue
            await self._play_one(request, muxer_stdin)

    async def _play_one(self, request: QueuedRequest, muxer_stdin: asyncio.StreamWriter) -> None:
        try:
            await self._play_one_inner(request, muxer_stdin)
        finally:
            if request.on_finished is not None:
                with contextlib.suppress(Exception):
                    request.on_finished()

    async def _play_one_inner(self, request: QueuedRequest, muxer_stdin: asyncio.StreamWriter) -> None:
        # Re-resolve now, not at !sr time — a stream URL resolved when this was
        # queued may have expired if it sat behind a long queue.
        try:
            tracks, _ = await self._resolver(request.webpage_url, request.requester_id, limit=1)
        except Exception:
            log.exception("Failed to re-resolve queued Twitch request: %s", request.webpage_url)
            return
        if not tracks:
            log.warning("Re-resolve returned nothing for %s — skipping", request.webpage_url)
            return
        track = tracks[0]

        self._now_playing = NowPlaying(
            title=track.title,
            uploader=track.uploader,
            thumbnail_url=track.thumbnail_url,
            requester_name=request.requester_name,
            webpage_url=track.webpage_url,
            started_at=time.monotonic(),
            duration=track.duration,
        )
        log.info("Twitch relay now playing: %s (requested by %s)", track.title, request.requester_name)

        decoder = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-re", "-i", track.stream_url,
            "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS), "-",
            stdout=asyncio.subprocess.PIPE,
        )
        self._current_decoder = decoder
        assert decoder.stdout is not None
        try:
            while True:
                chunk = await decoder.stdout.read(_CHUNK_BYTES)
                if not chunk:
                    break
                muxer_stdin.write(chunk)
                await muxer_stdin.drain()
        finally:
            with contextlib.suppress(ProcessLookupError):
                decoder.kill()
            await decoder.wait()
            self._current_decoder = None
            self._now_playing = None
