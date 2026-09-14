import pytest

from sea.db import DB
from sea.events import Emitter, Event
from sea.llm import (
    AnthropicProvider,
    Completion,
    FakeProvider,
    Message,
    OpenAICompatProvider,
    ToolCall,
    ToolSpec,
    get_provider,
    tool_call,
)

# ----------------------------------------------------------------------------- llm


def test_message_round_trip():
    m = Message("assistant", "hi", tool_calls=[ToolCall("c1", "add", {"a": 1})], raw=[{"type": "text"}])
    assert Message.from_dict(m.to_dict()) == m


def test_openai_message_conversion():
    msgs = [
        Message.system("sys"),
        Message.user("q"),
        Message("assistant", "", tool_calls=[ToolCall("c1", "add", {"a": 1, "b": 2})]),
        Message.tool("c1", "3"),
    ]
    out = [OpenAICompatProvider._to_openai(m) for m in msgs]
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[2]["tool_calls"][0]["function"] == {"name": "add", "arguments": '{"a": 1, "b": 2}'}
    assert out[2]["content"] is None
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "3"}


def test_anthropic_message_conversion_merges_tool_results_and_replays_raw():
    raw = [
        {"type": "thinking", "thinking": "..."},
        {"type": "tool_use", "id": "c1", "name": "a", "input": {}},
    ]
    msgs = [
        Message.system("sys"),
        Message.user("q"),
        Message("assistant", "", tool_calls=[ToolCall("c1", "a", {}), ToolCall("c2", "b", {})], raw=raw),
        Message.tool("c1", "r1"),
        Message.tool("c2", "r2"),
    ]
    out = AnthropicProvider._to_anthropic(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[1]["content"] is raw
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["c1", "c2"]


async def test_fake_provider_scripts_and_records():
    fake = FakeProvider(["hello", tool_call("add", {"a": 1})])
    c1 = await fake.complete([Message.user("x")])
    c2 = await fake.complete([Message.user("y")], tools=[ToolSpec("add", "", {})])
    assert c1.text == "hello" and not c1.tool_calls
    assert c2.tool_calls[0].name == "add"
    assert len(fake.requests) == 2 and fake.requests[1]["tools"][0].name == "add"
    with pytest.raises(RuntimeError):
        await fake.complete([])


def test_get_provider_rejects_bad_strings():
    with pytest.raises(ValueError):
        get_provider("no-colon")
    with pytest.raises(ValueError):
        get_provider("nope:model")


def test_get_provider_requires_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        OpenAICompatProvider("groq", "m")


# ----------------------------------------------------------------------------- db


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "t.db"
    DB(path).close()
    d = DB(path)
    assert d._one("SELECT COUNT(*) AS n FROM schema_migrations")["n"] == 1
    d.close()


def test_conversation_run_events_round_trip(db: DB):
    cid = db.create_conversation("t")
    db.add_message(cid, "user", "hi")
    rid = db.create_run(cid, "do it")
    db.update_run(rid, status="running", plan={"steps": []}, state={"wave": 0})
    db.add_usage(rid, 10, 5)
    db.add_usage(rid, 1, 1)

    run = db.get_run(rid)
    assert run["status"] == "running" and run["plan"] == {"steps": []} and run["state"] == {"wave": 0}
    assert (run["tokens_in"], run["tokens_out"]) == (11, 6)
    assert db.get_messages(cid)[0]["content"] == "hi"

    assert db.add_event(rid, "run_started", {"task": "do it"}) == 1
    assert db.add_event(rid, "step_started", {"step_id": "s1"}) == 2
    events = db.get_events(rid, after_seq=1)
    assert len(events) == 1 and events[0]["payload"] == {"step_id": "s1"}


def test_approvals(db: DB):
    cid = db.create_conversation()
    rid = db.create_run(cid, "t")
    aid = db.create_approval(rid, "plan", {"steps": [1]})
    assert db.pending_approvals(rid)[0]["payload"] == {"steps": [1]}
    db.resolve_approval(aid, "approved", {"edits": None})
    assert db.pending_approvals(rid) == []
    assert db.get_approval(aid)["decision"] == {"edits": None}


def test_tool_versions(db: DB):
    base = dict(description="d", input_schema={"type": "object"}, source="def f(): pass", test_code="")
    db.insert_tool(name="f", version=1, **base)
    db.insert_tool(name="f", version=2, **base)
    db.insert_tool(name="g", version=1, **base)
    assert db.latest_tool("f")["version"] == 2
    assert [t["name"] for t in db.active_tools()] == ["f", "g"]
    db.record_tool_call("f", 2, failed=True)
    assert db.latest_tool("f")["failures"] == 1
    db.set_tool_status("f", "deprecated")
    assert [t["name"] for t in db.active_tools()] == ["g"]


# ----------------------------------------------------------------------------- events


def test_emitter_persists_and_fans_out(db: DB):
    rid = db.create_run(db.create_conversation(), "t")
    seen: list[Event] = []
    em = Emitter(db, rid, [seen.append])
    em.emit("run_started", task="t")
    em.child(step_id="s1").emit("step_started")
    assert [e.type for e in seen] == ["run_started", "step_started"]
    assert seen[1].payload == {"step_id": "s1"} and seen[1].seq == 2
    assert len(db.get_events(rid)) == 2
    with pytest.raises(ValueError):
        em.emit("not_a_type")


def test_completion_as_message():
    c = Completion("t", [ToolCall("c", "n", {})], raw=[1])
    m = c.as_message()
    assert m.role == "assistant" and m.tool_calls == c.tool_calls and m.raw == [1]
