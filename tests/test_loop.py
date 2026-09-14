import pytest

from sea.agents.orchestrator import Orchestrator
from sea.db import DB
from sea.events import Emitter, Event
from sea.interrupts import AutoHuman
from sea.llm import Completion, FakeProvider, Message, ToolCall, ToolSpec, tool_call
from sea.loop import run_loop
from sea.sandbox.runner import LocalSandbox
from sea.tools.base import Tool, ToolContext
from sea.tools.builtin import builtin_tools
from tests.conftest import single_step_planner


async def add(ctx: ToolContext, a: int, b: int) -> str:
    return str(a + b)


async def boom(ctx: ToolContext) -> str:
    raise ValueError("kaboom")


TOOLS = {
    "add": Tool(ToolSpec("add", "adds", {"type": "object"}), add),
    "boom": Tool(ToolSpec("boom", "fails", {"type": "object"}), boom),
    "danger": Tool(ToolSpec("danger", "side effect", {"type": "object"}), add, risk="approval"),
}


@pytest.fixture
def harness(db: DB, tmp_path):
    rid = db.create_run(db.create_conversation(), "t")
    seen: list[Event] = []
    emitter = Emitter(db, rid, [seen.append])
    human = AutoHuman()
    ctx = ToolContext(workspace=tmp_path, sandbox=LocalSandbox(), human=human, step_id="s1")
    return db, rid, emitter, human, ctx, seen


async def _run(harness, script, messages=None, max_steps=None, tools=TOOLS):
    db, rid, emitter, human, ctx, seen = harness
    provider = FakeProvider(script)
    messages = messages or [Message.system("s"), Message.user("u")]
    result = await run_loop(
        provider=provider, messages=messages, tools=tools, ctx=ctx, emitter=emitter,
        human=human, db=db, run_id=rid, max_steps=max_steps,
    )  # fmt: skip
    return result, messages, provider, seen


async def test_tool_loop_until_answer(harness):
    result, messages, provider, seen = await _run(harness, [tool_call("add", {"a": 1, "b": 2}), "3 it is"])
    assert result.text == "3 it is" and result.steps == 2
    assert [m.role for m in messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert messages[3].content == "3" and messages[3].tool_call_id == messages[2].tool_calls[0].id
    assert [e.type for e in seen] == ["llm_called", "tool_called", "tool_result", "llm_called"]
    assert provider.requests[0]["tools"][0].name == "add"


async def test_tool_errors_are_returned_not_raised(harness):
    result, messages, _, seen = await _run(harness, [tool_call("boom"), tool_call("nope"), "done"])
    assert "kaboom" in messages[3].content
    assert "no tool named 'nope'" in messages[5].content
    assert [e.payload["error"] for e in seen if e.type == "tool_result"] == [True, True]


async def test_bad_arguments_reported(harness):
    _, messages, _, _ = await _run(harness, [tool_call("add", {"a": 1}), "ok"])
    assert "bad arguments" in messages[3].content


async def test_max_steps_forces_answer(harness):
    result, messages, provider, _ = await _run(
        harness, [tool_call("add", {"a": 1, "b": 1})] * 2 + ["final"], max_steps=2
    )
    assert result.stopped_early and result.text == "final"
    assert "run out of steps" in messages[-2].content
    assert provider.requests[-1]["tools"] == []  # forced answer, no tools offered


async def test_parallel_calls_all_answered(harness):
    c = Completion("", [ToolCall("x1", "add", {"a": 1, "b": 1}), ToolCall("x2", "add", {"a": 2, "b": 2})])
    _, messages, _, _ = await _run(harness, [c, "ok"])
    assert [(m.tool_call_id, m.content) for m in messages if m.role == "tool"] == [("x1", "2"), ("x2", "4")]


async def test_resume_finishes_unanswered_calls(harness):
    # Simulate a checkpoint taken after the assistant asked for two tools but only one ran.
    messages = [
        Message.system("s"),
        Message.user("u"),
        Message(
            "assistant",
            "",
            tool_calls=[ToolCall("x1", "add", {"a": 1, "b": 1}), ToolCall("x2", "add", {"a": 5, "b": 5})],
        ),
        Message.tool("x1", "2"),
    ]
    _, messages, provider, _ = await _run(harness, ["done"], messages=messages)
    assert messages[4].tool_call_id == "x2" and messages[4].content == "10"
    assert len(provider.requests) == 1


async def test_policy_denies_dangerous_shell(harness):
    tools = builtin_tools()
    _, messages, _, _ = await _run(
        harness, [tool_call("run_shell", {"command": "sudo rm -rf /"}), "ok"], tools=tools
    )
    assert messages[3].content.startswith("Refused by policy")


async def test_approval_tool_asks_human(harness):
    db, rid, emitter, human, ctx, seen = harness
    _, messages, _, _ = await _run(harness, [tool_call("danger", {"a": 1, "b": 1}), "ok"])
    assert human.decisions[0][0] == "tool_call" and messages[3].content == "2"
    assert any(e.type == "approval_resolved" and e.payload["approved"] for e in seen)


async def test_declined_tool_tells_model(harness):
    db, rid, emitter, human, ctx, seen = harness

    class NoHuman(AutoHuman):
        async def decide(self, kind, key, payload):
            return {"approved": False, "reason": "not today"}

    ctx.human = NoHuman()
    db, rid, emitter, _, _, seen = harness
    provider = FakeProvider([tool_call("danger", {"a": 1, "b": 1}), "ok"])
    messages = [Message.user("u")]
    await run_loop(
        provider=provider, messages=messages, tools=TOOLS, ctx=ctx, emitter=emitter,
        human=ctx.human, db=db, run_id=rid,
    )  # fmt: skip
    assert "declined" in messages[2].content and "not today" in messages[2].content


# ----------------------------------------------------------------------------- orchestrator


async def test_orchestrator_single_step_run(db: DB, workspace):
    tmp_path = workspace
    worker = FakeProvider([tool_call("write_file", {"path": "a.txt", "content": "hi"}), "wrote it"])
    orch = Orchestrator(db, LocalSandbox(), providers={"worker": worker, "planner": single_step_planner()})
    cid = db.create_conversation()

    run = await orch.start(cid, "write hi to a.txt")
    # write_file needs approval; DetachedHuman pauses the run.
    assert run["status"] == "awaiting_approval"
    pending = db.pending_approvals(run["id"])
    assert len(pending) == 1 and pending[0]["payload"]["name"] == "write_file"
    assert not (tmp_path / cid / "a.txt").exists()

    run = await orch.resume(run["id"], pending[0]["id"], {"approved": True})
    assert run["status"] == "completed" and run["answer"] == "wrote it"
    assert (tmp_path / cid / "a.txt").read_text() == "hi"
    types = [e["type"] for e in db.get_events(run["id"])]
    assert types[:5] == ["run_started", "llm_called", "plan_proposed", "plan_approved", "step_started"]
    assert "run_paused" in types and "run_resumed" in types and types[-1] == "run_completed"
    assert [m["role"] for m in db.get_messages(cid)] == ["user", "assistant"]
    assert len(worker.requests) == 2  # the LLM was not re-asked after resume


async def test_orchestrator_denied_action(db: DB, workspace):
    tmp_path = workspace
    worker = FakeProvider([tool_call("write_file", {"path": "a.txt", "content": "hi"}), "could not write"])
    orch = Orchestrator(db, LocalSandbox(), providers={"worker": worker, "planner": single_step_planner()})
    run = await orch.start(db.create_conversation(), "write")
    aid = db.pending_approvals(run["id"])[0]["id"]
    run = await orch.resume(run["id"], aid, {"approved": False, "reason": "no"})
    assert run["status"] == "completed" and run["answer"] == "could not write"
    assert not list(tmp_path.rglob("a.txt"))
    with pytest.raises(ValueError, match="already resolved"):
        await orch.resume(run["id"], aid, {"approved": True})


async def test_orchestrator_records_failure(db: DB, workspace):
    orch = Orchestrator(
        db, LocalSandbox(), providers={"worker": FakeProvider([]), "planner": single_step_planner()}
    )
    run = await orch.start(db.create_conversation(), "x")
    assert run["status"] == "failed" and "exhausted" in run["error"]


async def test_malformed_tool_call_is_fed_back(harness):
    from sea.llm import MalformedToolCall

    class Flaky(FakeProvider):
        async def complete(self, messages, tools=None, json_mode=False):
            self.requests.append({"messages": list(messages), "tools": tools or [], "json_mode": json_mode})
            if len(self.requests) == 1:
                raise MalformedToolCall("tool_use_failed")
            return Completion("recovered")

    db, rid, emitter, human, ctx, seen = harness
    messages = [Message.user("u")]
    result = await run_loop(
        provider=Flaky([]), messages=messages, tools=TOOLS, ctx=ctx, emitter=emitter,
        human=human, db=db, run_id=rid,
    )  # fmt: skip
    assert result.text == "recovered"
    assert "could not be parsed" in messages[1].content and messages[1].role == "user"
    assert seen[0].payload.get("error")
