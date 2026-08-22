from __future__ import annotations

import re
from dataclasses import dataclass, field

_MD_ESCAPE_RE = re.compile(r"([\\*_`|~<>{}[\]()+#\-!])")


def _escape_md(text: str) -> str:
    return _MD_ESCAPE_RE.sub(r"\\\1", text)


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    stream_url: str
    uploader: str
    duration: int
    requester_id: int
    query: str
    thumbnail_url: str = ""
    resolved_at: float = 0.0
    tags: list[str] = field(default_factory=list)
    acodec: str = ""
    abr: float = 0.0

    _escaped_title: str | None = field(default=None, init=False, repr=False, compare=False)
    _escaped_uploader: str | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def escaped_title(self) -> str:
        if self._escaped_title is None:
            self._escaped_title = _escape_md(self.title)
        return self._escaped_title

    @property
    def escaped_uploader(self) -> str:
        if self._escaped_uploader is None:
            self._escaped_uploader = _escape_md(self.uploader or "Unknown")
        return self._escaped_uploader

    @property
    def duration_label(self) -> str:
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


@dataclass(slots=True)
class ResolvedTrackData:
    title: str
    webpage_url: str
    stream_url: str
    uploader: str
    duration: int
    query: str
    resolved_at: float
    thumbnail_url: str = ""
    tags: list[str] = field(default_factory=list)
    acodec: str = ""
    abr: float = 0.0


@dataclass(slots=True)
class NowPlayingController:
    channel_id: int
    message_id: int
    expires_at: float
    status_text: str = ""
    _last_render_key: tuple | None = field(default=None, init=False, repr=False, compare=False)
