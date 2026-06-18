"""models.py — Pure data classes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_MD_ESCAPE_RE = re.compile(r"([\\*_`|~<>{}[\]()+#\-!])")


def _escape_md(text: str) -> str:
    """Lightweight markdown escaper (mirrors discord.utils.escape_markdown)."""
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
    acodec: str = ""  # e.g. "opus", "aac" — set by yt-dlp on full extract

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


@dataclass(slots=True)
class NowPlayingController:
    channel_id: int
    message_id: int
    expires_at: float
    status_text: str = ""
    _last_render_key: tuple | None = field(default=None, init=False, repr=False, compare=False)


@dataclass(slots=True)
class SearchQueryContext:
    normalized_query: str
    raw_query_tokens: list[str]
    query_tokens: list[str]
    query_token_set: set[str]
    anchor_phrases: list[str]
    intent: dict[str, bool] = field(default_factory=dict)


@dataclass(slots=True)
class SearchEntryContext:
    item: dict[str, Any]
    normalized_title: str
    normalized_uploader: str
    normalized_metadata: str
    title_tokens: list[str]
    uploader_tokens: list[str]
    metadata_tokens: list[str]
    title_token_set: set[str]
    uploader_token_set: set[str]
    metadata_token_set: set[str]
    duration: int
    view_count: int
    channel_is_verified: bool
    upload_date: str
    was_live: bool = False
    description_token_set: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True)
class ScoreBreakdown:
    rank: int
    title: str
    uploader: str
    webpage_url: str
    duration: int
    final_score: float
    title_overlap: float
    uploader_overlap: float
    ratio: float
    topic_bonus: float
    uploader_pref_bonus: float
    anchor_score: float
    artist_match_bonus: float
    artist_completion_bonus: float
    title_uploader_synergy: float
    preferred_bonus: float
    discouraged_penalty: float
    duration_bonus: float
    jp_original_bonus: float = 0.0
    view_bonus: float = 0.0
    verified_bonus: float = 0.0
    recency_bonus: float = 0.0
    strong_uploader_bonus: float = 0.0
    selected: bool = False


@dataclass(slots=True)
class SearchDebugRecord:
    query_text: str
    guild_id: int
    timestamp: float
    candidates: list[ScoreBreakdown]
