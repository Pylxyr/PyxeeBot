"""test_scoring.py — unit tests for musicbot.cogs.music.scoring."""

from __future__ import annotations

from collections import OrderedDict
from unittest.mock import MagicMock

import pytest

from musicbot.cogs.music.scoring import (
    build_query_context,
    detect_intent,
    normalize_text,
    prepare_entry,
    rank_entries,
    score_anchor_match,
    score_entry,
    signal_tokens,
    token_overlap_ratio,
    tokenize_text,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _item(
    title: str, uploader: str = "", duration: int = 210, view_count: int = 500_000, channel: str = ""
) -> dict:
    return {
        "title": title,
        "uploader": uploader or title,
        "channel": channel or uploader or title,
        "duration": duration,
        "view_count": view_count,
        "webpage_url": "https://youtube.com/watch?v=x",
    }


def _score(query_text: str, item: dict, *, curation_mode: bool = False) -> float:
    entry = prepare_entry(item)
    ctx = build_query_context(query_text, [entry])
    if ctx is None:
        return 0.0
    return score_entry(ctx, entry, curation_mode=curation_mode)


def _rank(query_text: str, items: list[dict]) -> list[str]:
    ranked = rank_entries(
        query_text,
        items,
        guild_id=None,
        last_search=OrderedDict(),
        last_search_max=50,
        playlist_entry_url=lambda i: i.get("webpage_url", ""),
    )
    return [i["title"] for i in ranked]


# ── normalize_text / tokenize_text ───────────────────────────────────────────


def test_normalize_text_lowercases_and_strips_symbols():
    assert normalize_text("The Weeknd — Blinding Lights") == "the weeknd blinding lights"


def test_normalize_text_empty():
    assert normalize_text("") == ""


def test_tokenize_text_cached_returns_tuple():
    result = tokenize_text("Hello World")
    assert isinstance(result, tuple)
    assert result == tokenize_text("Hello World")


def test_tokenize_text_strips_punctuation():
    assert "feat" in tokenize_text("Artist feat. Someone")


# ── signal_tokens ─────────────────────────────────────────────────────────────


def test_signal_tokens_removes_generic_words():
    tokens = signal_tokens("the weeknd official video")
    assert "official" not in tokens
    assert "video" not in tokens
    assert "weeknd" in tokens


def test_signal_tokens_fallback_when_all_generic():
    tokens = signal_tokens("official video audio")
    assert len(tokens) > 0


# ── detect_intent ─────────────────────────────────────────────────────────────


def test_detect_intent_anime_keyword_ost():
    intent = detect_intent("attack on titan ost")
    assert intent["anime"] is True


def test_detect_intent_dash_format():
    intent = detect_intent("Artist - Song Title")
    assert intent["dash_format"] is True


def test_detect_intent_single_word_no_artist():
    intent = detect_intent("levitating")
    assert intent["has_artist"] is False


# ── token_overlap_ratio ───────────────────────────────────────────────────────


def test_token_overlap_full_match():
    assert token_overlap_ratio(["a", "b"], {"a", "b", "c"}) == 1.0


def test_token_overlap_partial():
    ratio = token_overlap_ratio(["a", "b", "c"], {"a", "c"})
    assert abs(ratio - 2 / 3) < 1e-9


def test_token_overlap_no_match():
    assert token_overlap_ratio(["x", "y"], {"a", "b"}) == 0.0


def test_token_overlap_empty_query():
    assert token_overlap_ratio([], {"a", "b"}) == 0.0


def test_token_overlap_empty_candidate():
    assert token_overlap_ratio(["a"], set()) == 0.0


# ── score_entry: live / cover penalties ──────────────────────────────────────


def test_live_in_title_penalises():
    studio = _item("Blinding Lights", "The Weeknd")
    live = _item("Blinding Lights (Live at Glastonbury)", "The Weeknd")
    s_studio = _score("the weeknd blinding lights", studio)
    s_live = _score("the weeknd blinding lights", live)
    assert s_studio > s_live


def test_cover_in_title_penalises():
    original = _item("As It Was", "Harry Styles")
    cover = _item("As It Was - Piano Cover", "SomePianist")
    assert _score("harry styles as it was", original) > _score("harry styles as it was", cover)


def test_curation_mode_amplifies_live_penalty():
    live = _item("Song (Live at Festival)", "Artist")
    normal = _score("artist song", live, curation_mode=False)
    curate = _score("artist song", live, curation_mode=True)
    assert normal > curate


# ── score_entry: topic / label boosts ────────────────────────────────────────


def test_topic_channel_boosts_score():
    topic = _item("Levitating", "Dua Lipa - Topic")
    normal = _item("Levitating", "Dua Lipa")
    assert _score("dua lipa levitating", topic) > _score("dua lipa levitating", normal)


def test_uploader_pref_bonus_for_known_label():
    label = _item("Some Song", "HYBE LABELS")
    other = _item("Some Song", "RandomChannel")
    assert _score("some song", label) > _score("some song", other)


# ── score_entry: CJK / JP original bonus ────────────────────────────────────


def test_jp_original_bonus_for_cjk_title():
    # The jp_original_bonus fires when the raw title is mostly CJK
    # but the uploader is ASCII (so normalized_metadata is non-empty).
    jp_item = {
        "title": "だから僕は音楽を辞めた MV",  # mostly CJK, "MV" keeps metadata non-empty
        "channel": "Yorushika",
        "uploader": "Yorushika",
        "duration": 240,
        "view_count": 1_000_000,
        "webpage_url": "https://youtube.com/watch?v=jp",
    }
    non_jp = {
        "title": "Dakara Boku wa Ongaku wo Yameta (AMV)",
        "channel": "SomeFan",
        "uploader": "SomeFan",
        "duration": 240,
        "view_count": 50_000,
        "webpage_url": "https://youtube.com/watch?v=nj",
    }
    query = "yorushika dakara boku wa ongaku wo yameta"
    assert _score(query, jp_item) > _score(query, non_jp)


def test_chinese_title_no_jp_bonus():
    # Pure CJK but no kana — should not receive jp_original_bonus
    chinese = {
        "title": "制服·剪刀·鲨鱼尾",
        "uploader": "san-z Topic",
        "channel": "san-z Topic",
        "duration": 163,
        "view_count": 800_000,
        "webpage_url": "https://youtube.com/watch?v=zh",
    }
    latin = {
        "title": "Seifuku Scissors Sharkuzu",
        "uploader": "san-z",
        "channel": "san-z",
        "duration": 163,
        "view_count": 800_000,
        "webpage_url": "https://youtube.com/watch?v=la",
    }
    bd_cn: dict = {}
    bd_la: dict = {}
    from musicbot.cogs.music.scoring import prepare_entry, build_query_context, score_entry

    e_cn = prepare_entry(chinese)
    e_la = prepare_entry(latin)
    ctx = build_query_context("san-z pinking", [e_cn, e_la])
    score_entry(ctx, e_cn, breakdown=bd_cn)
    score_entry(ctx, e_la, breakdown=bd_la)
    assert bd_cn.get("jp_original_bonus", 0.0) == 0.0, "Chinese title must not receive jp_original_bonus"


def test_jp_kana_title_gets_jp_bonus():
    # Title with hiragana/katakana must receive jp_original_bonus
    jp = {
        "title": "三月のパンタシア 『パステルレイン』",
        "uploader": "Sangatsu no Phantasia",
        "channel": "Sangatsu no Phantasia",
        "duration": 224,
        "view_count": 2_000_000,
        "webpage_url": "https://youtube.com/watch?v=jp",
    }
    bd: dict = {}
    from musicbot.cogs.music.scoring import prepare_entry, build_query_context, score_entry

    entry = prepare_entry(jp)
    ctx = build_query_context("sangatsu no phantasia pastel rain", [entry])
    score_entry(ctx, entry, breakdown=bd)
    assert bd.get("jp_original_bonus", 0.0) > 0.0, "Kana title must receive jp_original_bonus"


def test_jp_romanized_anchor_bonus_beats_latin_live():
    # Official CJK-titled JP track should outscore a Latin-titled version when
    # the artist is known (anchor) and the query is romanized Latin.
    jp_studio = {
        "title": "三月のパンタシア 『パステルレイン』",
        "uploader": "Sangatsu no Phantasia",
        "channel": "Sangatsu no Phantasia",
        "duration": 224,
        "view_count": 2_000_000,
        "webpage_url": "https://youtube.com/watch?v=jp",
    }
    latin_live = {
        "title": "Sangatsu No Phantasia - Pastel Rain",
        "uploader": "Sangatsu no Phantasia",
        "channel": "Sangatsu no Phantasia",
        "duration": 219,
        "view_count": 500_000,
        "webpage_url": "https://youtube.com/watch?v=la",
    }
    query = "sangatsu no phantasia pastel rain"
    assert _score(query, jp_studio) > _score(query, latin_live)


def test_was_live_applies_penalty():
    base = _item("Artist - Song", "Artist")
    live = dict(base, was_live=True)
    assert _score("artist song", live) < _score("artist song", base)


def test_description_live_keyword_penalised():
    # A track whose description contains "live" (but not the title) should score
    # lower than one without it, for a non-live query.
    clean = _item("Artist - Song", "Artist")
    with_desc = dict(clean, description="Recorded live at Budokan 2024")
    assert _score("artist song", with_desc) < _score("artist song", clean)


def test_topic_bonus_zeroed_without_title_overlap():
    # A Topic channel song with zero title match should not receive topic or
    # preferred-uploader bonuses — they are song-level signals, not artist-level.
    topic_no_title = _item("Completely Different Song", "san-z Topic")
    topic_no_title["channel"] = "san-z Topic"
    correct_song = _item("pinking", "san-z")

    q = "pinking san-z"
    bd_wrong: dict = {}
    bd_right: dict = {}
    from musicbot.cogs.music.scoring import prepare_entry, build_query_context, score_entry

    e_wrong = prepare_entry(topic_no_title)
    e_right = prepare_entry(correct_song)
    ctx = build_query_context(q, [e_wrong, e_right])
    score_entry(ctx, e_wrong, breakdown=bd_wrong)
    score_entry(ctx, e_right, breakdown=bd_right)

    assert bd_wrong["topic_bonus"] == 0.0, "topic_bonus must be 0 when title_overlap is 0"
    assert bd_wrong["uploader_pref_bonus"] == 0.0, "uploader_pref_bonus must be 0 when title_overlap is 0"
    assert bd_right["final"] > bd_wrong["final"], "correct song must outscore wrong-channel song"


def test_discouraged_penalty_capped():
    # Even a heavily penalised track (e.g. lyrics video) should not score lower
    # than a completely title-irrelevant track.
    lyrics_correct = dict(_item("pinking", "san-z"), title="pinking san-z official english lyrics")
    irrelevant = _item("Completely Different Song", "san-z")
    assert _score("pinking san-z", lyrics_correct) > _score("pinking san-z", irrelevant)


# ── score_anchor_match ────────────────────────────────────────────────────────


def test_anchor_uploader_exact_match_gives_strong_bonus():
    entry = prepare_entry(_item("Blinding Lights", "The Weeknd"))
    score = score_anchor_match(entry, ["the weeknd"])
    assert score > 1.0


def test_anchor_no_match_gives_penalty():
    entry = prepare_entry(_item("Some Track", "Some Artist"))
    score = score_anchor_match(entry, ["completely different artist"])
    assert score < 0.0


def test_anchor_title_only_partial_bonus():
    entry = prepare_entry(_item("The Weeknd Blinding Lights fan mix", "SomeFan"))
    score = score_anchor_match(entry, ["the weeknd"])
    assert 0.0 < score < 1.0


# ── rank_entries ──────────────────────────────────────────────────────────────


def test_rank_studio_beats_live():
    items = [
        _item("Blinding Lights (Live at Coachella)", "The Weeknd"),
        _item("Blinding Lights", "The Weeknd"),
    ]
    ranked = _rank("the weeknd blinding lights", items)
    assert ranked[0] == "Blinding Lights"


def test_rank_original_beats_cover():
    items = [
        _item("Heat Waves - Guitar Cover", "GuitarGuy"),
        _item("Heat Waves", "Glass Animals"),
    ]
    assert _rank("glass animals heat waves", items)[0] == "Heat Waves"


def test_rank_empty_returns_empty():
    assert _rank("anything", []) == []


def test_rank_single_item_returned():
    items = [_item("Solo Track", "Solo Artist")]
    assert _rank("solo track", items) == ["Solo Track"]


def test_rank_topic_channel_preferred():
    items = [
        _item("Levitating", "SomeVEVO"),
        _item("Levitating", "Dua Lipa - Topic"),
    ]
    ranked = rank_entries(
        "dua lipa levitating",
        items,
        guild_id=None,
        last_search=OrderedDict(),
        last_search_max=50,
        playlist_entry_url=lambda i: i.get("webpage_url", ""),
    )
    assert ranked[0]["uploader"] == "Dua Lipa - Topic"


def test_rank_stores_debug_record_when_guild_id_given():
    last_search = OrderedDict()
    items = [_item("Track A", "Artist"), _item("Track B", "Artist")]
    rank_entries(
        "artist track",
        items,
        guild_id=42,
        last_search=last_search,
        last_search_max=50,
        playlist_entry_url=lambda i: i.get("webpage_url", ""),
    )
    assert 42 in last_search
    assert last_search[42].query_text == "artist track"
    assert last_search[42].candidates[0].selected is True


def test_rank_no_debug_record_when_guild_id_none():
    last_search = OrderedDict()
    rank_entries(
        "query",
        [_item("T", "A")],
        guild_id=None,
        last_search=last_search,
        last_search_max=50,
        playlist_entry_url=lambda i: "",
    )
    assert len(last_search) == 0


def test_rank_last_search_evicts_oldest_when_over_max():
    last_search = OrderedDict()
    for gid in range(5):
        rank_entries(
            "query",
            [_item("T", "A")],
            guild_id=gid,
            last_search=last_search,
            last_search_max=3,
            playlist_entry_url=lambda i: "",
        )
    assert len(last_search) == 3
    assert 0 not in last_search


# ── Recency bonus ─────────────────────────────────────────────────────────────


def test_recency_bonus_recent_upload_scores_higher():
    from datetime import date, timedelta

    recent = (date.today() - timedelta(days=60)).strftime("%Y%m%d")
    new_item = {**_item("Track", "Artist"), "upload_date": recent}
    old_item = _item("Track", "Artist")
    assert _score("artist track", new_item) > _score("artist track", old_item)


def test_recency_bonus_absent_beyond_two_years():
    from datetime import date, timedelta

    old = (date.today() - timedelta(days=800)).strftime("%Y%m%d")
    with_old = {**_item("Track", "Artist"), "upload_date": old}
    without_date = _item("Track", "Artist")
    assert abs(_score("artist track", with_old) - _score("artist track", without_date)) < 0.001


def test_recency_bonus_suppressed_when_heavily_penalised():
    from datetime import date, timedelta

    recent = (date.today() - timedelta(days=30)).strftime("%Y%m%d")
    live_new = {**_item("Song Live at Festival", "Artist"), "upload_date": recent}
    studio_old = _item("Song", "Artist")
    assert _score("artist song", studio_old) > _score("artist song", live_new)
