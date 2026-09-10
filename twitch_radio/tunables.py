from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Single source of truth for each tunable's valid range — shared by the
# /settings HTTP form (admin_server.py), the chat-based !setlimit mod
# command (chatbot.py), and from_dict()'s own defense-in-depth clamp below,
# so all three enforce identical limits instead of three copies quietly
# drifting apart.
TUNABLE_BOUNDS: dict[str, tuple[int, int]] = {
    "max_pending_per_chatter": (1, 10),
    "request_cooldown_seconds": (0, 3600),
    "queue_cap": (1, 200),
    "max_request_duration_seconds": (30, 3600),
    "vote_skip_threshold": (2, 20),
}


@dataclass(slots=True)
class TwitchTunables:
    """Request-limit knobs adjustable at runtime from the /settings page or
    chat mod commands, without restarting the service. Defaults here are the
    fallback when nothing has been saved to the JSON store yet."""

    max_pending_per_chatter: int = 2
    request_cooldown_seconds: int = 0
    queue_cap: int = 50
    max_request_duration_seconds: int = 600
    vote_skip_threshold: int = 3

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TwitchTunables":
        defaults = cls()

        def _field(name: str, default: int) -> int:
            # admin_server and the chat !setlimit command both already
            # enforce TUNABLE_BOUNDS before ever writing to the store, so
            # this only ever matters for a hand-edited or corrupted
            # tunables.json, or one left over from a version with different
            # limits — but "!sr crashes with no reply because someone
            # typo'd the file" is a worse failure mode than "one bad field
            # reverts to its default", so this degrades per-field instead
            # of raising.
            if name not in data:
                return default
            try:
                value = int(data[name])
            except (TypeError, ValueError):
                log.warning("tunables.json: %r is not a valid number (%r) — using default.", name, data[name])
                return default
            bounds = TUNABLE_BOUNDS.get(name)
            if bounds is not None:
                lo, hi = bounds
                if not (lo <= value <= hi):
                    clamped = max(lo, min(hi, value))
                    log.warning(
                        "tunables.json: %r=%r is outside the allowed range %d-%d — clamping to %d.",
                        name, value, lo, hi, clamped,
                    )
                    return clamped
            return value

        return cls(
            max_pending_per_chatter=_field("max_pending_per_chatter", defaults.max_pending_per_chatter),
            request_cooldown_seconds=_field(
                "request_cooldown_seconds", defaults.request_cooldown_seconds
            ),
            queue_cap=_field("queue_cap", defaults.queue_cap),
            max_request_duration_seconds=_field(
                "max_request_duration_seconds", defaults.max_request_duration_seconds
            ),
            vote_skip_threshold=_field("vote_skip_threshold", defaults.vote_skip_threshold),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_pending_per_chatter": self.max_pending_per_chatter,
            "request_cooldown_seconds": self.request_cooldown_seconds,
            "queue_cap": self.queue_cap,
            "max_request_duration_seconds": self.max_request_duration_seconds,
            "vote_skip_threshold": self.vote_skip_threshold,
        }
