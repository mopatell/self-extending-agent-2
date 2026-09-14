"""Human-in-the-loop.

The runtime asks a `Human` for decisions (approve a plan, allow a tool call,
answer a question). Two implementations:

- an interactive one (in cli.py) that prompts in the terminal and returns at once;
- `DetachedHuman`, used by the API and by `sea resume`: it records a pending
  approval in the DB and raises `Interrupt`. The orchestrator catches that,
  checkpoints the run, and stops. When the decision arrives, the run is resumed
  with the decision pre-filled so the same code path continues where it left off.
"""

from __future__ import annotations

from typing import Any, Protocol

from sea.db import DB
from sea.events import Emitter

PLAN, TOOL_CALL, QUESTION = "plan", "tool_call", "question"


class Interrupt(Exception):
    def __init__(self, approval_id: str, kind: str, payload: dict[str, Any]):
        super().__init__(f"waiting for human: {kind} ({approval_id})")
        self.approval_id = approval_id
        self.kind = kind
        self.payload = payload


class Human(Protocol):
    async def decide(self, kind: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        """kind: plan | tool_call | question. key: stable id so a resumed run finds its answer.

        Returns e.g. {"approved": True}, {"approved": False, "reason": "..."},
        {"approved": True, "plan": {...edited...}}, or {"answer": "..."}.
        """
        ...


class DetachedHuman:
    def __init__(
        self, db: DB, run_id: str, emitter: Emitter, prefilled: dict[str, dict[str, Any]] | None = None
    ):
        self.db = db
        self.run_id = run_id
        self.emitter = emitter
        self.prefilled = dict(prefilled or {})

    async def decide(self, kind: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        if key in self.prefilled:
            return self.prefilled[key]
        approval_id = self.db.create_approval(self.run_id, kind, {"key": key, **payload})
        self.emitter.emit(
            "approval_requested" if kind != QUESTION else "question_asked",
            approval_id=approval_id,
            kind=kind,
            key=key,
            **payload,
        )
        raise Interrupt(approval_id, kind, payload)


class AutoHuman:
    """Approves everything and answers questions with a fixed string. Used by evals/tests."""

    def __init__(self, answer: str = "Use your best judgement."):
        self.answer = answer
        self.decisions: list[tuple[str, str, dict[str, Any]]] = []

    async def decide(self, kind: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.decisions.append((kind, key, payload))
        return {"answer": self.answer} if kind == QUESTION else {"approved": True}
