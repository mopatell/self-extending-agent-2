"""`sea` command line. Streams run events to the terminal and answers approvals inline."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from sea.agents.orchestrator import Orchestrator
from sea.config import settings
from sea.db import DB
from sea.events import Emitter, Event
from sea.interrupts import PLAN, QUESTION
from sea.sandbox.runner import get_sandbox

console = Console()


# ----------------------------------------------------------------------------- rendering


def render(event: Event) -> None:
    p = event.payload
    t = event.type
    sid = f"[dim]{p['step_id']}[/] " if p.get("step_id") else ""
    if t == "run_started":
        console.print(f"[bold]▶ task:[/] {escape(p['task'])}")
    elif t == "plan_proposed":
        table = Table(title="Proposed plan", show_lines=False)
        table.add_column("id"), table.add_column("step"), table.add_column("after"), table.add_column("needs")
        for s in p["plan"]["steps"]:
            needs = ", ".join(s.get("tools", [])) + (
                f" [red]+ build: {', '.join(m['name'] for m in s.get('missing_tools', []))}[/]"
                if s.get("missing_tools")
                else ""
            )
            table.add_row(
                s["id"], escape(s["description"]), ", ".join(s.get("depends_on", [])) or "-", needs or "-"
            )
        console.print(table)
    elif t == "tool_gap_found":
        console.print(f"[yellow]⚒ missing tool:[/] {p['name']} — {_short(p['description'], 200)}")
    elif t == "tool_test_result":
        mark = "[green]passed[/]" if p["passed"] else "[red]failed[/]"
        console.print(f"  tests {mark} (attempt {p['attempt']})")
    elif t == "tool_registered":
        console.print(f"[green]✔ tool registered:[/] {p['name']} v{p['version']}")
    elif t == "tool_build_failed":
        console.print(f"[red]✘ could not build tool {p['name']}:[/] {_short(p['error'], 300)}")
    elif t == "step_started":
        console.print(f"{sid}[bold]● step:[/] {escape(p['description'])}")
    elif t == "llm_called":
        calls = f" → {', '.join(p['tool_calls'])}" if p["tool_calls"] else ""
        console.print(f"{sid}[dim]llm {p['model']} ({p['tokens_in']}↑ {p['tokens_out']}↓){calls}[/]")
    elif t == "tool_called":
        console.print(f"{sid}[cyan]⚙ {p['name']}[/]({_short(json.dumps(p['arguments']), 120)})")
    elif t == "tool_result":
        style = "red" if p["error"] else "dim"
        console.print(f"{sid}[{style}]  ↳ {_short(p['output'], 300)}[/]")
    elif t == "step_completed":
        console.print(f"{sid}[green]✔ step done[/]")
    elif t == "step_failed":
        console.print(f"{sid}[red]✘ step failed:[/] {_short(p.get('error', ''), 300)}")
    elif t == "run_completed":
        console.print(Panel(escape(p["answer"]), title="Answer", border_style="green"))
    elif t == "run_failed":
        console.print(Panel(escape(p["error"]), title="Run failed", border_style="red"))
    elif t == "run_paused":
        console.print(f"[yellow]⏸ paused ({p['kind']}). Resume with:[/] sea resume {p['run_id']}")


def plan_table(plan: dict[str, Any], title: str = "Proposed plan") -> Table:
    table = Table(title=title, show_lines=False)
    table.add_column("id"), table.add_column("step"), table.add_column("after"), table.add_column("needs")
    for s in plan["steps"]:
        needs = ", ".join(s.get("tools", [])) + (
            f" [red]+ build: {', '.join(m['name'] for m in s.get('missing_tools', []))}[/]"
            if s.get("missing_tools")
            else ""
        )
        table.add_row(
            s["id"], escape(s["description"]), ", ".join(s.get("depends_on", [])) or "-", needs or "-"
        )
    return table


def _short(text: str, n: int) -> str:
    text = text.replace("\n", " ⏎ ")
    return escape(text if len(text) <= n else text[:n] + "…")


# ----------------------------------------------------------------------------- interactive human


class ConsoleHuman:
    """Prompts in the terminal. Records the approval in the DB too, for the audit trail."""

    def __init__(self, db: DB, run_id: str, emitter: Emitter, prefilled: dict[str, dict[str, Any]]):
        self.db, self.run_id, self.emitter, self.prefilled = db, run_id, emitter, prefilled

    async def decide(self, kind: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        if key in self.prefilled:
            return self.prefilled[key]
        aid = self.db.create_approval(self.run_id, kind, {"key": key, **payload})
        self.emitter.emit(
            "question_asked" if kind == QUESTION else "approval_requested",
            approval_id=aid,
            kind=kind,
            key=key,
            **payload,
        )
        decision = await asyncio.to_thread(self._prompt, kind, payload)
        status = "answered" if kind == QUESTION else ("approved" if decision.get("approved") else "denied")
        self.db.resolve_approval(aid, status, decision)
        return decision

    def _prompt(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == QUESTION:
            console.print(
                Panel(escape(payload["question"]), title="The agent has a question", border_style="yellow")
            )
            return {"answer": Prompt.ask("Your answer")}
        if kind == PLAN:
            return self._plan_prompt(payload["plan"])
        args = json.dumps(payload["arguments"], indent=2)
        body = f"[bold]{payload['name']}[/]\n{escape(args)}\n\n[dim]{escape(payload['reason'])}[/]"
        console.print(Panel(body, title="Approve this action?", border_style="yellow"))
        if Confirm.ask("Allow?", default=True):
            return {"approved": True}
        return {"approved": False, "reason": Prompt.ask("Reason (sent to the agent)", default="")}

    def _plan_prompt(self, plan: dict[str, Any]) -> dict[str, Any]:
        while True:
            choice = Prompt.ask(
                "Plan: (a)pprove, (e)dit a step, (r)eject", choices=["a", "e", "r"], default="a"
            )
            if choice == "a":
                return {"approved": True}
            if choice == "r":
                return {"approved": False, "reason": Prompt.ask("Why? (sent to the planner)", default="")}
            ids = [s["id"] for s in plan["steps"]]
            sid = Prompt.ask("Which step?", choices=ids)
            step = next(s for s in plan["steps"] if s["id"] == sid)
            step["description"] = Prompt.ask("New description", default=step["description"])
            console.print(plan_table(plan, title="Edited plan"))
            if Confirm.ask("Approve this plan?", default=True):
                return {"approved": True, "plan": plan}


def show_pending(db: DB, run_id: str) -> list[dict[str, Any]]:
    pending = db.pending_approvals(run_id)
    for a in pending:
        p = a["payload"]
        if a["kind"] == PLAN:
            console.print(plan_table(p["plan"], title=f"Pending plan approval {a['id']}"))
        elif a["kind"] == QUESTION:
            console.print(
                Panel(escape(p["question"]), title=f"Pending question {a['id']}", border_style="yellow")
            )
        else:
            args = escape(json.dumps(p["arguments"]))
            console.print(f"[yellow]pending approval {a['id']}:[/] {p['name']}({_short(args, 200)})")
    return pending


# ----------------------------------------------------------------------------- commands


def _orchestrator(db: DB, interactive: bool) -> Orchestrator:
    kwargs: dict[str, Any] = {"listeners": [render]}
    if interactive:
        kwargs["human_factory"] = ConsoleHuman
    return Orchestrator(db, get_sandbox(), **kwargs)


def cmd_run(args: argparse.Namespace) -> None:
    db = DB()
    cid = db.create_conversation(title=args.task[:60], cid=args.conversation)
    interactive = not args.detached and sys.stdin.isatty()
    run = asyncio.run(_orchestrator(db, interactive).start(cid, args.task))
    console.print(
        f"[dim]run {run['id']} · conversation {cid} · {run['tokens_in']}↑ {run['tokens_out']}↓ tokens[/]"
    )
    if run["status"] not in ("completed",):
        sys.exit(1)


def cmd_resume(args: argparse.Namespace) -> None:
    db = DB()
    run = db.get_run(args.run_id)
    if not run:
        console.print("[red]no such run[/]")
        sys.exit(1)
    pending = db.pending_approvals(args.run_id)
    if not pending:
        console.print(f"[red]nothing pending for this run (status: {run['status']})[/]")
        sys.exit(1)
    console.print(f"[bold]▶ task:[/] {escape(run['task'])}")
    human = ConsoleHuman(db, args.run_id, Emitter(db, args.run_id, []), {})
    orch = _orchestrator(db, True)
    while pending:
        approval = pending[0]
        if approval["kind"] == PLAN:
            console.print(plan_table(approval["payload"]["plan"]))
        decision = human._prompt(approval["kind"], approval["payload"])
        run = asyncio.run(orch.resume(args.run_id, approval["id"], decision))
        pending = db.pending_approvals(args.run_id) if run["status"].startswith("awaiting") else []
    console.print(f"[dim]run {run['id']} → {run['status']}[/]")
    if run["status"] != "completed":
        sys.exit(1)


def cmd_cancel(args: argparse.Namespace) -> None:
    db = DB()
    run = db.get_run(args.run_id)
    if not run:
        console.print("[red]no such run[/]")
        sys.exit(1)
    if run["status"] in ("completed", "failed", "cancelled"):
        console.print(f"[red]run is already {run['status']}[/]")
        sys.exit(1)
    for a in db.pending_approvals(args.run_id):
        db.resolve_approval(a["id"], "denied", {"approved": False, "reason": "run cancelled"})
    db.update_run(args.run_id, status="cancelled")
    Emitter(db, args.run_id, []).emit("run_failed", error="cancelled by user")
    console.print(f"[dim]run {run['id']} → cancelled[/]")


def cmd_approvals(args: argparse.Namespace) -> None:
    db = DB()
    waiting = [r for r in db.list_runs(limit=200) if r["status"].startswith("awaiting")]
    if not waiting:
        console.print("[dim]no runs are waiting on a human[/]")
        return
    for r in waiting:
        console.print(f"[bold]{r['id']}[/] {_short(r['task'], 70)}  [dim]{r['status']}[/]")
        show_pending(db, r["id"])
        console.print(f"[dim]→ sea resume {r['id']}[/]\n")


def cmd_runs(args: argparse.Namespace) -> None:
    db = DB()
    if args.run_id:
        run = db.get_run(args.run_id)
        if not run:
            console.print("[red]no such run[/]")
            sys.exit(1)
        tokens = f"{run['tokens_in']}↑ {run['tokens_out']}↓"
        console.print(f"[bold]{escape(run['task'])}[/]  status={run['status']}  tokens {tokens}")
        for e in db.get_events(args.run_id):
            render(Event(e["type"], e["payload"], e["seq"]))
        if run["status"].startswith("awaiting"):
            show_pending(db, args.run_id)
            console.print(f"[dim]→ sea resume {args.run_id}[/]")
        return
    table = Table(title="Runs")
    for col in ("id", "status", "task", "tokens", "created"):
        table.add_column(col)
    for r in db.list_runs(limit=args.limit):
        table.add_row(
            r["id"],
            r["status"],
            _short(r["task"], 60),
            f"{r['tokens_in']}↑{r['tokens_out']}↓",
            r["created_at"][:19],
        )
    console.print(table)


def cmd_tools(args: argparse.Namespace) -> None:
    db = DB()
    if args.name:
        t = db.latest_tool(args.name)
        if not t:
            console.print("[red]no such tool[/]")
            sys.exit(1)
        console.print(
            Panel(escape(t["source"]), title=f"{t['name']} v{t['version']} — {escape(t['description'])}")
        )
        console.print(Panel(escape(t["test_code"]), title="tests", border_style="dim"))
        return
    table = Table(title="Agent-written tools")
    for col in ("name", "ver", "risk", "calls", "fails", "description"):
        table.add_column(col)
    for t in db.active_tools():
        table.add_row(
            t["name"], str(t["version"]), t["risk"], str(t["calls"]), str(t["failures"]), t["description"]
        )
    console.print(table)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sea", description="Self-extending agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run a task")
    p.add_argument("task")
    p.add_argument("--conversation", "-c", help="conversation id to use (created if new)")
    p.add_argument("--detached", action="store_true", help="don't prompt; pause the run on approvals")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("resume", help="answer a pending approval and continue a paused run")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("cancel", help="cancel a paused or running run")
    p.add_argument("run_id")
    p.set_defaults(fn=cmd_cancel)

    p = sub.add_parser("approvals", help="list every run waiting on a human")
    p.set_defaults(fn=cmd_approvals)

    p = sub.add_parser("runs", help="list runs, or replay one")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_runs)

    p = sub.add_parser("tools", help="list agent-written tools, or show one")
    p.add_argument("name", nargs="?")
    p.set_defaults(fn=cmd_tools)

    args = parser.parse_args(argv)
    if args.cmd == "run":
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    args.fn(args)


if __name__ == "__main__":
    main()
