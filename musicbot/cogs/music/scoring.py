from __future__ import annotations

import logging
import math
import re
import time
from collections import OrderedDict
from datetime import date as _date
from functools import lru_cache
from typing import Any
from rapidfuzz import fuzz as _fuzz

from musicbot.cogs.music.constants import (
    _ANIME_INTENT_RE,
    _BRACKET_STRIP_RE,
    _CJK_RE,
    _DASH_SEPARATED_RE,
    _HANGUL_RE,
    _JP_COVER_BRACKET_RE,
    SEARCH_ANIME_SIGNAL_TOKENS,
    SEARCH_CURATION_EXTRA_PHRASES,
    SEARCH_CURATION_EXTRA_TOKENS,
    SEARCH_DISCOURAGED_PHRASES,
    SEARCH_DISCOURAGED_TOKENS,
    SEARCH_GENERIC_TOKENS,
    SEARCH_PREFERRED_PHRASES,
    SEARCH_PREFERRED_UPLOADER_TOKENS,
)
from musicbot.cogs.music.models import (
    ScoreBreakdown,
    SearchDebugRecord,
    SearchEntryContext,
    SearchQueryContext,
)

log = logging.getLogger(__name__)

@lru_cache(maxsize=4096)
def normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))

@lru_cache(maxsize=4096)
def tokenize_text(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))

def _wb(p: str, t: str) -> bool:
    return (
        p == t
        or t.startswith(p + " ")
        or t.endswith(" " + p)
        or (" " + p + " ") in t
    )

def signal_tokens(query: str) -> list[str]:
    tokens = list(tokenize_text(query))
    filtered = [
        t for t in tokens
        if t not in SEARCH_GENERIC_TOKENS or t in SEARCH_ANIME_SIGNAL_TOKENS
    ]
    return filtered or tokens

def detect_intent(query: str) -> dict[str, bool]:
    q = query.strip()
    return {
        "anime":       bool(_ANIME_INTENT_RE.search(q)),
        "dash_format": bool(_DASH_SEPARATED_RE.match(q)),
        "has_artist":  " " in q,
    }

def token_overlap_ratio(
    query_tokens: list[str],
    candidate: set[str] | list[str] | tuple[str, ...],
) -> float:
    if not query_tokens or not candidate:
        return 0.0
    cset = candidate if isinstance(candidate, set) else set(candidate)
    return sum(1 for t in query_tokens if t in cset) / len(query_tokens)

def _candidate_title_text(item: dict[str, Any]) -> str:
    return normalize_text(str(item.get("title") or ""))

def _candidate_uploader_text(item: dict[str, Any]) -> str:
    parts = [item.get("channel"), item.get("uploader"), item.get("artist"), item.get("creator")]
    uploader = " ".join(
        p.strip()
        for p in dict.fromkeys(str(p) for p in parts if isinstance(p, str) and p.strip())
    )
    return normalize_text(uploader)

def prepare_entry(item: dict[str, Any]) -> SearchEntryContext:
    normalized_title    = _candidate_title_text(item)
    normalized_uploader = _candidate_uploader_text(item)
    title_tokens    = list(tokenize_text(normalized_title))
    uploader_tokens = list(tokenize_text(normalized_uploader))
    metadata_tokens = title_tokens + uploader_tokens
    return SearchEntryContext(
        item=item,
        normalized_title=normalized_title,
        normalized_uploader=normalized_uploader,
        normalized_metadata=" ".join(p for p in (normalized_title, normalized_uploader) if p),
        title_tokens=title_tokens,
        uploader_tokens=uploader_tokens,
        metadata_tokens=metadata_tokens,
        title_token_set=set(title_tokens),
        uploader_token_set=set(uploader_tokens),
        metadata_token_set=set(metadata_tokens),
        duration=int(item.get("duration") or 0),
        view_count=int(item.get("view_count") or 0),
        channel_is_verified=bool(item.get("channel_is_verified", False)),
        upload_date=str(item.get("upload_date") or ""),
    )

@lru_cache(maxsize=512)
def _derive_anchor_phrases_cached(
    query_tokens: tuple[str, ...],
    uploader_texts: tuple[str, ...],
) -> tuple[str, ...]:
    if not query_tokens or not uploader_texts:
        return ()
    max_phrase_size = min(3, len(query_tokens))

    if len(uploader_texts) == 1:
        single = uploader_texts[0]
        for size in range(max_phrase_size, 0, -1):
            phrases: list[str] = []
            seen: set[str] = set()
            for start in range(len(query_tokens) - size + 1):
                phrase = " ".join(query_tokens[start: start + size])
                if phrase in seen:
                    continue
                seen.add(phrase)
                if phrase in single:
                    phrases.append(phrase)
            if phrases:
                return tuple(phrases[:4])
        return ()

    for size in range(max_phrase_size, 0, -1):
        matches: list[tuple[int, str]] = []
        seen: set[str] = set()
        for start in range(len(query_tokens) - size + 1):
            phrase = " ".join(query_tokens[start: start + size])
            if phrase in seen:
                continue
            seen.add(phrase)
            if size == 1 and len(phrase) <= 2:
                continue
            count = sum(1 for text in uploader_texts if _wb(phrase, text))
            if 0 < count < len(uploader_texts):
                matches.append((count, phrase))
        if matches:
            matches.sort(key=lambda pair: (pair[0], pair[1]))
            best = matches[0][0]
            return tuple(phrase for cnt, phrase in matches if cnt == best)[:4]
    return ()

def derive_anchor_phrases(
    query_tokens: list[str],
    entries: list[SearchEntryContext],
) -> list[str]:
    uploader_texts = tuple(e.normalized_uploader for e in entries if e.normalized_uploader)
    return list(_derive_anchor_phrases_cached(tuple(query_tokens), uploader_texts))

def build_query_context(
    search_text: str,
    entries: list[SearchEntryContext],
) -> SearchQueryContext | None:
    if not search_text:
        return None
    q_tokens   = signal_tokens(search_text)
    normalized = normalize_text(search_text)
    if not normalized:
        return None
    return SearchQueryContext(
        normalized_query=normalized,
        raw_query_tokens=list(tokenize_text(search_text)),
        query_tokens=q_tokens,
        query_token_set=set(q_tokens),
        anchor_phrases=derive_anchor_phrases(q_tokens, entries),
        intent=detect_intent(search_text),
    )

def score_anchor_match(
    entry: SearchEntryContext,
    anchor_phrases: list[str],
) -> float:
    if not anchor_phrases or not entry.normalized_metadata:
        return 0.0
    uploader_matches = [p for p in anchor_phrases if _wb(p, entry.normalized_uploader)]
    if uploader_matches:
        longest = max(len(p.split()) for p in uploader_matches)
        return 1.05 + ((longest - 1) * 0.20)
    title_only = [
        p for p in anchor_phrases
        if _wb(p, entry.normalized_metadata) and not _wb(p, entry.normalized_uploader)
    ]
    if title_only:
        longest = max(len(p.split()) for p in title_only)
        return 0.20 + ((longest - 1) * 0.10)
    return -0.30

def score_entry(
    query: SearchQueryContext,
    entry: SearchEntryContext,
    *,
    breakdown: dict[str, float] | None = None,
    curation_mode: bool = False,
) -> float:
    if not query.normalized_query or not entry.normalized_metadata:
        return 0.0

    is_anime_query = query.intent.get("anime", False)
    is_dash_query  = query.intent.get("dash_format", False)

    title_overlap    = token_overlap_ratio(query.query_tokens, entry.title_token_set)
    uploader_overlap = token_overlap_ratio(query.query_tokens, entry.uploader_token_set)
    metadata_overlap = token_overlap_ratio(query.query_tokens, entry.metadata_token_set)

    missing_title_tokens = [t for t in query.query_tokens if t not in entry.title_token_set]
    missing_title_uploader_overlap = token_overlap_ratio(missing_title_tokens, entry.uploader_token_set)

    ratio          = _fuzz.ratio(query.normalized_query, entry.normalized_title) / 100.0
    metadata_ratio = _fuzz.partial_ratio(query.normalized_query, entry.normalized_metadata) / 100.0

    exact_metadata_match      = 1.0 if query.normalized_query in entry.normalized_metadata else 0.0
    metadata_prefix_match     = 1.0 if entry.normalized_metadata.startswith(query.normalized_query) else 0.0
    all_title_tokens_match    = 1.0 if query.query_token_set and query.query_token_set.issubset(entry.title_token_set) else 0.0
    all_metadata_tokens_match = 1.0 if query.query_token_set and query.query_token_set.issubset(entry.metadata_token_set) else 0.0

    artist_token_matches  = len(query.query_token_set & entry.uploader_token_set)
    artist_match_bonus    = 0.28 if artist_token_matches >= 2 else (0.12 if artist_token_matches == 1 else 0.0)
    strong_uploader_bonus = 0.18 if uploader_overlap >= 0.45 else 0.0
    topic_bonus = (0.55 if curation_mode else 0.30) if "topic" in entry.uploader_token_set else 0.0
    uploader_preference_bonus = sum(
        w for tok, w in SEARCH_PREFERRED_UPLOADER_TOKENS.items() if tok in entry.uploader_token_set
    )

    artist_completion_bonus = 0.0
    title_only_penalty      = 0.0
    if missing_title_tokens and title_overlap >= 0.45:
        artist_completion_bonus += missing_title_uploader_overlap * 0.90
        if missing_title_uploader_overlap >= 0.99:
            artist_completion_bonus += 0.45
        elif missing_title_uploader_overlap >= 0.50:
            artist_completion_bonus += 0.20
        elif title_overlap >= 0.75:
            title_only_penalty = 0.40
    elif not missing_title_tokens and uploader_overlap >= 0.20:
        artist_completion_bonus += 0.12

    title_uploader_synergy = 0.0
    if title_overlap >= 0.55 and uploader_overlap >= 0.20:
        title_uploader_synergy = 0.36
    elif title_overlap >= 0.75 and missing_title_tokens and missing_title_uploader_overlap >= 0.50:
        title_uploader_synergy = 0.24

    dash_format_bonus = 0.18 if is_dash_query and ratio >= 0.70 else 0.0
    preferred_bonus   = sum(w for phrase, w in SEARCH_PREFERRED_PHRASES.items() if phrase in entry.normalized_metadata)

    discouraged_penalty   = 0.0
    raw_query_token_set   = set(query.raw_query_tokens)

    for token, weight in SEARCH_DISCOURAGED_TOKENS.items():
        if token not in raw_query_token_set and token in entry.metadata_token_set:
            if is_anime_query and token in {"live", "stage", "concert"}:
                discouraged_penalty += weight * 0.3
            elif curation_mode and token in SEARCH_CURATION_EXTRA_TOKENS:
                discouraged_penalty += weight * 3.0
            else:
                discouraged_penalty += weight
    for phrase, weight in SEARCH_DISCOURAGED_PHRASES.items():
        if phrase not in query.normalized_query and phrase in entry.normalized_metadata:
            if is_anime_query and phrase == "tv size":
                continue
            discouraged_penalty += weight

    if curation_mode:
        norm_meta = entry.normalized_metadata
        for phrase in SEARCH_CURATION_EXTRA_PHRASES:
            if phrase not in query.normalized_query and phrase in norm_meta:
                discouraged_penalty += 0.65

    raw_title = str(entry.item.get("title") or "")
    if _JP_COVER_BRACKET_RE.search(raw_title):
        query_asks_cover = any(
            tok in raw_query_token_set
            for tok in ("guitar", "piano", "violin", "bass", "acoustic", "cover", "fingerstyle", "ukulele")
        )
        if not query_asks_cover:
            discouraged_penalty += 0.75

    if 90 <= entry.duration <= 600:
        duration_bonus = 0.10
    elif 60 <= entry.duration <= 660:
        duration_bonus = 0.05
    elif entry.duration > 900:
        duration_bonus = -0.12
    else:
        duration_bonus = 0.0

    anchor_score = score_anchor_match(entry, query.anchor_phrases)

    vc = entry.view_count
    _is_topic = "topic" in entry.uploader_token_set
    if vc >= 1000:
        view_bonus = min(0.35, (math.log10(vc) - 3.0) / 6.0 * 0.35)
    elif _is_topic:
        view_bonus = 0.05
    else:
        view_bonus = 0.0

    verified_bonus = 0.15 if entry.channel_is_verified else 0.0

    recency_bonus = 0.0
    ud = entry.upload_date
    if len(ud) == 8 and ud.isdigit() and discouraged_penalty < 0.50:
        try:
            uploaded = _date(int(ud[:4]), int(ud[4:6]), int(ud[6:8]))
            days_old = (_date.today() - uploaded).days
            if days_old <= 180:
                recency_bonus = 0.20
            elif days_old <= 365:
                recency_bonus = 0.12
            elif days_old <= 730:
                recency_bonus = 0.06
        except ValueError:
            pass

    jp_original_bonus = 0.0
    title_core = _BRACKET_STRIP_RE.sub("", raw_title).strip()
    if discouraged_penalty < 0.50 and _CJK_RE.search(title_core):
        latin_chars  = len(re.findall(r"[a-zA-Z]", title_core))
        total_chars  = len(title_core.replace(" ", ""))
        hangul_count = len(_HANGUL_RE.findall(title_core))
        cjk_count    = len(re.findall(r'[\u3040-\u30ff\u4e00-\u9fff]', title_core))
        latin_ratio  = latin_chars / total_chars if total_chars else 1.0
        is_jp = latin_ratio < 0.35 and (hangul_count == 0 or cjk_count > hangul_count * 1.5)
        if is_jp:
            jp_original_bonus = 0.55

    final = (
          (ratio          * 0.32)
        + (metadata_ratio * 0.20)
        + (title_overlap  * 0.44)
        + (uploader_overlap * 0.50)
        + (metadata_overlap * 0.36)
        + (exact_metadata_match     * 0.18)
        + (metadata_prefix_match    * 0.10)
        + (all_title_tokens_match   * 0.16)
        + (all_metadata_tokens_match * 0.24)
        + artist_match_bonus
        + strong_uploader_bonus
        + topic_bonus
        + uploader_preference_bonus
        + artist_completion_bonus
        + title_uploader_synergy
        + dash_format_bonus
        + preferred_bonus
        + duration_bonus
        + anchor_score
        + jp_original_bonus
        + view_bonus
        + verified_bonus
        + recency_bonus
        - title_only_penalty
        - discouraged_penalty
    )

    if breakdown is not None:
        breakdown.update({
            "title_overlap": title_overlap, "uploader_overlap": uploader_overlap,
            "ratio": ratio, "metadata_ratio": metadata_ratio,
            "topic_bonus": topic_bonus, "uploader_pref_bonus": uploader_preference_bonus,
            "anchor_score": anchor_score, "artist_match_bonus": artist_match_bonus,
            "strong_uploader_bonus": strong_uploader_bonus,
            "artist_completion_bonus": artist_completion_bonus,
            "title_uploader_synergy": title_uploader_synergy,
            "preferred_bonus": preferred_bonus, "discouraged_penalty": discouraged_penalty,
            "duration_bonus": duration_bonus, "jp_original_bonus": jp_original_bonus,
            "view_bonus": view_bonus, "verified_bonus": verified_bonus,
            "recency_bonus": recency_bonus, "final": final,
        })
    return final

def rank_entries(
    search_text: str,
    entries: list[dict[str, Any]],
    guild_id: int | None,
    last_search: OrderedDict[int, SearchDebugRecord],
    last_search_max: int,
    playlist_entry_url: Any,   # callable(item) -> str|None
    curation_mode: bool = False,
) -> list[dict[str, Any]]:
    """Score, sort, and return search result dicts in descending score order."""
    prepared = [(i, item, prepare_entry(item)) for i, item in enumerate(entries) if item]
    if not prepared:
        return []

    ctx = build_query_context(search_text, [p for _, _, p in prepared])
    if ctx is None:
        return [item for (_, item, _) in prepared]

    scored: list[tuple[float, int, dict[str, Any], SearchEntryContext, dict[str, float] | None]] = []
    need_debug = guild_id is not None
    for orig_i, item, ectx in prepared:
        bd: dict[str, float] | None = {} if need_debug else None
        sc = score_entry(ctx, ectx, breakdown=bd, curation_mode=curation_mode)
        scored.append((sc, orig_i, item, ectx, bd))
    scored.sort(key=lambda t: (t[0], -t[1]), reverse=True)

    if need_debug:
        records: list[ScoreBreakdown] = []
        for rank, (sc, _oi, item, ectx, bd) in enumerate(scored[:8], start=1):
            assert bd is not None
            records.append(ScoreBreakdown(
                rank=rank,
                title=str(item.get("title") or ""),
                uploader=str(item.get("uploader") or ectx.normalized_uploader),
                webpage_url=playlist_entry_url(item) or "",
                duration=ectx.duration,
                final_score=round(sc, 4),
                title_overlap=           round(bd.get("title_overlap", 0.0), 3),
                uploader_overlap=        round(bd.get("uploader_overlap", 0.0), 3),
                ratio=                   round(bd.get("ratio", 0.0), 3),
                topic_bonus=             round(bd.get("topic_bonus", 0.0), 3),
                uploader_pref_bonus=     round(bd.get("uploader_pref_bonus", 0.0), 3),
                anchor_score=            round(bd.get("anchor_score", 0.0), 3),
                artist_match_bonus=      round(bd.get("artist_match_bonus", 0.0), 3),
                artist_completion_bonus= round(bd.get("artist_completion_bonus", 0.0), 3),
                title_uploader_synergy=  round(bd.get("title_uploader_synergy", 0.0), 3),
                preferred_bonus=         round(bd.get("preferred_bonus", 0.0), 3),
                discouraged_penalty=     round(bd.get("discouraged_penalty", 0.0), 3),
                duration_bonus=          round(bd.get("duration_bonus", 0.0), 3),
                jp_original_bonus=       round(bd.get("jp_original_bonus", 0.0), 3),
                view_bonus=              round(bd.get("view_bonus", 0.0), 3),
                verified_bonus=          round(bd.get("verified_bonus", 0.0), 3),
            ))
        if records:
            records[0].selected = True
        last_search[guild_id] = SearchDebugRecord(
            query_text=search_text,
            guild_id=guild_id,
            timestamp=time.monotonic(),
            candidates=records,
        )
        last_search.move_to_end(guild_id)
        while len(last_search) > last_search_max:
            last_search.popitem(last=False)
        log.debug(
            "Search scores | guild=%s query=%r | %s",
            guild_id, search_text,
            " | ".join(f"[{r.rank}] {r.title!r} score={r.final_score}" for r in records),
        )

    return [item for (_, _, item, _, _) in scored]
