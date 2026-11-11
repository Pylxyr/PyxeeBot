from __future__ import annotations

import types
from pathlib import Path

import pytest

import musicbot.config as config
from musicbot.cogs.music._extraction import js_runtime_options


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("DISCORD_TOKEN", "x.y.z")
    for name in ("YTDLP_CACHE_DIR", "SYNC_SLASH_COMMANDS", "LOG_TO_FILE"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_cache_dir_defaults_inside_data_dir(isolated_config: Path) -> None:
    settings = config.load_settings()
    assert settings.ytdlp_cache_dir == isolated_config / "data" / "ytdlp-cache"
    assert settings.ytdlp_cache_dir.is_dir()
    assert settings.sync_slash_commands is False


def test_cache_dir_override_and_slash_sync_flag(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YTDLP_CACHE_DIR", "custom-cache")
    monkeypatch.setenv("SYNC_SLASH_COMMANDS", "TRUE")
    settings = config.load_settings()
    assert settings.ytdlp_cache_dir == isolated_config / "custom-cache"
    assert settings.sync_slash_commands is True


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/usr/bin/node", "node"),
        ("/opt/deno/deno", "deno"),
        ("/usr/local/bin/bun", "bun"),
        ("/usr/bin/qjs-quickjs", "node"),  # unknown file name falls back to node semantics
        ("/usr/bin/quickjs", "quickjs"),
    ],
)
def test_js_runtime_options_pick_runtime_by_binary_name(path: str, name: str) -> None:
    assert js_runtime_options(path) == {name: {"path": path}}


def test_intents_are_minimal() -> None:
    from musicbot.bot import MusicBot

    settings = types.SimpleNamespace(default_prefix="!")
    bot = MusicBot(settings, types.SimpleNamespace())  # type: ignore[arg-type]
    intents = bot.intents
    assert intents.message_content and intents.guild_messages and intents.dm_messages
    assert intents.guilds and intents.voice_states
    assert not (intents.members or intents.presences or intents.reactions or intents.typing)
    assert bot._connection.max_messages is None
