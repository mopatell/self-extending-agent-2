"""HTTP API. Runs execute as background tasks in the server; clients follow them over SSE.

GET  /health
POST /conversations                     -> {id}
GET  /conversations                     -> pages with their latest run summary
PATCH /conversations/{id} {title} · DELETE /conversations/{id}
GET  /conversations/{id}/messages · GET /conversations/{id}/runs
POST /conversations/{id}/runs {task}    -> 202 {run_id}
GET  /runs                              -> recent runs
GET  /runs/{id}                         -> run + pending approvals
GET  /runs/{id}/events?after=0          -> SSE: replays stored events, then tails until the run ends
POST /runs/{id}/resume {approval_id, decision} -> 202
POST /runs/{id}/cancel
GET  /approvals                         -> everything waiting on a human
GET  /tools · GET /tools/{name} · POST /tools/{name}/deprecate · POST /tools/{name}/restore
GET  /settings · PUT /settings          -> .env-backed configuration (secrets masked)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sea import config
from sea.agents.orchestrator import Orchestrator
from sea.db import DB
from sea.sandbox.runner import get_sandbox

# The desktop app (Tauri) and the Vite dev server are separate origins.
ORIGINS = ["http://localhost:1420", "http://127.0.0.1:1420", "tauri://localhost", "http://tauri.localhost"]

TERMINAL = {"completed", "failed", "cancelled"}
WAITING = {"awaiting_plan_approval", "awaiting_approval", "awaiting_input"}


class TaskIn(BaseModel):
    task: str


class ResumeIn(BaseModel):
    approval_id: str
    decision: dict[str, Any]


class TitleIn(BaseModel):
    title: str


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
    app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["*"], allow_headers=["*"])

    def spawn(coro: Any) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def get_run_or_404(run_id: str) -> dict[str, Any]:
        run = app.state.db.get_run(run_id)
        if not run:
            raise HTTPException(404, "no such run")
        return run

    def get_conversation_or_404(cid: str) -> dict[str, Any]:
        conv = app.state.db.get_conversation(cid)
        if not conv:
            raise HTTPException(404, "no such conversation")
        return conv

    @app.get("/health")
    def health() -> dict[str, Any]:
        from importlib.metadata import version

        return {
            "ok": True,
            "version": version("sea"),
            "sandbox": type(app.state.orchestrator.sandbox).__name__,
        }

    # ------------------------------------------------------------ conversations

    @app.post("/conversations", status_code=201)
    def create_conversation(title: str = "") -> dict[str, str]:
        return {"id": app.state.db.create_conversation(title)}

    @app.get("/conversations")
    def list_conversations() -> list[dict[str, Any]]:
        return app.state.db.list_conversations()

    @app.patch("/conversations/{cid}")
    def rename_conversation(cid: str, body: TitleIn) -> dict[str, Any]:
        get_conversation_or_404(cid)
        app.state.db.rename_conversation(cid, body.title.strip())
        return app.state.db.get_conversation(cid)

    @app.delete("/conversations/{cid}", status_code=204)
    def delete_conversation(cid: str) -> None:
        get_conversation_or_404(cid)
        app.state.db.delete_conversation(cid)

    @app.get("/conversations/{cid}/runs")
    def conversation_runs(cid: str) -> list[dict[str, Any]]:
        get_conversation_or_404(cid)
        return app.state.db.list_runs(cid, limit=200, oldest_first=True)

    @app.get("/conversations/{cid}/messages")
    def messages(cid: str) -> list[dict[str, Any]]:
        get_conversation_or_404(cid)
        return app.state.db.get_messages(cid)

    @app.post("/conversations/{cid}/runs", status_code=202)
    async def start_run(cid: str, body: TaskIn) -> dict[str, str]:
        db: DB = app.state.db
        get_conversation_or_404(cid)
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

    @app.post("/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict[str, Any]:
        get_run_or_404(run_id)
        try:
            return app.state.orchestrator.cancel(run_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/approvals")
    def approvals() -> list[dict[str, Any]]:
        return app.state.db.all_pending_approvals()

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

    @app.post("/tools/{name}/deprecate")
    @app.post("/tools/{name}/restore")
    def set_tool_status(name: str, request: Request) -> dict[str, Any]:
        if not app.state.db.latest_tool(name):
            raise HTTPException(404, "no such tool")
        status = "deprecated" if request.url.path.endswith("/deprecate") else "active"
        app.state.db.set_tool_status(name, status)
        return app.state.db.latest_tool(name)

    # ------------------------------------------------------------ settings

    @app.get("/settings")
    def get_settings() -> list[dict[str, str]]:
        return config.read_settings()

    @app.put("/settings")
    def put_settings(body: dict[str, str]) -> list[dict[str, str]]:
        try:
            config.write_settings(body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return config.read_settings()

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
