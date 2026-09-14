"""Run the benchmark against real models and the real sandbox.

    uv run python -m evals.run_evals                      # everything
    uv run python -m evals.run_evals write edit            # categories or task ids
    uv run python -m evals.run_evals --merge hitl allow    # re-run some tasks into the latest results

Each task gets its own SQLite file and workspace under evals/.work/<timestamp>/. Results go
to evals/results/<timestamp>.json and evals/results/latest.md. A full run is ~150k tokens,
which is about one run per day on Groq's free tier for a single model - hence --merge.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.tasks import TASKS, Task
from sea.agents.orchestrator import Orchestrator
from sea.config import settings
from sea.db import DB
from sea.events import Emitter
from sea.interrupts import QUESTION
from sea.sandbox.runner import get_sandbox

ROOT = Path(__file__).parent
TASK_TIMEOUT = 300


class EvalHuman:
    """Approves everything except the tools a task says to deny; answers questions vaguely so the
    agent has to cope, and records what it was asked."""

    def __init__(self, db: DB, run_id: str, emitter: Emitter, prefilled: dict[str, Any], deny: set[str]):
        self.db, self.run_id, self.emitter, self.deny = db, run_id, emitter, deny

    async def decide(self, kind: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        aid = self.db.create_approval(self.run_id, kind, {"key": key, **payload})
        if kind == QUESTION:
            self.emitter.emit("question_asked", approval_id=aid, kind=kind, key=key, **payload)
            decision = {"answer": "I can't give more detail than the task already has. Use your judgement."}
        elif kind == "tool_call" and payload["name"] in self.deny:
            decision = {"approved": False, "reason": "not allowed in this environment"}
        else:
            decision = {"approved": True}
        self.db.resolve_approval(aid, "answered" if kind == QUESTION else "approved", decision)
        return decision


async def run_task(task: Task, work: Path) -> dict[str, Any]:
    task_dir = work / task.id
    task_dir.mkdir(parents=True)
    settings.db_path = task_dir / "sea.db"
    settings.workspaces_dir = task_dir / "workspaces"
    settings.auto_approve = "safe"  # plans auto-approved; tool calls go through EvalHuman

    db = DB(settings.db_path)
    for t in task.seed_tools:
        db.insert_tool(**t)
    cid = db.create_conversation(title=task.id)
    ws = settings.workspace_for(cid)
    for name, content in task.files.items():
        (ws / name).parent.mkdir(parents=True, exist_ok=True)
        (ws / name).write_text(content)

    def human(db: DB, run_id: str, emitter: Emitter, prefilled: dict[str, Any]) -> EvalHuman:
        return EvalHuman(db, run_id, emitter, prefilled, task.deny_tools)

    orch = Orchestrator(db, get_sandbox(), human_factory=human)
    started = time.time()
    record: dict[str, Any] = {
        "id": task.id,
        "category": task.category,
        "task": task.task,
        "models": [settings.planner_model, settings.toolsmith_model, settings.worker_model],
        "ran_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        run = await asyncio.wait_for(orch.start(cid, task.task), TASK_TIMEOUT)
        events = db.get_events(run["id"])
        try:
            passed = run["status"] == task.expect_status and bool(task.check(run, db, events, ws))
        except Exception as e:  # a check that crashes is a fail with a reason
            passed, run["error"] = False, f"check raised {type(e).__name__}: {e}"
        record.update(
            status="pass" if passed else "fail",
            run_status=run["status"],
            answer=(run.get("answer") or "")[:600],
            error=run.get("error"),
            tokens_in=run["tokens_in"],
            tokens_out=run["tokens_out"],
            tools_built=[e["payload"]["name"] for e in events if e["type"] == "tool_registered"],
            llm_calls=sum(1 for e in events if e["type"] == "llm_called"),
            steps=len(run["plan"]["steps"]) if run.get("plan") else 0,
        )
    except TimeoutError:
        record.update(status="timeout", run_status="running")
    except Exception as e:
        record.update(status="error", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-1500:])
    record["seconds"] = round(time.time() - started, 1)
    db.close()
    return record


def summarize(results: list[dict[str, Any]]) -> str:
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    lines = ["| category | passed | tokens | avg s |", "|---|---|---|---|"]
    for cat, rs in by_cat.items():
        passed = sum(r["status"] == "pass" for r in rs)
        tokens = sum(r.get("tokens_in", 0) + r.get("tokens_out", 0) for r in rs)
        secs = sum(r["seconds"] for r in rs) / len(rs)
        lines.append(f"| {cat} | {passed}/{len(rs)} | {tokens:,} | {secs:.0f} |")
    total = sum(r["status"] == "pass" for r in results)
    lines.append(
        f"| **total** | **{total}/{len(results)}** | "
        f"{sum(r.get('tokens_in', 0) + r.get('tokens_out', 0) for r in results):,} | |"
    )
    lines += ["", "| task | result | tools built | llm calls | note |", "|---|---|---|---|---|"]
    for r in results:
        mark = {"pass": "✅", "fail": "❌", "timeout": "⏱", "error": "💥"}[r["status"]]
        note = (r.get("error") or r.get("answer") or "").replace("\n", " ").replace("|", "/")[:90]
        lines.append(
            f"| {r['id']} | {mark} | {', '.join(r.get('tools_built', [])) or '-'} | {r.get('llm_calls', '-')} | {note} |"
        )
    return "\n".join(lines)


async def main(selected: list[str]) -> int:
    tasks = [
        t
        for t in TASKS
        if not selected
        or t.id in selected
        or t.category in selected
        or any(t.id.startswith(s) for s in selected)
    ]
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    work = ROOT / ".work" / stamp
    results = []
    print(
        f"running {len(tasks)} tasks · models: {settings.planner_model} / {settings.toolsmith_model} / {settings.worker_model}"
    )
    for task in tasks:
        r = await run_task(task, work)
        results.append(r)
        if r.get("run_status") == "failed" and "rate_limit_exceeded" in (r.get("error") or ""):
            print("  rate limit hit - stopping; re-run the rest later with --merge")
            results = results[:-1]
            break
        print(
            f"  {r['status']:7} {task.id:14} {r['seconds']:5.0f}s  {r.get('tokens_in', 0) + r.get('tokens_out', 0):6,} tok  {(r.get('error') or '')[:80]}"
        )

    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    meta = {
        "timestamp": stamp,
        "models": {
            "planner": settings.planner_model,
            "toolsmith": settings.toolsmith_model,
            "worker": settings.worker_model,
        },
    }
    (out / f"{stamp}.json").write_text(json.dumps({**meta, "results": results}, indent=2))
    table = summarize(results)
    (out / "latest.md").write_text(f"# Eval results · {stamp}\n\nModels: `{meta['models']}`\n\n{table}\n")
    print("\n" + table)
    shutil.rmtree(work, ignore_errors=True)
    return 0 if all(r["status"] == "pass" for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
