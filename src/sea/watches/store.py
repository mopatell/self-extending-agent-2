"""Small shared helpers for watches that aren't SQL (that lives in db.py)."""

from __future__ import annotations

import re

# All watch compile runs hang off this one conversation, so `sea runs` groups them.
WATCHES_CONVERSATION = "watches"

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """'30s' -> 30, '5m' -> 300, '2h' -> 7200, '90' -> 90 (bare number = seconds)."""
    text = text.strip().lower()
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", text)
    if not match:
        raise ValueError(f"bad duration {text!r}; use e.g. 30s, 5m, 2h, 1d")
    return int(match.group(1)) * _UNITS.get(match.group(2) or "s", 1)


def short(value: object, limit: int = 120) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
