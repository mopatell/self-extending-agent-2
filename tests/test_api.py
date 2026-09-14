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
