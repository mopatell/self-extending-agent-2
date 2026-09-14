"""HTTP API. Runs execute as background tasks in the server; clients follow them over SSE.

POST /conversations                     -> {id}
GET  /conversations/{id}/messages
POST /conversations/{id}/runs {task}    -> 202 {run_id}
GET  /runs                              -> recent runs
GET  /runs/{id}                         -> run + pending approvals
GET  /runs/{id}/events?after=0          -> SSE: replays stored events, then tails until the run ends
POST /runs/{id}/resume {approval_id, decision} -> 202
GET  /tools, GET /tools/{name}
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sea.agents.orchestrator import Orchestrator
from sea.db import DB
from sea.sandbox.runner import get_sandbox

TERMINAL = {"completed", "failed", "cancelled"}
WAITING = {"awaiting_plan_approval", "awaiting_approval", "awaiting_input"}


class TaskIn(BaseModel):
    task: str


class ResumeIn(BaseModel):
    approval_id: str
    decision: dict[str, Any]


def create_app(db: DB | None = None, orchestrator: Orchestrator | None = None) -> FastAPI:
    tasks: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = db or DB()
        app.state.orchestrator = orchestrator or Orchestrator(app.state.db, get_sandbox())
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="sea", lifespan=lifespan)

    def spawn(coro: Any) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def get_run_or_404(run_id: str) -> dict[str, Any]:
        run = app.state.db.get_run(run_id)
        if not run:
            raise HTTPException(404, "no such run")
        return run

    # ------------------------------------------------------------ conversations

    @app.post("/conversations", status_code=201)
    def create_conversation(title: str = "") -> dict[str, str]:
        return {"id": app.state.db.create_conversation(title)}

    @app.get("/conversations")
    def list_conversations() -> list[dict[str, Any]]:
        return app.state.db.list_conversations()

    @app.get("/conversations/{cid}/messages")
    def messages(cid: str) -> list[dict[str, Any]]:
        if not app.state.db.get_conversation(cid):
            raise HTTPException(404, "no such conversation")
        return app.state.db.get_messages(cid)

    @app.post("/conversations/{cid}/runs", status_code=202)
    async def start_run(cid: str, body: TaskIn) -> dict[str, str]:
        db: DB = app.state.db
        if not db.get_conversation(cid):
            raise HTTPException(404, "no such conversation")
        # Create the row here so the client gets an id back before any LLM call happens.
        run_id = db.create_run(cid, body.task)
        spawn(app.state.orchestrator.start_existing(run_id))
        return {"run_id": run_id, "status": "pending"}

    # ------------------------------------------------------------ runs

    @app.get("/runs")
    def list_runs(limit: int = 20) -> list[dict[str, Any]]:
        return app.state.db.list_runs(limit=limit)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        run = get_run_or_404(run_id)
        run["pending_approvals"] = app.state.db.pending_approvals(run_id)
        return run

    @app.post("/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str, body: ResumeIn) -> dict[str, str]:
        run = get_run_or_404(run_id)
        approval = app.state.db.get_approval(body.approval_id)
        if not approval or approval["run_id"] != run_id:
            raise HTTPException(404, "no such approval for this run")
        if approval["status"] != "pending":
            raise HTTPException(409, "approval already resolved")
        if run["status"] not in WAITING:
            raise HTTPException(409, f"run is {run['status']}, not waiting")
        spawn(app.state.orchestrator.resume(run_id, body.approval_id, body.decision))
        return {"run_id": run_id, "status": "resuming"}

    @app.get("/runs/{run_id}/events")
    async def events(run_id: str, after: int = 0) -> StreamingResponse:
        get_run_or_404(run_id)
        return StreamingResponse(event_stream(app.state.db, run_id, after), media_type="text/event-stream")

    # ------------------------------------------------------------ tools

    @app.get("/tools")
    def list_tools() -> list[dict[str, Any]]:
        return [
            {k: v for k, v in t.items() if k not in ("source", "test_code")}
            for t in app.state.db.active_tools()
        ]

    @app.get("/tools/{name}")
    def get_tool(name: str) -> dict[str, Any]:
        tool = app.state.db.latest_tool(name)
        if not tool:
            raise HTTPException(404, "no such tool")
        return tool

    return app


async def event_stream(db: DB, run_id: str, after: int, poll: float = 0.3) -> AsyncIterator[str]:
    """Replay events after `after`, then keep polling until the run is finished or paused."""
    seq = after
    while True:
        for e in db.get_events(run_id, after_seq=seq):
            seq = e["seq"]
            yield f"id: {seq}\nevent: {e['type']}\ndata: {json.dumps(e['payload'], default=str)}\n\n"
        status = (db.get_run(run_id) or {}).get("status")
        if status in TERMINAL or status in WAITING:
            # One last sweep so nothing emitted between the query and the status check is lost.
            for e in db.get_events(run_id, after_seq=seq):
                seq = e["seq"]
                yield f"id: {seq}\nevent: {e['type']}\ndata: {json.dumps(e['payload'], default=str)}\n\n"
            yield f"event: end\ndata: {json.dumps({'status': status})}\n\n"
            return
        await asyncio.sleep(poll)


app = create_app()
