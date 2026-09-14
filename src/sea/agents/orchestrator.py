"""Orchestrator: owns a run from task to answer, and can pause/resume it.

Phases: plan -> (human approves) -> build missing tools -> run steps in waves -> answer.
State lives in one dict that is checkpointed to `runs.state_json` whenever
something meaningful happens, so a run can be resumed after a human interrupt
or a process restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sea.agents import planner, toolsmith, worker
from sea.agents.planner import Plan, Step
from sea.config import settings
from sea.db import DB
from sea.events import Emitter, Listener
from sea.interrupts import PLAN, QUESTION, DetachedHuman, Human, Interrupt
from sea.llm import Message, Provider, get_provider
from sea.sandbox.runner import Sandbox
from sea.tools.base import Tool, ToolContext
from sea.tools.builtin import builtin_tools
from sea.tools.registry import Registry

HumanFactory = Callable[[DB, str, Emitter, dict[str, dict[str, Any]]], Human]
MAX_REPLANS = 2


def detached_human(db: DB, run_id: str, emitter: Emitter, prefilled: dict[str, dict[str, Any]]) -> Human:
    return DetachedHuman(db, run_id, emitter, prefilled)


class StepFailed(Exception):
    pass


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
        return get_provider(getattr(settings, f"{role}_model"))

    # ------------------------------------------------------------------ public

    async def start(self, conversation_id: str, task: str) -> dict[str, Any]:
        run_id = self.db.create_run(conversation_id, task)
        self.db.add_message(conversation_id, "user", task)
        emitter = Emitter(self.db, run_id, self.listeners)
        emitter.emit("run_started", task=task)
        state = {"phase": "planning", "plan": None, "steps": {}, "replans": 0, "tool_builds": {}}
        return await self._drive(run_id, state, emitter, prefilled={})

    async def resume(self, run_id: str, approval_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        approval = self.db.get_approval(approval_id)
        if not run or not approval or approval["run_id"] != run_id:
            raise ValueError("unknown run/approval")
        if approval["status"] != "pending":
            raise ValueError("approval already resolved")
        if approval["kind"] == QUESTION:
            status = "answered"
        else:
            status = "approved" if decision.get("approved") else "denied"
        self.db.resolve_approval(approval_id, status, decision)
        emitter = Emitter(self.db, run_id, self.listeners)
        emitter.emit("run_resumed", approval_id=approval_id, kind=approval["kind"])
        state = run["state"]
        _restore(state)
        return await self._drive(run_id, state, emitter, prefilled={approval["payload"]["key"]: decision})

    # ------------------------------------------------------------------ driver

    async def _drive(
        self, run_id: str, state: dict[str, Any], emitter: Emitter, prefilled: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        run = self.db.get_run(run_id)
        human = self.human_factory(self.db, run_id, emitter, prefilled)
        try:
            self.db.update_run(run_id, status="running")
            answer = await self._execute(run, state, emitter, human)
        except Interrupt as i:
            status = {PLAN: "awaiting_plan_approval", QUESTION: "awaiting_input"}.get(
                i.kind, "awaiting_approval"
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
        tools = self._tools()
        workspace = settings.workspace_for(run["conversation_id"])
        smith = self._toolsmith(run_id, workspace, emitter, tools)

        if state["plan"] is None:
            await self._plan(run, state, emitter, human, tools)
        plan = Plan.model_validate(state["plan"])

        if state["phase"] == "building_tools":
            await self._build_missing(plan, state, tools, smith, emitter)
            state["phase"] = "running"
            self._checkpoint(run_id, state)

        results = await self._run_waves(plan, state, run, tools, workspace, smith, emitter, human)
        if all(s["status"] != "done" for s in state["steps"].values()):
            raise RuntimeError("every step failed: " + "; ".join(results.values()))
        return await self._answer(task, plan, results, emitter, run_id)

    # ------------------------------------------------------------------ phases

    async def _plan(
        self,
        run: dict[str, Any],
        state: dict[str, Any],
        emitter: Emitter,
        human: Human,
        tools: dict[str, Tool],
    ) -> None:
        run_id, task = run["id"], run["task"]
        catalog = "\n".join(t.catalog_line() for t in tools.values())
        feedback = state.get("plan_feedback")
        history = _history(self.db.get_messages(run["conversation_id"])[:-1])

        while True:
            if state.get("proposed_plan") is None:
                plan = await planner.make_plan(
                    provider=self.provider("planner"), task=task, catalog=catalog, emitter=emitter,
                    db=self.db, run_id=run_id, feedback=feedback, history=history,
                )  # fmt: skip
                state["proposed_plan"] = plan.model_dump()
                self._checkpoint(run_id, state)  # so a resume after approval doesn't re-plan
                emitter.emit("plan_proposed", plan=plan.model_dump())
            plan = Plan.model_validate(state["proposed_plan"])

            if settings.auto_approve in ("safe", "all"):
                decision: dict[str, Any] = {"approved": True}
            else:
                decision = await human.decide(PLAN, f"plan:{state['replans']}", {"plan": plan.model_dump()})

            if decision.get("approved"):
                if decision.get("plan"):
                    plan = planner.plan_from_dict(decision["plan"])
                emitter.emit("plan_approved", plan=plan.model_dump(), edited=bool(decision.get("plan")))
                break

            feedback = decision.get("reason") or "no reason given"
            state["replans"] += 1
            state["plan_feedback"] = feedback
            state["proposed_plan"] = None
            emitter.emit("plan_rejected", reason=feedback, replans=state["replans"])
            if state["replans"] > MAX_REPLANS:
                raise RuntimeError(f"plan rejected {state['replans']} times; giving up")

        state["plan"] = plan.model_dump()
        state["steps"] = {s.id: {"status": "pending", "messages": [], "result": None} for s in plan.steps}
        state["phase"] = "building_tools"
        self.db.update_run(run_id, plan=state["plan"])
        self._checkpoint(run_id, state)

    async def _build_missing(
        self, plan: Plan, state: dict[str, Any], tools: dict[str, Tool], smith: Any, emitter: Emitter
    ) -> None:
        wanted = {m.name: m.description for s in plan.steps for m in s.missing_tools}
        for name, description in wanted.items():
            if name in tools or name in state["tool_builds"]:
                continue
            message = await smith(capability=f"{name}: {description}")
            built = name in tools or any(t.generated and t.name in message for t in tools.values())
            state["tool_builds"][name] = {"ok": built, "message": message}

    async def _run_waves(
        self,
        plan: Plan,
        state: dict[str, Any],
        run: dict[str, Any],
        tools: dict[str, Tool],
        workspace: Path,
        smith: Any,
        emitter: Emitter,
        human: Human,
    ) -> dict[str, str]:
        results: dict[str, str] = {
            sid: s["result"] for sid, s in state["steps"].items() if s["status"] in ("done", "failed")
        }
        limit = asyncio.Semaphore(settings.max_parallel_steps)

        for wave in plan.waves():
            todo = [s for s in wave if state["steps"][s.id]["status"] not in ("done", "failed", "blocked")]
            for s in todo:
                if any(state["steps"][d]["status"] in ("failed", "blocked") for d in s.depends_on):
                    state["steps"][s.id]["status"] = "blocked"
                    results[s.id] = "(blocked: a step it depends on failed)"
                    emitter.child(step_id=s.id).emit("step_blocked", description=s.description)
            todo = [s for s in todo if state["steps"][s.id]["status"] != "blocked"]
            if not todo:
                continue

            async def one(step: Step) -> None:
                async with limit:
                    await self._run_step(step, state, run, tools, workspace, smith, emitter, human, results)

            outcomes = await asyncio.gather(*(one(s) for s in todo), return_exceptions=True)
            self._checkpoint(run["id"], state)
            interrupts = [o for o in outcomes if isinstance(o, Interrupt)]
            if interrupts:
                raise interrupts[0]
            for o in outcomes:
                if isinstance(o, BaseException) and not isinstance(o, StepFailed):
                    raise o
        return results

    async def _run_step(
        self,
        step: Step,
        state: dict[str, Any],
        run: dict[str, Any],
        tools: dict[str, Tool],
        workspace: Path,
        smith: Any,
        emitter: Emitter,
        human: Human,
        results: dict[str, str],
    ) -> None:
        sstate = state["steps"][step.id]
        step_emitter = emitter.child(step_id=step.id)
        if not sstate["messages"]:
            upstream = {d: results.get(d, "") for d in step.depends_on}
            notes = [
                b["message"]
                for n, b in state["tool_builds"].items()
                if not b["ok"] and n in [m.name for m in step.missing_tools]
            ]
            sstate["messages"] = worker.build_messages(step.model_dump(), run["task"], upstream, tools, notes)
            step_emitter.emit("step_started", description=step.description)
        sstate["status"] = "running"
        ctx = ToolContext(
            workspace=workspace,
            sandbox=self.sandbox,
            human=human,
            step_id=step.id,
            tools=tools,
            toolsmith=smith,
        )
        try:
            result = await worker.run_step(
                provider=self.provider("worker"), messages=sstate["messages"], tools=tools, ctx=ctx,
                emitter=step_emitter, human=human, db=self.db, run_id=run["id"],
            )  # fmt: skip
        except Interrupt:
            raise
        except Exception as e:
            sstate["status"], sstate["result"] = "failed", f"(failed: {type(e).__name__}: {e})"
            results[step.id] = sstate["result"]
            step_emitter.emit("step_failed", error=f"{type(e).__name__}: {e}")
            raise StepFailed() from e
        sstate["status"], sstate["result"] = "done", result.text
        results[step.id] = result.text
        step_emitter.emit("step_completed", result=result.text, llm_steps=result.steps)

    async def _answer(
        self, task: str, plan: Plan, results: dict[str, str], emitter: Emitter, run_id: str
    ) -> str:
        if len(plan.steps) == 1:
            return results[plan.steps[0].id]
        summary = "\n\n".join(f"[{s.id}] {s.description}\n{results.get(s.id, '')}" for s in plan.steps)
        provider = self.provider("worker")
        completion = await provider.complete(
            [
                Message.system(
                    "Write the final answer to the user's task from the step results below. Be direct and "
                    "factual; report numbers and findings exactly; mention any step that failed. No preamble."
                ),
                Message.user(f"Task: {task}\n\nStep results:\n{summary}"),
            ]
        )
        self.db.add_usage(run_id, completion.usage.input_tokens, completion.usage.output_tokens)
        emitter.emit(
            "llm_called", model=provider.model, tokens_in=completion.usage.input_tokens,
            tokens_out=completion.usage.output_tokens, tool_calls=[], role="answer",
        )  # fmt: skip
        return completion.text

    # ------------------------------------------------------------------ helpers

    def _tools(self) -> dict[str, Tool]:
        return {**builtin_tools(), **self.registry.load()}

    def _toolsmith(self, run_id: str, workspace: Path, emitter: Emitter, tools: dict[str, Tool]):
        """Closure the worker's build_tool/fix_tool built-ins call. Adds the result to the live tool set."""
        reserved = set(builtin_tools())

        async def smith(
            capability: str | None = None, edit_name: str | None = None, problem: str | None = None
        ) -> str:
            result = await toolsmith.build_tool(
                provider=self.provider("toolsmith"), registry=self.registry, workspace=workspace,
                emitter=emitter, db=self.db, run_id=run_id, capability=capability, edit_name=edit_name,
                problem=problem, reserved=reserved,
            )  # fmt: skip
            if result.ok and result.name:
                tools[result.name] = self.registry.load()[result.name]
            return result.message

        return smith

    def _checkpoint(self, run_id: str, state: dict[str, Any], **fields: Any) -> None:
        self.db.update_run(run_id, state=_serialize(state), **fields)


def _history(messages: list[dict[str, Any]], limit: int = 6, chars: int = 400) -> str | None:
    recent = messages[-limit:]
    if not recent:
        return None
    return "\n".join(f"{m['role']}: {m['content'][:chars]}" for m in recent)


def _serialize(state: dict[str, Any]) -> dict[str, Any]:
    out = {**state, "steps": {}}
    for sid, s in state["steps"].items():
        out["steps"][sid] = {**s, "messages": [m.to_dict() for m in s["messages"]]}
    return out


def _restore(state: dict[str, Any]) -> None:
    for s in state["steps"].values():
        s["messages"] = [Message.from_dict(m) for m in s["messages"]]
