from __future__ import annotations

import asyncio
import types
from pathlib import Path

import musicbot.config as config
from musicbot.bot import MusicBot
from musicbot.cogs.admin import AdminCog
from musicbot.cogs.music import MusicCog


def test_hybrid_commands_register_valid_app_commands(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Registers every cog and checks the slash tree discord.py would sync is well-formed
    (names, descriptions, lengths) without touching the network."""
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("DISCORD_TOKEN", "x.y.z")
    settings = config.load_settings()

    async def main() -> list[str]:
        bot = MusicBot(settings, types.SimpleNamespace())  # type: ignore[arg-type]
        await bot.add_cog(AdminCog(bot))
        await bot.add_cog(MusicCog(bot))
        names = [c.name for c in bot.tree.get_commands()]
        payloads = [c.to_dict(bot.tree) for c in bot.tree.get_commands()]
        assert all(p["description"] and len(p["description"]) <= 100 for p in payloads)
        assert len(names) == len(set(names)) <= 100
        await bot.http.close()
        return names

    names = asyncio.run(main())
    assert "play" in names and "skip" in names
    assert "sync" not in names  # owner-only prefix command, deliberately not a slash command
