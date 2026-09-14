import io
import json

import pytest
from rich.console import Console

from sea import cli
from sea.agents.orchestrator import Orchestrator
from sea.db import DB
from sea.events import TYPES, Event
from sea.llm import FakeProvider, tool_call
from tests.conftest import plan_json


@pytest.fixture
def env(workspace, monkeypatch):
    """Real on-disk DB in tmp, local sandbox, fake providers, captured console."""
    monkeypatch.setenv("SANDBOX", "local")
    monkeypatch.setattr("sea.config.settings.auto_approve", "none")
    out = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=out, width=120, force_terminal=False))
    providers: dict[str, FakeProvider] = {}
    monkeypatch.setattr(Orchestrator, "provider", lambda self, role: providers[role])
    return providers, out


def test_full_hitl_flow_through_cli(env, monkeypatch, workspace):
    providers, out = env
    providers["planner"] = FakeProvider([plan_json({"id": "s1", "description": "write the file"})])
    providers["worker"] = FakeProvider([tool_call("write_file", {"path": "a.txt", "content": "hi"}), "done"])

    # 1. detached run pauses at the plan gate
    with pytest.raises(SystemExit):
        cli.main(["run", "--detached", "-c", "conv1", "write a.txt"])
    db = DB()
    run = db.list_runs()[0]
    assert run["status"] == "awaiting_plan_approval"
    assert "paused (plan)" in out.getvalue()

    # 2. approvals lists it
    cli.main(["approvals"])
    assert run["id"] in out.getvalue() and "Pending plan approval" in out.getvalue()

    # 3. resume: approve plan, then approve the write_file call, run completes
    answers = iter([{"approved": True}, {"approved": True}])
    monkeypatch.setattr(cli.ConsoleHuman, "_prompt", lambda self, kind, payload: next(answers))
    cli.main(["resume", run["id"]])
    run = db.get_run(run["id"])
    assert run["status"] == "completed" and run["answer"] == "done"
    assert (workspace / "conv1" / "a.txt").read_text() == "hi"
    assert [a["status"] for a in db._all("SELECT status FROM approvals ORDER BY created_at")] == [
        "approved",
        "approved",
    ]

    # 4. replay shows the whole story, nothing pending
    out.truncate(0), out.seek(0)
    cli.main(["runs", run["id"]])
    text = out.getvalue()
    assert "Answer" in text and "→ sea resume" not in text  # nothing pending after completion


def test_resume_denial_and_cancel(env, monkeypatch, workspace):
    providers, out = env
    providers["planner"] = FakeProvider([plan_json({"id": "s1", "description": "write"})])
    providers["worker"] = FakeProvider([tool_call("write_file", {"path": "a.txt", "content": "hi"})])
    with pytest.raises(SystemExit):
        cli.main(["run", "--detached", "write"])
    db = DB()
    rid = db.list_runs()[0]["id"]

    # approve plan, then deny the write -> worker script is exhausted on purpose, run fails cleanly
    answers = iter([{"approved": True}, {"approved": False, "reason": "no"}])
    monkeypatch.setattr(cli.ConsoleHuman, "_prompt", lambda self, kind, payload: next(answers))
    with pytest.raises(SystemExit):
        cli.main(["resume", rid])
    assert db.get_run(rid)["status"] == "failed"
    assert not (workspace / db.get_run(rid)["conversation_id"] / "a.txt").exists()

    with pytest.raises(SystemExit):  # already finished
        cli.main(["cancel", rid])

    # a fresh paused run can be cancelled; its approval is denied
    providers["planner"] = FakeProvider([plan_json({"id": "s1", "description": "x"})])
    with pytest.raises(SystemExit):
        cli.main(["run", "--detached", "again"])
    rid2 = db.list_runs()[0]["id"]
    cli.main(["cancel", rid2])
    assert db.get_run(rid2)["status"] == "cancelled" and db.pending_approvals(rid2) == []
    with pytest.raises(SystemExit):
        cli.main(["resume", rid2])


def test_plan_prompt_edit_path(env, monkeypatch):
    providers, out = env
    plan = json.loads(plan_json({"id": "s1", "description": "old"}, {"id": "s2", "description": "keep"}))
    prompts = iter(["e", "s1", "new text"])
    monkeypatch.setattr(cli.Prompt, "ask", staticmethod(lambda *a, **k: next(prompts)))
    monkeypatch.setattr(cli.Confirm, "ask", staticmethod(lambda *a, **k: True))
    human = cli.ConsoleHuman(DB(":memory:"), "r", None, {})
    decision = human._prompt("plan", {"plan": plan})
    assert decision["approved"] and decision["plan"]["steps"][0]["description"] == "new text"
    assert decision["plan"]["steps"][1]["description"] == "keep"
    assert "Edited plan" in out.getvalue()


def test_render_handles_every_event_type(env):
    _, out = env
    plan = {"steps": [{"id": "s1", "description": "d", "tools": ["x"], "missing_tools": [{"name": "m"}]}]}
    sample = {
        "task": "t", "plan": plan,
        "name": "n", "description": "[weird] markup", "passed": True, "attempt": 1, "version": 2,
        "error": "[e]", "step_id": "s1", "model": "m", "tokens_in": 1, "tokens_out": 2, "tool_calls": ["a"],
        "arguments": {"a": "[b]"}, "output": "[null, 0.0]", "answer": "[ok]", "kind": "plan", "run_id": "r",
        "reason": "r", "replans": 1, "question": "q?", "approval_id": "a", "key": "k",
    }  # fmt: skip
    for t in sorted(TYPES):
        cli.render(Event(t, dict(sample), 1))
    text = out.getvalue()
    assert "[null, 0.0]" in text and "[weird] markup" in text and "[ok]" in text
