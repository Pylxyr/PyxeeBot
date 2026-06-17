from __future__ import annotations

import logging
import math
import re
import time
from collections import OrderedDict
from datetime import date as _date
from functools import lru_cache
from typing import Any, NamedTuple
from rapidfuzz import fuzz as _fuzz

from musicbot.cogs.music.constants import (
    _ANIME_INTENT_RE,
    _BRACKET_STRIP_RE,
    _CJK_RE,
    _KANA_RE,
    _DASH_SEPARATED_RE,
    _HANGUL_RE,
    _JP_COVER_BRACKET_RE,
    _JP_EVENT_FROM_RE,
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

# ---------------------------------------------------------------------------
# Scoring formula weights (multiplied against [0, 1] signal values)
# ---------------------------------------------------------------------------
_W_FUZZY_RATIO = 0.32
_W_METADATA_RATIO = 0.20
_W_TITLE_OVERLAP = 0.44
_W_UPLOADER_OVERLAP = 0.50
_W_METADATA_OVERLAP = 0.36
_W_EXACT_METADATA = 0.18
_W_PREFIX_MATCH = 0.10
_W_ALL_TITLE_TOKENS = 0.16
_W_ALL_METADATA_TOKENS = 0.24

# ---------------------------------------------------------------------------
# Thresholds used in conditional logic
# ---------------------------------------------------------------------------
_THR_UPLOADER_STRONG = 0.45  # uploader_overlap → strong_uploader_bonus
_THR_UPLOADER_WEAK = 0.20  # uploader_overlap → synergy / weak completion bonus
_THR_UPLOADER_FULL = 0.99  # missing tokens fully covered by uploader
_THR_UPLOADER_PARTIAL = 0.50  # partial uploader coverage of missing tokens
_THR_TITLE_MIN = 0.45  # minimum title_overlap to trigger completion logic
_THR_TITLE_HIGH = 0.75  # high title_overlap for penalty / partial synergy
_THR_TITLE_SYNERGY = 0.55  # title_overlap required for full synergy bonus
_THR_DASH_RATIO = 0.70  # fuzzy ratio required for dash-format bonus
_THR_PENALTY_GATE = 0.50  # above this, skip recency/JP bonuses
_MAX_DISCOURAGED_PENALTY = 0.65  # hard cap so a perfect title match can never rank last
_THR_JP_LATIN_RATIO = 0.35  # max latin-char ratio to qualify as JP original
_THR_JP_CJK_HANGUL = 1.5  # CJK must exceed hangul by this multiple for JP

# ---------------------------------------------------------------------------
# Duration windows (seconds)
# ---------------------------------------------------------------------------
_DUR_OK_MIN = 60
_DUR_IDEAL_MIN = 90
_DUR_IDEAL_MAX = 600
_DUR_OK_MAX = 660
_DUR_LONG = 900

# ---------------------------------------------------------------------------
# View-count bonus scaling
# ---------------------------------------------------------------------------
_VIEW_MIN = 1_000
_VIEW_BONUS_MAX = 0.35
_VIEW_BONUS_LOG_REF = 3.0  # log10(_VIEW_MIN) — zero point of the log scale
_VIEW_BONUS_LOG_RNG = 6.0  # log-scale range over which the bonus grows
_VIEW_BONUS_TOPIC = 0.05  # floor bonus for low-view topic channels

# ---------------------------------------------------------------------------
# Recency windows (days) and bonuses
# ---------------------------------------------------------------------------
_RECENCY_DAYS_NEW = 180
_RECENCY_DAYS_RECENT = 365
_RECENCY_DAYS_OLDER = 730
_RECENCY_BONUS_NEW = 0.20
_RECENCY_BONUS_RECENT = 0.12
_RECENCY_BONUS_OLDER = 0.06

# ---------------------------------------------------------------------------
# Anchor-match scores
# ---------------------------------------------------------------------------
_ANCHOR_UPLOADER_BASE = 1.05
_ANCHOR_UPLOADER_PER_WORD = 0.20
_ANCHOR_TITLE_BASE = 0.20
_ANCHOR_TITLE_PER_WORD = 0.10
_ANCHOR_NO_MATCH = -0.30

# ---------------------------------------------------------------------------
# Signal bonuses and penalties
# ---------------------------------------------------------------------------
_ARTIST_BONUS_MULTI = 0.28  # ≥2 artist tokens match
_ARTIST_BONUS_SINGLE = 0.12  # exactly 1 artist token matches
_STRONG_UPLOADER_BONUS = 0.18
_TOPIC_BONUS_NORMAL = 0.30
_TOPIC_BONUS_CURATION = 0.55
_COMPLETION_SCALE = 0.90  # continuous scale for missing-token uploader coverage
_COMPLETION_BONUS_FULL = 0.45  # all missing tokens covered by uploader
_COMPLETION_BONUS_PARTIAL = 0.20  # partial uploader coverage of missing tokens
_COMPLETION_BONUS_WEAK = 0.12  # no missing tokens but uploader present
_SYNERGY_BONUS_FULL = 0.36  # strong title + uploader overlap
_SYNERGY_BONUS_PARTIAL = 0.24  # high title overlap with partial uploader coverage
_DASH_FORMAT_BONUS = 0.18
_VERIFIED_BONUS = 0.15
_JP_ORIGINAL_BONUS = 0.55
_JP_ROMANIZED_ANCHOR_BONUS = 1.80  # extra boost: CJK-titled JP original, Latin query, known artist
_WAS_LIVE_PENALTY = 0.50
_DURATION_BONUS_IDEAL = 0.10
_DURATION_BONUS_OK = 0.05
_DURATION_PENALTY_LONG = -0.12
_TITLE_ONLY_PENALTY = 0.40
_JP_COVER_PENALTY = 0.75
_CURATION_PHRASE_PENALTY = 0.65
_CURATION_PENALTY_SCALE = 3.0  # token-penalty multiplier in curation mode
_ANIME_LIVE_PENALTY_SCALE = 0.3  # reduced penalty for live/concert in anime queries


@lru_cache(maxsize=4096)
def normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


@lru_cache(maxsize=4096)
def tokenize_text(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def _word_boundary_match(p: str, t: str) -> bool:
    return p == t or t.startswith(p + " ") or t.endswith(" " + p) or (" " + p + " ") in t


def signal_tokens(query: str) -> list[str]:
    tokens = list(tokenize_text(query))
    filtered = [t for t in tokens if t not in SEARCH_GENERIC_TOKENS or t in SEARCH_ANIME_SIGNAL_TOKENS]
    return filtered or tokens


def detect_intent(query: str) -> dict[str, bool]:
    q = query.strip()
    return {
        "anime": bool(_ANIME_INTENT_RE.search(q)),
        "dash_format": bool(_DASH_SEPARATED_RE.match(q)),
        "has_artist": " " in q,
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
        p.strip() for p in dict.fromkeys(str(p) for p in parts if isinstance(p, str) and p.strip())
    )
    return normalize_text(uploader)


def prepare_entry(item: dict[str, Any]) -> SearchEntryContext:
    normalized_title = _candidate_title_text(item)
    normalized_uploader = _candidate_uploader_text(item)
    title_tokens = list(tokenize_text(normalized_title))
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
        was_live=bool(item.get("was_live", False)),
        description_token_set=frozenset(tokenize_text(str(item.get("description") or "")[:500])),
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
                phrase = " ".join(query_tokens[start : start + size])
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
            phrase = " ".join(query_tokens[start : start + size])
            if phrase in seen:
                continue
            seen.add(phrase)
            if size == 1 and len(phrase) <= 2:
                continue
            count = sum(1 for text in uploader_texts if _word_boundary_match(phrase, text))
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
    q_tokens = signal_tokens(search_text)
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
    uploader_matches = [p for p in anchor_phrases if _word_boundary_match(p, entry.normalized_uploader)]
    if uploader_matches:
        longest = max(len(p.split()) for p in uploader_matches)
        return _ANCHOR_UPLOADER_BASE + ((longest - 1) * _ANCHOR_UPLOADER_PER_WORD)
    title_only = [
        p
        for p in anchor_phrases
        if _word_boundary_match(p, entry.normalized_metadata)
        and not _word_boundary_match(p, entry.normalized_uploader)
    ]
    if title_only:
        longest = max(len(p.split()) for p in title_only)
        return _ANCHOR_TITLE_BASE + ((longest - 1) * _ANCHOR_TITLE_PER_WORD)
    return _ANCHOR_NO_MATCH


class _OverlapSignals(NamedTuple):
    title_overlap: float
    uploader_overlap: float
    metadata_overlap: float
    missing_title_tokens: list[str]
    missing_title_uploader_overlap: float


def _compute_overlap_signals(query: SearchQueryContext, entry: SearchEntryContext) -> _OverlapSignals:
    title_overlap = token_overlap_ratio(query.query_tokens, entry.title_token_set)
    uploader_overlap = token_overlap_ratio(query.query_tokens, entry.uploader_token_set)
    metadata_overlap = token_overlap_ratio(query.query_tokens, entry.metadata_token_set)
    missing_title_tokens = [t for t in query.query_tokens if t not in entry.title_token_set]
    missing_title_uploader_overlap = token_overlap_ratio(missing_title_tokens, entry.uploader_token_set)
    return _OverlapSignals(
        title_overlap,
        uploader_overlap,
        metadata_overlap,
        missing_title_tokens,
        missing_title_uploader_overlap,
    )


class _FuzzySignals(NamedTuple):
    ratio: float
    metadata_ratio: float
    exact_metadata_match: float
    metadata_prefix_match: float
    all_title_tokens_match: float
    all_metadata_tokens_match: float


def _compute_fuzzy_signals(query: SearchQueryContext, entry: SearchEntryContext) -> _FuzzySignals:
    ratio = _fuzz.ratio(query.normalized_query, entry.normalized_title) / 100.0
    metadata_ratio = _fuzz.partial_ratio(query.normalized_query, entry.normalized_metadata) / 100.0
    exact_metadata_match = 1.0 if query.normalized_query in entry.normalized_metadata else 0.0
    metadata_prefix_match = 1.0 if entry.normalized_metadata.startswith(query.normalized_query) else 0.0
    all_title_tokens_match = (
        1.0 if query.query_token_set and query.query_token_set.issubset(entry.title_token_set) else 0.0
    )
    all_metadata_tokens_match = (
        1.0 if query.query_token_set and query.query_token_set.issubset(entry.metadata_token_set) else 0.0
    )
    return _FuzzySignals(
        ratio,
        metadata_ratio,
        exact_metadata_match,
        metadata_prefix_match,
        all_title_tokens_match,
        all_metadata_tokens_match,
    )


class _ChannelSignals(NamedTuple):
    artist_match_bonus: float
    strong_uploader_bonus: float
    topic_bonus: float
    uploader_preference_bonus: float


def _score_channel_signals(
    query: SearchQueryContext,
    entry: SearchEntryContext,
    uploader_overlap: float,
    title_overlap: float,
    curation_mode: bool,
) -> _ChannelSignals:
    artist_token_matches = len(query.query_token_set & entry.uploader_token_set)
    artist_match_bonus = (
        _ARTIST_BONUS_MULTI
        if artist_token_matches >= 2
        else (_ARTIST_BONUS_SINGLE if artist_token_matches == 1 else 0.0)
    )
    strong_uploader_bonus = _STRONG_UPLOADER_BONUS if uploader_overlap >= _THR_UPLOADER_STRONG else 0.0
    topic_bonus = (
        (_TOPIC_BONUS_CURATION if curation_mode else _TOPIC_BONUS_NORMAL)
        if "topic" in entry.uploader_token_set
        else 0.0
    )
    uploader_preference_bonus = sum(
        w for tok, w in SEARCH_PREFERRED_UPLOADER_TOKENS.items() if tok in entry.uploader_token_set
    )
    # Channel-level bonuses should amplify the right song, not every song from
    # the artist's channel.  With no title signal at all they do more harm than good.
    if title_overlap == 0:
        topic_bonus = 0.0
        uploader_preference_bonus = 0.0
    return _ChannelSignals(artist_match_bonus, strong_uploader_bonus, topic_bonus, uploader_preference_bonus)


class _CompletionSignals(NamedTuple):
    artist_completion_bonus: float
    title_only_penalty: float
    title_uploader_synergy: float


def _score_completion_and_synergy(
    title_overlap: float,
    uploader_overlap: float,
    missing_title_tokens: list[str],
    missing_title_uploader_overlap: float,
) -> _CompletionSignals:
    artist_completion_bonus = 0.0
    title_only_penalty = 0.0
    if missing_title_tokens and title_overlap >= _THR_TITLE_MIN:
        artist_completion_bonus += missing_title_uploader_overlap * _COMPLETION_SCALE
        if missing_title_uploader_overlap >= _THR_UPLOADER_FULL:
            artist_completion_bonus += _COMPLETION_BONUS_FULL
        elif missing_title_uploader_overlap >= _THR_UPLOADER_PARTIAL:
            artist_completion_bonus += _COMPLETION_BONUS_PARTIAL
        elif title_overlap >= _THR_TITLE_HIGH:
            title_only_penalty = _TITLE_ONLY_PENALTY
    elif not missing_title_tokens and uploader_overlap >= _THR_UPLOADER_WEAK:
        artist_completion_bonus += _COMPLETION_BONUS_WEAK

    title_uploader_synergy = 0.0
    if title_overlap >= _THR_TITLE_SYNERGY and uploader_overlap >= _THR_UPLOADER_WEAK:
        title_uploader_synergy = _SYNERGY_BONUS_FULL
    elif (
        title_overlap >= _THR_TITLE_HIGH
        and missing_title_tokens
        and missing_title_uploader_overlap >= _THR_UPLOADER_PARTIAL
    ):
        title_uploader_synergy = _SYNERGY_BONUS_PARTIAL
    return _CompletionSignals(artist_completion_bonus, title_only_penalty, title_uploader_synergy)


def _score_discouraged_penalty(
    query: SearchQueryContext,
    entry: SearchEntryContext,
    curation_mode: bool,
    is_anime_query: bool,
) -> float:
    discouraged_penalty = 0.0
    raw_query_token_set = set(query.raw_query_tokens)

    combined_token_set = entry.metadata_token_set | entry.description_token_set
    for token, weight in SEARCH_DISCOURAGED_TOKENS.items():
        if token not in raw_query_token_set and token in combined_token_set:
            if is_anime_query and token in {"live", "stage", "concert"}:
                discouraged_penalty += weight * _ANIME_LIVE_PENALTY_SCALE
            elif curation_mode and token in SEARCH_CURATION_EXTRA_TOKENS:
                discouraged_penalty += weight * _CURATION_PENALTY_SCALE
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
                discouraged_penalty += _CURATION_PHRASE_PENALTY

    raw_title = str(entry.item.get("title") or "")
    if _JP_COVER_BRACKET_RE.search(raw_title):
        query_asks_cover = any(
            tok in raw_query_token_set
            for tok in ("guitar", "piano", "violin", "bass", "acoustic", "cover", "fingerstyle", "ukulele")
        )
        if not query_asks_cover:
            discouraged_penalty += _JP_COVER_PENALTY

    if entry.was_live:
        discouraged_penalty += _WAS_LIVE_PENALTY

    if not curation_mode:
        discouraged_penalty = min(discouraged_penalty, _MAX_DISCOURAGED_PENALTY)

    return discouraged_penalty


def _score_duration_bonus(duration: int) -> float:
    if _DUR_IDEAL_MIN <= duration <= _DUR_IDEAL_MAX:
        return _DURATION_BONUS_IDEAL
    if _DUR_OK_MIN <= duration <= _DUR_OK_MAX:
        return _DURATION_BONUS_OK
    if duration > _DUR_LONG:
        return _DURATION_PENALTY_LONG
    return 0.0


def _score_view_bonus(entry: SearchEntryContext) -> float:
    vc = entry.view_count
    if vc >= _VIEW_MIN:
        return min(
            _VIEW_BONUS_MAX, (math.log10(vc) - _VIEW_BONUS_LOG_REF) / _VIEW_BONUS_LOG_RNG * _VIEW_BONUS_MAX
        )
    if "topic" in entry.uploader_token_set:
        return _VIEW_BONUS_TOPIC
    return 0.0


def _score_recency_bonus(entry: SearchEntryContext, discouraged_penalty: float, today: _date) -> float:
    ud = entry.upload_date
    if not (len(ud) == 8 and ud.isdigit() and discouraged_penalty < _THR_PENALTY_GATE):
        return 0.0
    try:
        uploaded = _date(int(ud[:4]), int(ud[4:6]), int(ud[6:8]))
        days_old = (today - uploaded).days
    except ValueError:
        return 0.0
    if days_old <= _RECENCY_DAYS_NEW:
        return _RECENCY_BONUS_NEW
    if days_old <= _RECENCY_DAYS_RECENT:
        return _RECENCY_BONUS_RECENT
    if days_old <= _RECENCY_DAYS_OLDER:
        return _RECENCY_BONUS_OLDER
    return 0.0


def _score_jp_original_bonus(
    query: SearchQueryContext,
    entry: SearchEntryContext,
    uploader_overlap: float,
    discouraged_penalty: float,
) -> float:
    if discouraged_penalty >= _THR_PENALTY_GATE:
        return 0.0
    raw_title = str(entry.item.get("title") or "")
    title_core = _BRACKET_STRIP_RE.sub("", raw_title).strip()
    if not _CJK_RE.search(title_core):
        return 0.0
    latin_chars = len(re.findall(r"[a-zA-Z]", title_core))
    total_chars = len(title_core.replace(" ", ""))
    hangul_count = len(_HANGUL_RE.findall(title_core))
    cjk_count = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]", title_core))
    latin_ratio = latin_chars / total_chars if total_chars else 1.0
    kana_count = len(_KANA_RE.findall(title_core))
    is_jp = (
        kana_count > 0
        and latin_ratio < _THR_JP_LATIN_RATIO
        and (hangul_count == 0 or cjk_count > hangul_count * _THR_JP_CJK_HANGUL)
    )
    if not is_jp or _JP_EVENT_FROM_RE.search(raw_title):
        return 0.0
    bonus = _JP_ORIGINAL_BONUS
    if uploader_overlap > 0 and not _CJK_RE.search(query.normalized_query):
        bonus += _JP_ROMANIZED_ANCHOR_BONUS
    return bonus


def score_entry(
    query: SearchQueryContext,
    entry: SearchEntryContext,
    *,
    breakdown: dict[str, float] | None = None,
    curation_mode: bool = False,
    _today: _date | None = None,
) -> float:
    if not query.normalized_query or not entry.normalized_metadata:
        return 0.0

    is_anime_query = query.intent.get("anime", False)
    is_dash_query = query.intent.get("dash_format", False)

    overlap = _compute_overlap_signals(query, entry)
    fuzzy = _compute_fuzzy_signals(query, entry)
    channel = _score_channel_signals(
        query, entry, overlap.uploader_overlap, overlap.title_overlap, curation_mode
    )
    completion = _score_completion_and_synergy(
        overlap.title_overlap,
        overlap.uploader_overlap,
        overlap.missing_title_tokens,
        overlap.missing_title_uploader_overlap,
    )

    dash_format_bonus = _DASH_FORMAT_BONUS if is_dash_query and fuzzy.ratio >= _THR_DASH_RATIO else 0.0
    preferred_bonus = sum(
        w for phrase, w in SEARCH_PREFERRED_PHRASES.items() if phrase in entry.normalized_metadata
    )

    discouraged_penalty = _score_discouraged_penalty(query, entry, curation_mode, is_anime_query)
    duration_bonus = _score_duration_bonus(entry.duration)
    anchor_score = score_anchor_match(entry, query.anchor_phrases)
    view_bonus = _score_view_bonus(entry)
    verified_bonus = _VERIFIED_BONUS if entry.channel_is_verified else 0.0
    recency_bonus = _score_recency_bonus(entry, discouraged_penalty, _today or _date.today())
    jp_original_bonus = _score_jp_original_bonus(query, entry, overlap.uploader_overlap, discouraged_penalty)

    final = (
        (fuzzy.ratio * _W_FUZZY_RATIO)
        + (fuzzy.metadata_ratio * _W_METADATA_RATIO)
        + (overlap.title_overlap * _W_TITLE_OVERLAP)
        + (overlap.uploader_overlap * _W_UPLOADER_OVERLAP)
        + (overlap.metadata_overlap * _W_METADATA_OVERLAP)
        + (fuzzy.exact_metadata_match * _W_EXACT_METADATA)
        + (fuzzy.metadata_prefix_match * _W_PREFIX_MATCH)
        + (fuzzy.all_title_tokens_match * _W_ALL_TITLE_TOKENS)
        + (fuzzy.all_metadata_tokens_match * _W_ALL_METADATA_TOKENS)
        + channel.artist_match_bonus
        + channel.strong_uploader_bonus
        + channel.topic_bonus
        + channel.uploader_preference_bonus
        + completion.artist_completion_bonus
        + completion.title_uploader_synergy
        + dash_format_bonus
        + preferred_bonus
        + duration_bonus
        + anchor_score
        + jp_original_bonus
        + view_bonus
        + verified_bonus
        + recency_bonus
        - completion.title_only_penalty
        - discouraged_penalty
    )

    if breakdown is not None:
        breakdown.update(
            {
                "title_overlap": overlap.title_overlap,
                "uploader_overlap": overlap.uploader_overlap,
                "ratio": fuzzy.ratio,
                "metadata_ratio": fuzzy.metadata_ratio,
                "topic_bonus": channel.topic_bonus,
                "uploader_pref_bonus": channel.uploader_preference_bonus,
                "anchor_score": anchor_score,
                "artist_match_bonus": channel.artist_match_bonus,
                "strong_uploader_bonus": channel.strong_uploader_bonus,
                "artist_completion_bonus": completion.artist_completion_bonus,
                "title_uploader_synergy": completion.title_uploader_synergy,
                "preferred_bonus": preferred_bonus,
                "discouraged_penalty": discouraged_penalty,
                "duration_bonus": duration_bonus,
                "jp_original_bonus": jp_original_bonus,
                "view_bonus": view_bonus,
                "verified_bonus": verified_bonus,
                "recency_bonus": recency_bonus,
                "final": final,
            }
        )
    return final


def rank_entries(
    search_text: str,
    entries: list[dict[str, Any]],
    guild_id: int | None,
    last_search: OrderedDict[int, SearchDebugRecord],
    last_search_max: int,
    playlist_entry_url: Any,  # callable(item) -> str|None
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
    today = _date.today()
    for orig_i, item, ectx in prepared:
        bd: dict[str, float] | None = {} if need_debug else None
        sc = score_entry(ctx, ectx, breakdown=bd, curation_mode=curation_mode, _today=today)
        scored.append((sc, orig_i, item, ectx, bd))
    scored.sort(key=lambda t: (t[0], -t[1]), reverse=True)

    if need_debug:
        records: list[ScoreBreakdown] = []
        for rank, (sc, _oi, item, ectx, bd) in enumerate(scored[:8], start=1):
            if bd is None:
                continue
            records.append(
                ScoreBreakdown(
                    rank=rank,
                    title=str(item.get("title") or ""),
                    uploader=str(item.get("uploader") or ectx.normalized_uploader),
                    webpage_url=playlist_entry_url(item) or "",
                    duration=ectx.duration,
                    final_score=round(sc, 4),
                    title_overlap=round(bd.get("title_overlap", 0.0), 3),
                    uploader_overlap=round(bd.get("uploader_overlap", 0.0), 3),
                    ratio=round(bd.get("ratio", 0.0), 3),
                    topic_bonus=round(bd.get("topic_bonus", 0.0), 3),
                    uploader_pref_bonus=round(bd.get("uploader_pref_bonus", 0.0), 3),
                    anchor_score=round(bd.get("anchor_score", 0.0), 3),
                    artist_match_bonus=round(bd.get("artist_match_bonus", 0.0), 3),
                    artist_completion_bonus=round(bd.get("artist_completion_bonus", 0.0), 3),
                    title_uploader_synergy=round(bd.get("title_uploader_synergy", 0.0), 3),
                    preferred_bonus=round(bd.get("preferred_bonus", 0.0), 3),
                    discouraged_penalty=round(bd.get("discouraged_penalty", 0.0), 3),
                    duration_bonus=round(bd.get("duration_bonus", 0.0), 3),
                    jp_original_bonus=round(bd.get("jp_original_bonus", 0.0), 3),
                    view_bonus=round(bd.get("view_bonus", 0.0), 3),
                    verified_bonus=round(bd.get("verified_bonus", 0.0), 3),
                    recency_bonus=round(bd.get("recency_bonus", 0.0), 3),
                )
            )
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
            guild_id,
            search_text,
            " | ".join(f"[{r.rank}] {r.title!r} score={r.final_score}" for r in records),
        )

    return [item for (_, _, item, _, _) in scored]
