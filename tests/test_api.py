import asyncio
import json

import httpx
import pytest

from sea.agents.orchestrator import Orchestrator
from sea.api import create_app
from sea.db import DB
from sea.llm import FakeProvider, tool_call
from sea.sandbox.runner import LocalSandbox
from tests.conftest import plan_json


@pytest.fixture
async def client(db: DB, workspace, monkeypatch):
    monkeypatch.setattr("sea.config.settings.auto_approve", "none")
    providers: dict[str, FakeProvider] = {}
    orch = Orchestrator(db, LocalSandbox(), providers=providers)
    app = create_app(db=db, orchestrator=orch)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            c.providers = providers  # type: ignore[attr-defined]
            yield c


async def wait_status(client, run_id: str, wanted: set[str], timeout: float = 3.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = (await client.get(f"/runs/{run_id}")).json()
        if run["status"] in wanted:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run never reached {wanted}: {run['status']}")


async def read_sse(client, run_id: str, after: int = 0) -> list[tuple[str, dict]]:
    events = []
    async with client.stream("GET", f"/runs/{run_id}/events", params={"after": after}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        name = None
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[6:])))
    return events


async def test_run_pause_resume_over_http(client, workspace):
    client.providers["planner"] = FakeProvider([plan_json({"id": "s1", "description": "write it"})])
    client.providers["worker"] = FakeProvider(
        [tool_call("write_file", {"path": "a.txt", "content": "hi"}), "done"]
    )

    cid = (await client.post("/conversations", params={"title": "t"})).json()["id"]
    r = await client.post(f"/conversations/{cid}/runs", json={"task": "write a.txt"})
    assert r.status_code == 202
    run_id = r.json()["run_id"]

    run = await wait_status(client, run_id, {"awaiting_plan_approval"})
    assert run["pending_approvals"][0]["kind"] == "plan"

    # SSE replays what happened so far and ends because the run is paused.
    events = await read_sse(client, run_id)
    assert [e[0] for e in events][:3] == ["run_started", "llm_called", "plan_proposed"]
    assert events[-1] == ("end", {"status": "awaiting_plan_approval"})
    last_seq = len(events) - 1

    aid = run["pending_approvals"][0]["id"]
    r = await client.post(f"/runs/{run_id}/resume", json={"approval_id": aid, "decision": {"approved": True}})
    assert r.status_code == 202
    run = await wait_status(client, run_id, {"awaiting_approval"})
    assert run["pending_approvals"][0]["payload"]["name"] == "write_file"

    # Resuming the same approval twice is rejected.
    r = await client.post(f"/runs/{run_id}/resume", json={"approval_id": aid, "decision": {"approved": True}})
    assert r.status_code == 409

    aid2 = run["pending_approvals"][0]["id"]
    await client.post(f"/runs/{run_id}/resume", json={"approval_id": aid2, "decision": {"approved": True}})
    run = await wait_status(client, run_id, {"completed"})
    assert run["answer"] == "done" and (workspace / cid / "a.txt").read_text() == "hi"

    # Tail from where we left off: only the new events, then end.
    tail = await read_sse(client, run_id, after=last_seq)
    names = [e[0] for e in tail]
    assert (
        names[0] == "run_resumed" and names[-2] == "run_completed" and tail[-1][1] == {"status": "completed"}
    )

    msgs = (await client.get(f"/conversations/{cid}/messages")).json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]


async def test_sse_tails_a_live_run(client, monkeypatch):
    class Slow(FakeProvider):
        async def complete(self, messages, tools=None, json_mode=False):
            await asyncio.sleep(0.4)
            return await super().complete(messages, tools, json_mode)

    client.providers["planner"] = Slow([plan_json({"id": "s1", "description": "x"})])
    client.providers["worker"] = Slow(["answer"])
    monkeypatch.setattr("sea.config.settings.auto_approve", "all")
    cid = (await client.post("/conversations")).json()["id"]
    run_id = (await client.post(f"/conversations/{cid}/runs", json={"task": "t"})).json()["run_id"]
    events = await read_sse(client, run_id)  # connects before the run finishes and follows it to the end
    names = [e[0] for e in events]
    assert names[0] == "run_started" and names[-2] == "run_completed" and names[-1] == "end"


async def test_404s_and_tools(client, db):
    assert (await client.get("/runs/nope")).status_code == 404
    assert (await client.get("/conversations/nope/messages")).status_code == 404
    assert (await client.post("/conversations/nope/runs", json={"task": "t"})).status_code == 404
    assert (await client.get("/tools/nope")).status_code == 404
    db.insert_tool(
        name="f",
        version=1,
        description="d",
        input_schema={"type": "object"},
        source="def f(): 1",
        test_code="assert 1",
    )
    tools = (await client.get("/tools")).json()
    assert tools[0]["name"] == "f" and "source" not in tools[0]
    assert (await client.get("/tools/f")).json()["source"] == "def f(): 1"


# ----------------------------------------------------------------------------- desktop endpoints


async def test_health_and_cors(client):
    r = await client.get("/health", headers={"Origin": "http://localhost:1420"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["sandbox"] == "LocalSandbox"
    assert r.headers["access-control-allow-origin"] == "http://localhost:1420"


async def test_pages_list_rename_delete(client, db):
    client.providers["planner"] = FakeProvider([plan_json({"id": "s1", "description": "x"})])
    client.providers["worker"] = FakeProvider(["done"])
    cid = (await client.post("/conversations", params={"title": "first"})).json()["id"]
    empty = (await client.post("/conversations", params={"title": "empty"})).json()["id"]
    rid = (await client.post(f"/conversations/{cid}/runs", json={"task": "hello"})).json()["run_id"]
    await wait_status(client, rid, {"awaiting_plan_approval"})

    pages = (await client.get("/conversations")).json()
    by_id = {p["id"]: p for p in pages}
    assert by_id[cid]["last_task"] == "hello" and by_id[cid]["run_count"] == 1
    assert by_id[cid]["last_status"] == "awaiting_plan_approval"
    assert by_id[empty]["last_task"] is None and by_id[empty]["run_count"] == 0
    assert pages[0]["id"] == cid  # most recently active first

    runs = (await client.get(f"/conversations/{cid}/runs")).json()
    assert [r["id"] for r in runs] == [rid]

    assert (await client.patch(f"/conversations/{cid}", json={"title": "renamed"})).json()[
        "title"
    ] == "renamed"
    pending = (await client.get("/approvals")).json()
    assert len(pending) == 1 and pending[0]["run_id"] == rid and pending[0]["task"] == "hello"

    assert (await client.post(f"/runs/{rid}/cancel")).json()["status"] == "cancelled"
    assert (await client.post(f"/runs/{rid}/cancel")).status_code == 409
    assert (await client.get("/approvals")).json() == []

    assert (await client.delete(f"/conversations/{cid}")).status_code == 204
    assert (await client.get(f"/conversations/{cid}/runs")).status_code == 404
    assert db.get_run(rid) is None and db.get_events(rid) == []
    assert (await client.delete(f"/conversations/{cid}")).status_code == 404


async def test_tool_deprecate_restore(client, db):
    db.insert_tool(
        name="f",
        version=1,
        description="d",
        input_schema={"type": "object"},
        source="def f(): 1",
        test_code="assert 1",
    )
    assert (await client.post("/tools/f/deprecate")).json()["status"] == "deprecated"
    assert (await client.get("/tools")).json() == []
    assert (await client.post("/tools/f/restore")).json()["status"] == "active"
    assert len((await client.get("/tools")).json()) == 1
    assert (await client.post("/tools/nope/deprecate")).status_code == 404


async def test_settings_round_trip(client, tmp_path, monkeypatch):
    from sea import config

    env = tmp_path / ".env"
    env.write_text("# keep me\nGROQ_API_KEY=sk-secret-1234\nUNRELATED=1\nWORKER_MODEL=groq:old\n")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setenv("GROQ_API_KEY", "sk-secret-1234")
    monkeypatch.setenv("WORKER_MODEL", "groq:old")

    got = {s["key"]: s for s in (await client.get("/settings")).json()}
    assert got["GROQ_API_KEY"]["value"] == "••••1234" and got["GROQ_API_KEY"]["kind"] == "secret"
    assert got["WORKER_MODEL"]["value"] == "groq:old"

    # Sending the masked key back keeps the real secret; other fields update; unknown keys ignored.
    r = await client.put(
        "/settings",
        json={"GROQ_API_KEY": "••••1234", "WORKER_MODEL": "groq:new", "MAX_PARALLEL_STEPS": "3", "EVIL": "x"},
    )
    assert r.status_code == 200
    text = env.read_text()
    assert "GROQ_API_KEY=sk-secret-1234" in text and "WORKER_MODEL=groq:new" in text
    assert (
        "MAX_PARALLEL_STEPS=3" in text
        and "# keep me" in text
        and "UNRELATED=1" in text
        and "EVIL" not in text
    )
    assert config.settings.worker_model == "groq:new" and config.settings.max_parallel_steps == 3

    assert (await client.put("/settings", json={"MAX_PARALLEL_STEPS": "many"})).status_code == 422
    assert (await client.put("/settings", json={"AUTO_APPROVE": "maybe"})).status_code == 422

    r = await client.put("/settings", json={"GROQ_API_KEY": "sk-brand-new-9999"})
    assert "GROQ_API_KEY=sk-brand-new-9999" in env.read_text()
    assert {s["key"]: s for s in r.json()}["GROQ_API_KEY"]["value"] == "••••9999"
