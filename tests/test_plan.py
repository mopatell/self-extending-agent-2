import asyncio
import json

import pytest

from sea.agents.orchestrator import Orchestrator
from sea.agents.planner import Plan, parse_plan
from sea.db import DB
from sea.llm import Completion, FakeProvider, ToolCall, tool_call
from sea.sandbox.runner import LocalSandbox
from tests.conftest import plan_json
from tests.test_tools import GOOD

# ----------------------------------------------------------------------------- planner


def test_parse_plan_and_waves():
    plan = parse_plan(
        "Sure:\n"
        + plan_json(
            {"id": "s1", "description": "fetch A"},
            {"id": "s2", "description": "fetch B"},
            {"id": "s3", "description": "combine", "depends_on": ["s1", "s2"]},
        )
    )
    assert [[s.id for s in w] for w in plan.waves()] == [["s1", "s2"], ["s3"]]


@pytest.mark.parametrize(
    "steps, error",
    [
        ([{"id": "s1", "description": "a", "depends_on": ["s9"]}], "unknown step"),
        ([{"id": "s1", "description": "a", "depends_on": ["s1"]}], "itself"),
        (
            [
                {"id": "s1", "description": "a", "depends_on": ["s2"]},
                {"id": "s2", "description": "b", "depends_on": ["s1"]},
            ],
            "cycle",
        ),
        ([{"id": "s1", "description": "a"}, {"id": "s1", "description": "b"}], "unique"),
        ([{"id": "step1", "description": "a"}], "id"),
        ([], "steps"),
    ],
)
def test_parse_plan_rejects(steps, error):
    with pytest.raises(ValueError, match=error):
        parse_plan(plan_json(*steps))


def test_parse_plan_no_json():
    with pytest.raises(ValueError, match="no JSON"):
        parse_plan("I cannot plan this")


# ----------------------------------------------------------------------------- orchestrator


def orch(db, **providers):
    return Orchestrator(db, LocalSandbox(), providers=providers)


async def test_planner_retry_then_fallback(db: DB, workspace):
    planner = FakeProvider(["garbage", "still garbage"])
    worker = FakeProvider(["done"])
    run = await orch(db, planner=planner, worker=worker).start(db.create_conversation(), "the task")
    assert run["status"] == "completed"
    assert run["plan"]["steps"][0]["description"] == "the task"  # fell back to a single step
    assert "invalid" in planner.requests[1]["messages"][-1].content


async def test_plan_approval_gate_pauses_and_resumes(db: DB, workspace, monkeypatch):
    monkeypatch.setattr("sea.config.settings.auto_approve", "none")
    planner = FakeProvider([plan_json({"id": "s1", "description": "x"})])
    worker = FakeProvider(["done"])
    o = orch(db, planner=planner, worker=worker)
    run = await o.start(db.create_conversation(), "t")
    assert run["status"] == "awaiting_plan_approval"
    approval = db.pending_approvals(run["id"])[0]
    assert approval["kind"] == "plan" and approval["payload"]["plan"]["steps"][0]["id"] == "s1"

    run = await o.resume(run["id"], approval["id"], {"approved": True})
    assert run["status"] == "completed" and len(planner.requests) == 1


async def test_plan_rejection_replans_with_feedback(db: DB, workspace, monkeypatch):
    monkeypatch.setattr("sea.config.settings.auto_approve", "none")
    planner = FakeProvider(
        [plan_json({"id": "s1", "description": "bad"}), plan_json({"id": "s1", "description": "good"})]
    )
    worker = FakeProvider(["done"])
    o = orch(db, planner=planner, worker=worker)
    run = await o.start(db.create_conversation(), "t")
    aid = db.pending_approvals(run["id"])[0]["id"]
    run = await o.resume(run["id"], aid, {"approved": False, "reason": "split it differently"})
    assert run["status"] == "awaiting_plan_approval"
    assert "split it differently" in planner.requests[1]["messages"][-1].content
    aid = db.pending_approvals(run["id"])[0]["id"]
    run = await o.resume(run["id"], aid, {"approved": True})
    assert run["status"] == "completed" and run["plan"]["steps"][0]["description"] == "good"
    types = [e["type"] for e in db.get_events(run["id"])]
    assert types.count("plan_proposed") == 2 and "plan_rejected" in types


async def test_human_can_edit_plan(db: DB, workspace, monkeypatch):
    monkeypatch.setattr("sea.config.settings.auto_approve", "none")
    planner = FakeProvider([plan_json({"id": "s1", "description": "original"})])
    worker = FakeProvider(["done"])
    o = orch(db, planner=planner, worker=worker)
    run = await o.start(db.create_conversation(), "t")
    aid = db.pending_approvals(run["id"])[0]["id"]
    edited = json.loads(plan_json({"id": "s1", "description": "edited by human"}))
    run = await o.resume(run["id"], aid, {"approved": True, "plan": edited})
    assert run["plan"]["steps"][0]["description"] == "edited by human"
    assert "edited by human" in worker.requests[0]["messages"][1].content


async def test_missing_tools_are_built_before_steps_run(db: DB, workspace):
    planner = FakeProvider(
        [
            plan_json(
                {
                    "id": "s1",
                    "description": "count",
                    "missing_tools": [{"name": "word_count", "description": "count words in text"}],
                }
            )
        ]
    )
    smith = FakeProvider([json.dumps(GOOD)])
    worker = FakeProvider([tool_call("word_count", {"text": "a b"}), "2 words"])
    run = await orch(db, planner=planner, toolsmith=smith, worker=worker).start(db.create_conversation(), "t")
    assert run["status"] == "completed" and run["answer"] == "2 words"
    types = [e["type"] for e in db.get_events(run["id"])]
    assert types.index("tool_registered") < types.index("step_started")
    assert "word_count" in [t.name for t in worker.requests[0]["tools"]]
    assert "word_count: count words in text" in smith.requests[0]["messages"][1].content


async def test_failed_tool_build_is_reported_to_worker(db: DB, workspace):
    planner = FakeProvider(
        [
            plan_json(
                {
                    "id": "s1",
                    "description": "count",
                    "missing_tools": [{"name": "word_count", "description": "count words"}],
                }
            )
        ]
    )
    smith = FakeProvider(["nope"] * 3)
    worker = FakeProvider(["did it by hand"])
    run = await orch(db, planner=planner, toolsmith=smith, worker=worker).start(db.create_conversation(), "t")
    assert run["status"] == "completed"
    assert "Could not build the tool" in worker.requests[0]["messages"][1].content


async def test_parallel_waves_and_upstream_results(db: DB, workspace, monkeypatch):
    monkeypatch.setattr("sea.config.settings.max_parallel_steps", 2)
    planner = FakeProvider(
        [
            plan_json(
                {"id": "s1", "description": "get A"},
                {"id": "s2", "description": "get B"},
                {"id": "s3", "description": "combine", "depends_on": ["s1", "s2"]},
            )
        ]
    )

    # A worker that records overlap: both wave-1 steps must be in flight at the same time.
    class SlowWorker(FakeProvider):
        active = 0
        max_active = 0

        async def complete(self, messages, tools=None, json_mode=False):
            SlowWorker.active += 1
            SlowWorker.max_active = max(SlowWorker.max_active, SlowWorker.active)
            await asyncio.sleep(0.05)
            SlowWorker.active -= 1
            step = next(m.content for m in messages if m.role == "user")
            self.requests.append({"messages": list(messages), "tools": tools or [], "json_mode": json_mode})
            if "(s1)" in step:
                return Completion("A=1")
            if "(s2)" in step:
                return Completion("B=2")
            if "(s3)" in step:
                assert "[s1] A=1" in step and "[s2] B=2" in step
                return Completion("A+B=3")
            return Completion("final: 3")  # the answer synthesis call

    worker = SlowWorker([])
    run = await orch(db, planner=planner, worker=worker).start(db.create_conversation(), "t")
    assert run["status"] == "completed" and run["answer"] == "final: 3"
    assert SlowWorker.max_active == 2
    events = db.get_events(run["id"])
    starts = [e["payload"]["step_id"] for e in events if e["type"] == "step_started"]
    done = [e["payload"]["step_id"] for e in events if e["type"] == "step_completed"]
    assert set(starts[:2]) == {"s1", "s2"} and done[-1] == "s3"


async def test_failed_step_blocks_dependents_but_not_siblings(db: DB, workspace):
    planner = FakeProvider(
        [
            plan_json(
                {"id": "s1", "description": "will fail"},
                {"id": "s2", "description": "independent"},
                {"id": "s3", "description": "needs s1", "depends_on": ["s1"]},
            )
        ]
    )

    class Worker(FakeProvider):
        async def complete(self, messages, tools=None, json_mode=False):
            step = next(m.content for m in messages if m.role == "user")
            if "(s1)" in step:
                raise RuntimeError("provider exploded")
            if "(s2)" in step:
                return Completion("s2 ok")
            return Completion("summary")

    run = await orch(db, planner=planner, worker=Worker([])).start(db.create_conversation(), "t")
    assert run["status"] == "completed"
    steps = run["state"]["steps"]
    assert (steps["s1"]["status"], steps["s2"]["status"], steps["s3"]["status"]) == (
        "failed",
        "done",
        "blocked",
    )
    types = [e["type"] for e in db.get_events(run["id"])]
    assert "step_failed" in types and "step_blocked" in types


async def test_interrupt_inside_wave_resumes_only_that_step(db: DB, workspace, monkeypatch):
    monkeypatch.setattr("sea.config.settings.auto_approve", "safe")  # tool calls still need approval
    planner = FakeProvider(
        [plan_json({"id": "s1", "description": "write"}, {"id": "s2", "description": "read"})]
    )

    class Worker(FakeProvider):
        async def complete(self, messages, tools=None, json_mode=False):
            self.requests.append({"messages": list(messages)})
            step = next(m.content for m in messages if m.role == "user")
            if "(s1)" in step and messages[-1].role != "tool":
                return Completion("", [ToolCall("w1", "write_file", {"path": "x.txt", "content": "hi"})])
            if "(s1)" in step:
                return Completion("wrote")
            if "(s2)" in step:
                return Completion("read nothing")
            return Completion("all done")

    worker = Worker([])
    o = orch(db, planner=planner, worker=worker)
    run = await o.start(db.create_conversation(), "t")
    assert run["status"] == "awaiting_approval"
    assert run["state"]["steps"]["s2"]["status"] == "done"  # the sibling finished before the pause
    pending = db.pending_approvals(run["id"])
    assert len(pending) == 1

    run = await o.resume(run["id"], pending[0]["id"], {"approved": True})
    assert run["status"] == "completed" and run["answer"] == "all done"
    # s2 was not re-run: only s1's follow-up and the final answer were requested after resume.
    after = worker.requests[2:]
    assert len(after) == 2


async def test_duplicate_interrupt_reuses_pending_approval(db: DB, workspace, monkeypatch):
    monkeypatch.setattr("sea.config.settings.auto_approve", "safe")
    planner = FakeProvider([plan_json({"id": "s1", "description": "a"}, {"id": "s2", "description": "b"})])

    class Worker(FakeProvider):
        async def complete(self, messages, tools=None, json_mode=False):
            step = next(m.content for m in messages if m.role == "user")
            sid = "s1" if "(s1)" in step else "s2"
            if messages[-1].role != "tool" and sid in step:
                return Completion(
                    "", [ToolCall(f"w_{sid}", "write_file", {"path": f"{sid}.txt", "content": "x"})]
                )
            return Completion("ok")

    o = orch(db, planner=planner, worker=Worker([]))
    run = await o.start(db.create_conversation(), "t")
    assert run["status"] == "awaiting_approval"
    assert len(db.pending_approvals(run["id"])) == 2  # both parallel steps asked
    first = db.pending_approvals(run["id"])[0]
    run = await o.resume(run["id"], first["id"], {"approved": True})
    assert run["status"] == "awaiting_approval"
    assert len(db.pending_approvals(run["id"])) == 1  # the other one was reused, not duplicated
    second = db.pending_approvals(run["id"])[0]
    run = await o.resume(run["id"], second["id"], {"approved": True})
    assert run["status"] == "completed"
    assert (workspace / run["conversation_id"] / "s1.txt").exists()
    assert (workspace / run["conversation_id"] / "s2.txt").exists()


def test_plan_model_dump_round_trip():
    plan = parse_plan(plan_json({"id": "s1", "description": "a"}))
    assert Plan.model_validate(plan.model_dump()) == plan
