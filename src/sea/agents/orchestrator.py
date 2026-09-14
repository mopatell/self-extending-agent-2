"""Orchestrator: owns a run from task to answer, and can pause/resume it.

State lives in one dict that is checkpointed to `runs.state_json` whenever
something meaningful happens, so a run can be resumed after a human interrupt
or a process restart.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sea.agents import toolsmith, worker
from sea.config import settings
from sea.db import DB
from sea.events import Emitter, Listener
from sea.interrupts import PLAN, DetachedHuman, Human, Interrupt
from sea.llm import Message, Provider, get_provider
from sea.sandbox.runner import Sandbox
from sea.tools.base import Tool, ToolContext
from sea.tools.builtin import builtin_tools
from sea.tools.registry import Registry

HumanFactory = Callable[[DB, str, Emitter, dict[str, dict[str, Any]]], Human]


def detached_human(db: DB, run_id: str, emitter: Emitter, prefilled: dict[str, dict[str, Any]]) -> Human:
    return DetachedHuman(db, run_id, emitter, prefilled)


class Orchestrator:
    def __init__(
        self,
        db: DB,
        sandbox: Sandbox,
        human_factory: HumanFactory = detached_human,
        listeners: list[Listener] | None = None,
        providers: dict[str, Provider] | None = None,
    ):
        self.db = db
        self.sandbox = sandbox
        self.human_factory = human_factory
        self.listeners = listeners or []
        self.providers = providers or {}
        self.registry = Registry(db, sandbox)

    def provider(self, role: str) -> Provider:
        if role in self.providers:
            return self.providers[role]
        model = getattr(settings, f"{role}_model")
        return get_provider(model)

    # ------------------------------------------------------------------ public

    async def start(self, conversation_id: str, task: str) -> dict[str, Any]:
        run_id = self.db.create_run(conversation_id, task)
        self.db.add_message(conversation_id, "user", task)
        emitter = Emitter(self.db, run_id, self.listeners)
        emitter.emit("run_started", task=task)
        state = {"phase": "planning", "plan": None, "steps": {}}
        return await self._drive(run_id, state, emitter, prefilled={})

    async def resume(self, run_id: str, approval_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        approval = self.db.get_approval(approval_id)
        if not run or not approval or approval["run_id"] != run_id:
            raise ValueError("unknown run/approval")
        if approval["status"] != "pending":
            raise ValueError("approval already resolved")
        status = (
            "answered"
            if approval["kind"] == "question"
            else ("approved" if decision.get("approved") else "denied")
        )
        self.db.resolve_approval(approval_id, status, decision)
        emitter = Emitter(self.db, run_id, self.listeners)
        emitter.emit("run_resumed", approval_id=approval_id, kind=approval["kind"])
        state = run["state"]
        _restore(state)
        return await self._drive(run_id, state, emitter, prefilled={approval["payload"]["key"]: decision})

    # ------------------------------------------------------------------ state machine

    async def _drive(
        self, run_id: str, state: dict[str, Any], emitter: Emitter, prefilled: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        human = self.human_factory(self.db, run_id, emitter, prefilled)
        try:
            self.db.update_run(run_id, status="running")
            answer = await self._execute(run, state, emitter, human)
        except Interrupt as i:
            status = (
                "awaiting_input"
                if i.kind == "question"
                else ("awaiting_plan_approval" if i.kind == PLAN else "awaiting_approval")
            )
            self._checkpoint(run_id, state, status=status)
            emitter.emit("run_paused", approval_id=i.approval_id, kind=i.kind, run_id=run_id)
        except Exception as e:
            self._checkpoint(run_id, state, status="failed", error=f"{type(e).__name__}: {e}")
            emitter.emit("run_failed", error=f"{type(e).__name__}: {e}")
        else:
            self._checkpoint(run_id, state, status="completed", answer=answer)
            self.db.add_message(run["conversation_id"], "assistant", answer)
            emitter.emit("run_completed", answer=answer)
        return self.db.get_run(run_id)

    async def _execute(
        self, run: dict[str, Any], state: dict[str, Any], emitter: Emitter, human: Human
    ) -> str:
        run_id, task = run["id"], run["task"]

        if state["plan"] is None:
            # M1: one step. M3 replaces this with the planner + approval gate.
            state["plan"] = {
                "steps": [
                    {"id": "s1", "description": task, "depends_on": [], "tools": [], "missing_tools": []}
                ]
            }
            for s in state["plan"]["steps"]:
                state["steps"][s["id"]] = {"status": "pending", "messages": [], "result": None}
            self.db.update_run(run_id, plan=state["plan"])
        state["phase"] = "running"

        tools = self._tools()
        workspace = settings.workspace_for(run["conversation_id"])
        results: dict[str, str] = {}
        smith = self._toolsmith(run_id, workspace, emitter, tools)

        for step in state["plan"]["steps"]:
            sstate = state["steps"][step["id"]]
            if sstate["status"] == "done":
                results[step["id"]] = sstate["result"]
                continue
            step_emitter = emitter.child(step_id=step["id"])
            if not sstate["messages"]:
                sstate["messages"] = worker.build_messages(step, task, results, tools)
                step_emitter.emit("step_started", description=step["description"])
            sstate["status"] = "running"
            ctx = ToolContext(
                workspace=workspace,
                sandbox=self.sandbox,
                human=human,
                step_id=step["id"],
                tools=tools,
                toolsmith=smith,
            )
            result = await worker.run_step(
                provider=self.provider("worker"),
                messages=sstate["messages"],
                tools=tools,
                ctx=ctx,
                emitter=step_emitter,
                human=human,
                db=self.db,
                run_id=run_id,
            )
            sstate["status"], sstate["result"] = "done", result.text
            results[step["id"]] = result.text
            step_emitter.emit("step_completed", result=result.text, llm_steps=result.steps)
            self._checkpoint(run_id, state)

        if len(results) == 1:
            return next(iter(results.values()))
        return "\n\n".join(f"[{sid}] {r}" for sid, r in results.items())

    def _tools(self) -> dict[str, Tool]:
        return {**builtin_tools(), **self.registry.load()}

    def _toolsmith(self, run_id: str, workspace: Any, emitter: Emitter, tools: dict[str, Tool]):
        """Closure the worker's build_tool/fix_tool built-ins call. Adds the result to the live tool set."""
        reserved = set(builtin_tools())

        async def smith(
            capability: str | None = None, edit_name: str | None = None, problem: str | None = None
        ) -> str:
            result = await toolsmith.build_tool(
                provider=self.provider("toolsmith"),
                registry=self.registry,
                workspace=workspace,
                emitter=emitter,
                db=self.db,
                run_id=run_id,
                capability=capability,
                edit_name=edit_name,
                problem=problem,
                reserved=reserved,
            )
            if result.ok and result.name:
                tools[result.name] = self.registry.load()[result.name]
            return result.message

        return smith

    def _checkpoint(self, run_id: str, state: dict[str, Any], **fields: Any) -> None:
        self.db.update_run(run_id, state=_serialize(state), **fields)


def _serialize(state: dict[str, Any]) -> dict[str, Any]:
    out = {**state, "steps": {}}
    for sid, s in state["steps"].items():
        out["steps"][sid] = {**s, "messages": [m.to_dict() for m in s["messages"]]}
    return out


def _restore(state: dict[str, Any]) -> None:
    for s in state["steps"].values():
        s["messages"] = [Message.from_dict(m) for m in s["messages"]]
