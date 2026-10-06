from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


async def is_public_http_url(url: str) -> bool:
    """Return False if `url` is http(s) and its host resolves to a non-public address.

    User-supplied URLs are handed to yt-dlp (and resulting stream URLs to ffmpeg and a
    HEAD request), all of which would otherwise happily connect to loopback, private
    ranges or cloud metadata endpoints on the bot's host. Every resolved address must be
    globally routable.

    Limits, deliberately accepted: redirects followed inside yt-dlp/ffmpeg and DNS
    rebinding between this check and the real connection are not covered. If the name
    can't be resolved at all nothing can connect either, so that case returns True and
    the downstream fetch fails normally instead of this check masking it.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return True
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return True
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        try:
            if not ipaddress.ip_address(address).is_global:
                return False
        except ValueError:
            return False
    return True
