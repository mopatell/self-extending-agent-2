"""Decides what happens before a tool call runs: allow it, ask a human, or refuse."""

from __future__ import annotations

import re
from typing import Any

from sea.config import settings
from sea.tools.base import Tool

ALLOW, APPROVAL, DENY = "allow", "approval", "deny"

# Shell commands we refuse outright, even if a human would approve them.
DENY_PATTERNS = [
    r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r",  # rm -rf / rm -fr
    r"\bsudo\b",
    r"\bdd\s+if=",
    r"\bmkfs\b",
    r">\s*/dev/",
    r":\(\)\s*\{\s*:\|:&\s*\};:",  # fork bomb
    r"\bshutdown\b|\breboot\b",
    r"\bchmod\s+-R\s+777\s+/",
]


def classify(tool: Tool, arguments: dict[str, Any]) -> tuple[str, str]:
    """Returns (decision, reason)."""
    if tool.name == "run_shell":
        command = str(arguments.get("command", ""))
        for pattern in DENY_PATTERNS:
            if re.search(pattern, command):
                return DENY, f"command matches blocked pattern {pattern!r}"

    if tool.risk == "safe":
        return ALLOW, "read-only or sandboxed pure tool"

    if settings.auto_approve == "all":
        return ALLOW, "AUTO_APPROVE=all"

    return APPROVAL, "side-effecting tool (writes files, runs commands, or uses the network)"
