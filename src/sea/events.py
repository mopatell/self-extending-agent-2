"""Events are the one thing every part of the system agrees on.

The runtime emits them, the DB stores them, and the CLI / API / evals read them.
An Event is just a type name plus a JSON-able payload.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sea.db import DB

# Known event types (documentation + a guard against typos).
TYPES = {
    "run_started",
    "run_completed",
    "run_failed",
    "run_paused",
    "run_resumed",
    "plan_proposed",
    "plan_approved",
    "plan_rejected",
    "tool_gap_found",
    "tool_synthesized",
    "tool_test_result",
    "tool_registered",
    "tool_build_failed",
    "step_started",
    "step_completed",
    "step_failed",
    "step_blocked",
    "llm_called",
    "tool_called",
    "tool_result",
    "approval_requested",
    "approval_resolved",
    "question_asked",
    "question_answered",
}


@dataclass
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0


Listener = Callable[[Event], None]


class Emitter:
    """Persists each event for a run and fans it out to in-process listeners."""

    def __init__(self, db: DB, run_id: str, listeners: list[Listener] | None = None):
        self.db = db
        self.run_id = run_id
        self.listeners = list(listeners or [])

    def emit(self, type_: str, **payload: Any) -> Event:
        if type_ not in TYPES:
            raise ValueError(f"Unknown event type {type_!r}")
        seq = self.db.add_event(self.run_id, type_, payload)
        event = Event(type_, payload, seq)
        for listener in self.listeners:
            listener(event)
        return event

    def child(self, **defaults: Any) -> Emitter:
        """Same run and listeners, but every payload gets these fields (e.g. step_id)."""
        parent = self

        class _Child(Emitter):
            def emit(self, type_: str, **payload: Any) -> Event:
                return parent.emit(type_, **{**defaults, **payload})

        return _Child(self.db, self.run_id, self.listeners)
