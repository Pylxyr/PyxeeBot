from __future__ import annotations

import asyncio
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from musicbot.bot import HelpOverviewView
from musicbot.cogs.curation import CuratedTrack, CurationSession, CurationView, RefillView
from musicbot.cogs.music.views import _close_interaction_message


def _interaction(user_id: int = 1, *, manage_messages: bool = False) -> Any:
    interaction = MagicMock()
    interaction.user = types.SimpleNamespace(id=user_id)
    interaction.permissions = types.SimpleNamespace(manage_messages=manage_messages)
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.delete_original_response = AsyncMock()
    interaction.message.delete = AsyncMock()
    return interaction


def _help_view() -> HelpOverviewView:
    return HelpOverviewView(
        categories=[("Playback Deck", ["`!play` — play"])],
        overview_embed=discord.Embed(title="Help"),
        colour=discord.Colour.blue(),
        total_commands=1,
        author_id=10,
    )


def _labels(view: discord.ui.View) -> list[str | None]:
    return [item.label for item in view.children if isinstance(item, discord.ui.Button)]


def test_help_menu_has_a_close_button() -> None:
    async def main() -> None:
        view = _help_view()
        assert "Close" in _labels(view)
        close = next(i for i in view.children if isinstance(i, discord.ui.Button) and i.label == "Close")
        assert not close.disabled

    asyncio.run(main())


def test_help_close_is_limited_to_the_opener_or_a_moderator() -> None:
    async def main() -> None:
        view = _help_view()
        close = next(i for i in view.children if isinstance(i, discord.ui.Button) and i.label == "Close")

        stranger = _interaction(user_id=99)
        with patch("musicbot.bot._close_interaction_message", new=AsyncMock()) as closer:
            await close.callback(stranger)
            closer.assert_not_called()
        stranger.response.send_message.assert_awaited_once()

        for who in (_interaction(user_id=10), _interaction(user_id=99, manage_messages=True)):
            with patch("musicbot.bot._close_interaction_message", new=AsyncMock()) as closer:
                await close.callback(who)
                closer.assert_awaited_once()

    asyncio.run(main())


def test_close_helper_falls_back_to_original_response_for_ephemeral_messages() -> None:
    async def main() -> None:
        interaction = _interaction()
        interaction.message.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
        await _close_interaction_message(interaction)
        interaction.delete_original_response.assert_awaited_once()

        normal = _interaction()
        await _close_interaction_message(normal)
        normal.message.delete.assert_awaited_once()
        normal.delete_original_response.assert_not_awaited()

    asyncio.run(main())


def test_curation_queue_all_with_nothing_selected_does_not_freeze_the_panel() -> None:
    async def main() -> None:
        session = CurationSession(
            guild_id=1,
            author_id=1,
            seed_query="q",
            seed_artist="a",
            seed_track="t",
            tracks=[CuratedTrack(title="t", artist="a", selected=False)],
        )
        view = CurationView(MagicMock(), session)
        queue_all = next(
            i for i in view.children if isinstance(i, discord.ui.Button) and i.label == "Queue All"
        )
        interaction = _interaction()
        await queue_all.callback(interaction)
        interaction.response.send_message.assert_awaited_once()
        interaction.response.edit_message.assert_not_awaited()
        assert all(not getattr(i, "disabled", False) for i in view.children)
        assert "Cancel" in _labels(view)

    asyncio.run(main())


def test_curation_and_refill_panels_recover_when_queueing_fails() -> None:
    async def main() -> None:
        cog = MagicMock()
        cog._resolve_and_queue = AsyncMock(side_effect=RuntimeError("boom"))
        session = CurationSession(
            guild_id=1,
            author_id=1,
            seed_query="q",
            seed_artist="a",
            seed_track="t",
            tracks=[CuratedTrack(title="t", artist="a")],
        )
        panels: list[tuple[discord.ui.View, str]] = [
            (CurationView(cog, session), "Queue All"),
            (RefillView(cog, 1, 1, [CuratedTrack(title="t", artist="a")]), "Add All"),
        ]
        for view, label in panels:
            button = next(i for i in view.children if isinstance(i, discord.ui.Button) and i.label == label)
            await button.callback(_interaction())
            assert all(not getattr(i, "disabled", False) for i in view.children), label

    asyncio.run(main())
