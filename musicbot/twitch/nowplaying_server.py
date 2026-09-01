"""Tiny HTTP JSON endpoint for "now playing" overlays.

This is deliberately separate from the RTMP video feed (see relay.py) — the
broadcast video itself is a static image that never changes, and the actual
"Song — Artist" + album art overlay is meant to be rendered by an OBS Browser
Source or a StreamElements custom widget that polls this endpoint, exactly the
way most "24/7 radio" Twitch setups already work. That split is what makes the
RTMP video cheap: the encoder never has to re-render a frame when a track
changes, only this JSON blob changes.

Resource footprint: one aiohttp route on the same event loop the rest of the
bot already runs — no extra process, negligible RAM (a handful of KB per
request, nothing held in memory beyond the current NowPlaying dataclass).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from musicbot.twitch.relay import TwitchRadioRelay

log = logging.getLogger(__name__)


def build_nowplaying_app(relay: "TwitchRadioRelay") -> web.Application:
    async def handle_nowplaying(_: web.Request) -> web.Response:
        np = relay.now_playing
        if np is None:
            return web.json_response(
                {
                    "playing": False,
                    "title": None,
                    "artist": None,
                    "thumbnail_url": None,
                    "requester": None,
                    "elapsed_seconds": 0,
                    "duration_seconds": 0,
                    "queue_length": relay.queue_length,
                }
            )
        return web.json_response(
            {
                "playing": True,
                "title": np.title,
                "artist": np.uploader,
                "thumbnail_url": np.thumbnail_url,
                "requester": np.requester_name,
                "webpage_url": np.webpage_url,
                "elapsed_seconds": round(np.elapsed_seconds, 1),
                "duration_seconds": np.duration,
                "queue_length": relay.queue_length,
            }
        )

    async def handle_health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/nowplaying.json", handle_nowplaying)
    app.router.add_get("/healthz", handle_health)
    return app


async def run_nowplaying_server(relay: "TwitchRadioRelay", *, host: str, port: int) -> web.AppRunner:
    """Starts the server and returns the runner — caller owns its lifetime
    (call runner.cleanup() on shutdown)."""
    app = build_nowplaying_app(relay)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("Twitch now-playing endpoint listening on http://%s:%d/nowplaying.json", host, port)
    return runner
