"""Runtime-configurable Twitch request limits.

This is the live source of truth chatbot.py reads directly on every command —
a plain attribute access, no DB round trip per chat message. The settings
panel (admin_server.py) mutates this object in place AND persists it to the
database in the same call, so a value set through the panel takes effect
immediately for the next command and survives a bot restart.

Deliberately separate from musicbot.config.Settings: those are .env-sourced,
read once at startup, and require a restart + redeploy to change — appropriate
for things like credentials, but not for "how many songs can Alice queue at
once", which the streamer should be able to tune live, mid-stream, without
touching a server.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TwitchTunables:
    max_pending_per_chatter: int = 2
    request_cooldown_seconds: int = 0
    queue_cap: int = 50
    max_request_duration_seconds: int = 600

    def clamp(self) -> None:
        """Applied on every load and every save — a value written directly to
        the database by hand, or a stale/malformed row, can never put the live
        bot into a nonsensical state (e.g. a 0-length queue cap)."""
        self.max_pending_per_chatter = max(1, min(20, self.max_pending_per_chatter))
        self.request_cooldown_seconds = max(0, min(3600, self.request_cooldown_seconds))
        self.queue_cap = max(1, min(200, self.queue_cap))
        self.max_request_duration_seconds = max(30, min(3600, self.max_request_duration_seconds))

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> "TwitchTunables":
        tunables = cls(
            max_pending_per_chatter=data["max_pending_per_chatter"],
            request_cooldown_seconds=data["request_cooldown_seconds"],
            queue_cap=data["queue_cap"],
            max_request_duration_seconds=data["max_request_duration_seconds"],
        )
        tunables.clamp()
        return tunables

    def to_dict(self) -> dict[str, int]:
        return {
            "max_pending_per_chatter": self.max_pending_per_chatter,
            "request_cooldown_seconds": self.request_cooldown_seconds,
            "queue_cap": self.queue_cap,
            "max_request_duration_seconds": self.max_request_duration_seconds,
        }
