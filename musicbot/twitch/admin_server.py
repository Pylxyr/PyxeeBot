"""HTTP surface for the Twitch integration: the now-playing JSON endpoint for
overlays (title/artist/art — see relay.py's NowPlaying) plus a small settings
GUI at /settings for the request-limit tunables.

/settings is the only part of this that can change bot behavior, so unlike
/nowplaying.json it's optionally protected by HTTP Basic Auth — set
TWITCH_SETTINGS_PASSWORD to enable it. Left unset, the panel still works (this
stays bound to 127.0.0.1 by default, same as the now-playing endpoint), but a
warning is logged once at startup so that's a deliberate choice, not a silent
gap, if you ever widen TWITCH_NOWPLAYING_HOST beyond localhost.
"""

from __future__ import annotations

import base64
import hmac
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from aiohttp import web

from musicbot.twitch.tunables import TwitchTunables

if TYPE_CHECKING:
    from musicbot.database import Database
    from musicbot.twitch.relay import TwitchRadioRelay

log = logging.getLogger(__name__)


def _settings_page(tunables: TwitchTunables, *, saved: bool, broadcast_info: dict[str, str]) -> str:
    banner = (
        '<p class="saved">Saved — takes effect on the next !sr, no restart needed.</p>' if saved else ""
    )
    broadcast_rows = "".join(
        f"<tr><td>{label}</td><td><code>{value}</code></td></tr>"
        for label, value in broadcast_info.items()
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PyxeeBot — Twitch settings</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    background: #1a1a2e; color: #e8e8f0; font-family: -apple-system, "Segoe UI", sans-serif;
    max-width: 560px; margin: 40px auto; padding: 0 20px; line-height: 1.5;
  }}
  h1 {{ font-size: 1.4rem; font-weight: 600; margin-bottom: 4px; }}
  h2 {{ font-size: 1rem; font-weight: 600; color: #aaa; margin-top: 32px; }}
  p.subtitle {{ color: #9a9ab0; margin-top: 0; font-size: 0.9rem; }}
  form {{ background: #22223a; border-radius: 10px; padding: 20px; }}
  label {{ display: block; font-weight: 600; margin-top: 16px; margin-bottom: 2px; }}
  label:first-child {{ margin-top: 0; }}
  .desc {{ color: #9a9ab0; font-size: 0.85rem; margin: 0 0 6px; }}
  input[type=number] {{
    width: 100%; box-sizing: border-box; background: #16162a; border: 1px solid #3a3a5a;
    color: #e8e8f0; border-radius: 6px; padding: 8px 10px; font-size: 1rem;
  }}
  input[type=number]:focus {{ outline: 2px solid #6a6aff; border-color: transparent; }}
  button {{
    margin-top: 20px; background: #5a5aff; color: white; border: none; border-radius: 6px;
    padding: 10px 18px; font-size: 1rem; font-weight: 600; cursor: pointer;
  }}
  button:hover {{ background: #6a6aff; }}
  p.saved {{ color: #7ee787; font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  td {{ padding: 6px 0; border-bottom: 1px solid #2a2a4a; }}
  td:first-child {{ color: #9a9ab0; }}
  td:last-child {{ text-align: right; }}
  code {{ color: #c9c9e8; }}
</style>
</head>
<body>
<h1>Twitch request settings</h1>
<p class="subtitle">Changes apply immediately — no restart, no redeploy.</p>
{banner}
<form method="post" action="/settings">
  <label for="max_pending_per_chatter">Max requests queued per viewer</label>
  <p class="desc">How many songs the same viewer can have waiting in the queue at once.</p>
  <input type="number" id="max_pending_per_chatter" name="max_pending_per_chatter"
         min="1" max="20" value="{tunables.max_pending_per_chatter}" required>

  <label for="request_cooldown_seconds">Cooldown between requests (seconds)</label>
  <p class="desc">How long a viewer must wait after !sr before their next one. 0 disables this.</p>
  <input type="number" id="request_cooldown_seconds" name="request_cooldown_seconds"
         min="0" max="3600" value="{tunables.request_cooldown_seconds}" required>

  <label for="queue_cap">Max total queue size</label>
  <p class="desc">Across all viewers combined — !sr is rejected once the queue hits this.</p>
  <input type="number" id="queue_cap" name="queue_cap"
         min="1" max="200" value="{tunables.queue_cap}" required>

  <label for="max_request_duration_seconds">Max song length (seconds)</label>
  <p class="desc">Longer requests are rejected outright. Live streams are always rejected (no fixed length).</p>
  <input type="number" id="max_request_duration_seconds" name="max_request_duration_seconds"
         min="30" max="3600" value="{tunables.max_request_duration_seconds}" required>

  <button type="submit">Save</button>
</form>

<h2>Broadcast settings (set via .env — restart required to change)</h2>
<table>{broadcast_rows}</table>
</body>
</html>"""


def _check_basic_auth(request: web.Request, password: str) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        _, _, supplied = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(supplied, password)


def build_admin_app(
    *,
    relay: "TwitchRadioRelay",
    tunables: TwitchTunables,
    database: "Database",
    settings_password: str | None,
    broadcast_info: dict[str, str],
) -> web.Application:
    if not settings_password:
        log.warning(
            "TWITCH_SETTINGS_PASSWORD is not set — the /settings panel has no auth. "
            "Fine if it's only reachable via 127.0.0.1 (the default); set a password "
            "before widening TWITCH_NOWPLAYING_HOST beyond localhost."
        )

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

    async def handle_settings_get(request: web.Request) -> web.Response:
        saved = request.query.get("saved") == "1"
        html = _settings_page(tunables, saved=saved, broadcast_info=broadcast_info)
        return web.Response(text=html, content_type="text/html")

    async def handle_settings_post(request: web.Request) -> web.Response:
        form = await request.post()
        try:
            candidate = TwitchTunables(
                max_pending_per_chatter=int(form["max_pending_per_chatter"]),  # type: ignore[arg-type]
                request_cooldown_seconds=int(form["request_cooldown_seconds"]),  # type: ignore[arg-type]
                queue_cap=int(form["queue_cap"]),  # type: ignore[arg-type]
                max_request_duration_seconds=int(form["max_request_duration_seconds"]),  # type: ignore[arg-type]
            )
        except (KeyError, ValueError) as exc:
            raise web.HTTPBadRequest(text=f"Invalid form data: {exc}") from exc
        candidate.clamp()

        # Mutate the live object in place rather than replacing it — chatbot.py
        # holds a direct reference to this same instance, so this takes effect
        # for the very next !sr with no restart and no extra wiring needed.
        tunables.max_pending_per_chatter = candidate.max_pending_per_chatter
        tunables.request_cooldown_seconds = candidate.request_cooldown_seconds
        tunables.queue_cap = candidate.queue_cap
        tunables.max_request_duration_seconds = candidate.max_request_duration_seconds

        await database.set_twitch_tunables(**tunables.to_dict())
        # 303, not 302: explicitly tells the client "fetch the result with GET"
        # regardless of what method got you here. A 302 is ambiguous enough
        # that some clients re-POST to the redirect target instead of GETting
        # it — confirmed while testing this, where a naive redirect caused the
        # form to be resubmitted against itself instead of just showing the
        # saved page.
        raise web.HTTPSeeOther("/settings?saved=1")

    @web.middleware
    async def cors_middleware(
        request: web.Request, handler: "Callable[[web.Request], Awaitable[web.StreamResponse]]"
    ) -> web.StreamResponse:
        # Only the read-only overlay data needs to be fetch()-able cross-origin
        # from a StreamElements-hosted widget. /settings is a page a human opens
        # directly in a browser, not fetched cross-origin by anything, so it's
        # deliberately excluded from this — no reason to loosen CORS on the one
        # route that can change bot behavior.
        if request.method == "OPTIONS":
            response: web.StreamResponse = web.Response(status=204)
        else:
            response = await handler(request)
        if request.path in ("/nowplaying.json", "/healthz"):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return response

    @web.middleware
    async def auth_middleware(
        request: web.Request, handler: "Callable[[web.Request], Awaitable[web.StreamResponse]]"
    ) -> web.StreamResponse:
        if settings_password and request.path.startswith("/settings"):
            if not _check_basic_auth(request, settings_password):
                return web.Response(
                    status=401,
                    headers={"WWW-Authenticate": 'Basic realm="PyxeeBot Twitch settings"'},
                )
        return await handler(request)

    app = web.Application(middlewares=[cors_middleware, auth_middleware])
    app.router.add_get("/nowplaying.json", handle_nowplaying)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/settings", handle_settings_get)
    app.router.add_post("/settings", handle_settings_post)
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda _: web.Response(status=204))
    return app


async def run_admin_server(
    *,
    relay: "TwitchRadioRelay",
    tunables: TwitchTunables,
    database: "Database",
    settings_password: str | None,
    broadcast_info: dict[str, str],
    host: str,
    port: int,
) -> web.AppRunner:
    """Starts the server and returns the runner — caller owns its lifetime
    (call runner.cleanup() on shutdown)."""
    app = build_admin_app(
        relay=relay,
        tunables=tunables,
        database=database,
        settings_password=settings_password,
        broadcast_info=broadcast_info,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("Twitch now-playing endpoint: http://%s:%d/nowplaying.json", host, port)
    log.info("Twitch settings panel:       http://%s:%d/settings", host, port)
    return runner
