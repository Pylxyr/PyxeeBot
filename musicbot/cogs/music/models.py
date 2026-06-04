"""models.py — Pure data classes. No discord/bot imports."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Track:
    title:         str
    webpage_url:   str
    stream_url:    str
    uploader:      str
    duration:      int
    requester_id:  int
    query:         str
    thumbnail_url: str       = ""
    resolved_at:   float     = 0.0
    tags:          list[str] = field(default_factory=list)

    @property
    def duration_label(self) -> str:
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes   = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


@dataclass(slots=True)
class ResolvedTrackData:
    title:         str
    webpage_url:   str
    stream_url:    str
    uploader:      str
    duration:      int
    query:         str
    resolved_at:   float
    thumbnail_url: str       = ""
    tags:          list[str] = field(default_factory=list)


@dataclass(slots=True)
class NowPlayingController:
    """Stores only channel_id + message_id as integers (no full Message object).

    Fix #6: avoids keeping a full discord.Message (embed payload, attachment
    data, member caches) alive for every guild that ever ran !np.
    """
    channel_id:  int
    message_id:  int
    expires_at:  float
    status_text: str = ""


@dataclass(slots=True)
class SearchQueryContext:
    normalized_query: str
    raw_query_tokens: list[str]
    query_tokens:     list[str]
    query_token_set:  set[str]
    anchor_phrases:   list[str]
    intent:           dict[str, bool] = field(default_factory=dict)


@dataclass(slots=True)
class SearchEntryContext:
    item:                 dict[str, Any]
    normalized_title:     str
    normalized_uploader:  str
    normalized_metadata:  str
    title_tokens:         list[str]
    uploader_tokens:      list[str]
    metadata_tokens:      list[str]
    title_token_set:      set[str]
    uploader_token_set:   set[str]
    metadata_token_set:   set[str]
    duration:             int
    view_count:           int
    channel_is_verified:  bool


@dataclass(slots=True)
class ScoreBreakdown:
    rank:                    int
    title:                   str
    uploader:                str
    webpage_url:             str
    duration:                int
    final_score:             float
    title_overlap:           float
    uploader_overlap:        float
    ratio:                   float
    topic_bonus:             float
    uploader_pref_bonus:     float
    anchor_score:            float
    artist_match_bonus:      float
    artist_completion_bonus: float
    title_uploader_synergy:  float
    preferred_bonus:         float
    discouraged_penalty:     float
    duration_bonus:          float
    jp_original_bonus:       float = 0.0
    view_bonus:              float = 0.0
    verified_bonus:          float = 0.0
    selected:                bool  = False


@dataclass(slots=True)
class SearchDebugRecord:
    query_text: str
    guild_id:   int
    timestamp:  float
    candidates: list[ScoreBreakdown]
