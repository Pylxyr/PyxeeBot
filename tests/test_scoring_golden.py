"""test_scoring_golden.py — golden ranking regressions from real-world search results.

Each case is a (query, items, expected_winner_url) triple. These pin down the exact
bugs found and fixed during scoring tuning; a regression here means a real query will
return the wrong track again.
"""

from __future__ import annotations

from collections import OrderedDict

import pytest

from musicbot.cogs.music.scoring import build_query_context, prepare_entry, rank_entries, score_entry


def _rank_urls(query: str, items: list[dict], *, curation_mode: bool = False) -> list[str]:
    ranked = rank_entries(
        query,
        items,
        guild_id=None,
        last_search=OrderedDict(),
        last_search_max=50,
        playlist_entry_url=lambda i: i.get("webpage_url", ""),
        curation_mode=curation_mode,
    )
    return [r["webpage_url"] for r in ranked]


GOLDEN_CASES = [
    pytest.param(
        "sangatsu no phantasia pastel rain",
        [
            {
                "title": "Sangatsu No Phantasia - Pastel Rain",
                "uploader": "Sangatsu no Phantasia",
                "channel": "Sangatsu no Phantasia",
                "duration": 219,
                "view_count": 500_000,
                "webpage_url": "live",
            },
            {
                "title": "三月のパンタシア 『パステルレイン』",
                "uploader": "三月のパンタシア",
                "channel": "三月のパンタシア",
                "artist": "Sangatsu no Phantasia",
                "duration": 224,
                "view_count": 2_000_000,
                "webpage_url": "studio",
            },
        ],
        "studio",
        id="jp_studio_beats_latin_titled_live",
    ),
    pytest.param(
        "pinking san-z",
        [
            {
                "title": "Tiny Giant",
                "uploader": "san-z Topic",
                "channel": "san-z Topic",
                "duration": 178,
                "view_count": 1_200_000,
                "webpage_url": "wrong1",
            },
            {
                "title": "BITE!",
                "uploader": "san-z Topic",
                "channel": "san-z Topic",
                "duration": 223,
                "view_count": 1_400_000,
                "webpage_url": "wrong2",
            },
            {
                "title": "制服·剪刀·鲨鱼尾",
                "uploader": "san-z Topic",
                "channel": "san-z Topic",
                "duration": 163,
                "view_count": 800_000,
                "webpage_url": "wrong3",
            },
            {
                "title": "pinKing",
                "uploader": "san-z Official",
                "channel": "san-z Official",
                "duration": 187,
                "view_count": 1_300_000,
                "webpage_url": "correct",
                "channel_is_verified": True,
            },
        ],
        "correct",
        id="correct_song_beats_wrong_songs_on_preferred_channel",
    ),
    pytest.param(
        "制服 剪刀 鲨鱼尾",
        [
            {
                "title": "制服·剪刀·鲨鱼尾",
                "uploader": "san-z",
                "channel": "san-z",
                "duration": 163,
                "view_count": 800_000,
                "webpage_url": "correct",
            },
            {
                "title": "Some Other Song",
                "uploader": "san-z",
                "channel": "san-z",
                "duration": 200,
                "view_count": 900_000,
                "webpage_url": "wrong",
            },
        ],
        "correct",
        id="chinese_query_still_matches_chinese_title",
    ),
    pytest.param(
        "artist song",
        [
            {
                "title": "Artist - Song",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 200,
                "view_count": 500_000,
                "webpage_url": "live",
                "was_live": True,
            },
            {
                "title": "Artist - Song",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 200,
                "view_count": 500_000,
                "webpage_url": "studio",
            },
        ],
        "studio",
        id="was_live_flag_demotes_track",
    ),
    pytest.param(
        "artist song",
        [
            {
                "title": "Artist - Song",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 200,
                "view_count": 500_000,
                "webpage_url": "live_desc",
                "description": "Recorded live at Budokan 2024",
            },
            {
                "title": "Artist - Song",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 200,
                "view_count": 500_000,
                "webpage_url": "clean",
            },
        ],
        "clean",
        id="description_live_keyword_demotes_track",
    ),
    pytest.param(
        "pinking san-z",
        [
            {
                "title": "pinKing - HOYO-MiX & San-Z-STUDIO | Official English Lyrics [Zenless Zone Zero",
                "uploader": "Lyrics Channel",
                "channel": "Lyrics Channel",
                "duration": 188,
                "view_count": 600_000,
                "webpage_url": "lyrics",
            },
            {
                "title": "Completely Different Song",
                "uploader": "Some Other Artist",
                "channel": "Some Other Artist",
                "duration": 200,
                "view_count": 800_000,
                "webpage_url": "irrelevant",
            },
        ],
        "lyrics",
        id="capped_penalty_still_beats_title_irrelevant_track",
    ),
    pytest.param(
        "lemon kenshi yonezu",
        [
            {
                "title": "Lemon",
                "uploader": "Kenshi Yonezu",
                "channel": "Kenshi Yonezu",
                "duration": 250,
                "view_count": 5_000_000,
                "webpage_url": "official",
                "channel_is_verified": True,
            },
            {
                "title": "【弾いてみた】Lemon / Kenshi Yonezu",
                "uploader": "Guitar Cover Channel",
                "channel": "Guitar Cover Channel",
                "duration": 245,
                "view_count": 100_000,
                "webpage_url": "cover",
            },
        ],
        "official",
        id="jp_cover_bracket_penalised_for_plain_query",
    ),
    pytest.param(
        "artist song",
        [
            {
                "title": "Artist - Song",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 200,
                "view_count": 500_000,
                "webpage_url": "unverified",
            },
            {
                "title": "Artist - Song",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 200,
                "view_count": 500_000,
                "webpage_url": "verified",
                "channel_is_verified": True,
            },
        ],
        "verified",
        id="verified_channel_breaks_near_tie",
    ),
    pytest.param(
        "artist song",
        [
            {
                "title": "Artist - Song",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 200,
                "view_count": 500_000,
                "webpage_url": "ideal",
            },
            {
                "title": "Artist - Song (3 Hour Mix)",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 10800,
                "view_count": 500_000,
                "webpage_url": "long_mix",
            },
        ],
        "ideal",
        id="long_mix_ranks_below_ideal_length_track",
    ),
    pytest.param(
        "shape of you tv size",
        [
            {
                "title": "Shape of You (TV Size)",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 90,
                "view_count": 500_000,
                "webpage_url": "tv_size",
            },
            {
                "title": "Shape of You",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 240,
                "view_count": 5_000_000,
                "webpage_url": "full",
            },
        ],
        "tv_size",
        id="explicit_tv_size_query_not_penalised",
    ),
    pytest.param(
        "angela aki letter song",
        [
            {
                "title": "Letter",
                "uploader": "Random Coffee Shop Vlogs",
                "channel": "Random Coffee Shop Vlogs",
                "duration": 200,
                "view_count": 5000,
                "webpage_url": "wrong_uploader",
            },
            {
                "title": "Letter Song",
                "uploader": "Angela Aki",
                "channel": "Angela Aki",
                "duration": 240,
                "view_count": 800_000,
                "webpage_url": "right_uploader",
            },
        ],
        "right_uploader",
        id="anchor_phrase_disambiguates_same_titled_uploaders",
    ),
    pytest.param(
        "Artist - Song",
        [
            {
                "title": "Artist - Song",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 200,
                "view_count": 500_000,
                "webpage_url": "dash",
            },
            {
                "title": "Artist Song",
                "uploader": "Artist",
                "channel": "Artist",
                "duration": 200,
                "view_count": 500_000,
                "webpage_url": "nodash",
            },
        ],
        "dash",
        id="dash_format_query_favours_dash_format_title",
    ),
    pytest.param(
        "sangatsu no phantasia",
        [
            {
                "title": "三月のパンタシア",
                "uploader": "Sangatsu no Phantasia",
                "channel": "Sangatsu no Phantasia",
                "duration": 200,
                "view_count": 1_000_000,
                "webpage_url": "jp_correct",
            },
            {
                "title": "制服剪刀鲨鱼尾",
                "uploader": "Sangatsu no Phantasia",
                "channel": "Sangatsu no Phantasia",
                "duration": 200,
                "view_count": 1_000_000,
                "webpage_url": "chinese_wrong",
            },
        ],
        "jp_correct",
        id="kana_required_chinese_title_does_not_outrank_japanese",
    ),
]


@pytest.mark.parametrize("query,items,expected_winner_url", GOLDEN_CASES)
def test_golden_ranking(query: str, items: list[dict], expected_winner_url: str) -> None:
    assert _rank_urls(query, items)[0] == expected_winner_url


def test_curation_mode_skips_discouraged_penalty_cap() -> None:
    heavy = {
        "title": "pinking san-z official english lyrics tv size cover",
        "uploader": "Random",
        "channel": "Random",
        "duration": 188,
        "view_count": 600_000,
        "webpage_url": "heavy_penalty",
    }
    entry = prepare_entry(heavy)
    ctx = build_query_context("pinking san-z", [entry])
    breakdown: dict[str, float] = {}
    score_entry(ctx, entry, breakdown=breakdown, curation_mode=True)
    assert breakdown["discouraged_penalty"] > 0.65
