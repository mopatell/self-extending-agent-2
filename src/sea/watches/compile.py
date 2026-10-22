"""Compile a natural-language watch request into a stored, ready-to-poll watch.

One planner call decides what to track (and whether there's an alert condition); the ToolSmith
then builds a single `check_<id>(path)` tool, which we verify against the real page to get a
baseline. After this, polling needs no LLM. All of it is recorded against a run so it shows up in
`sea runs` and token usage is accounted for.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sea.config import settings
from sea.db import DB, new_id
from sea.events import Emitter, Listener
from sea.llm import Message, Provider, get_provider
from sea.sandbox.runner import Sandbox
from sea.tools.builtin import builtin_tools
from sea.tools.registry import Registry
from sea.watches import fetch, poll
from sea.watches.store import WATCHES_CONVERSATION

SAMPLE_CHARS = 3000

PLAN_SYSTEM = """You set up a website monitor. Given a request and a sample of the page, reply with
ONE JSON object, no prose:

{
  "name": "short title for the alert (<=40 chars)",
  "value_description": "what to track, e.g. 'the ticket price as a float' or 'in-stock status as text'",
  "value_kind": "number" | "text",
  "needs_condition": true | false,
  "condition_description": "when to alert, e.g. 'the price is below 80' (only if needs_condition)",
  "interval_seconds": 300
}

- needs_condition is true when the request names a trigger (below X, available, sold out, contains Y).
  It is false when the user just wants to know when the value CHANGES.
- Keep value_description concrete and tied to what is actually on the page sample.
"""

PLAN_USER = "Request: {request}\nURL: {url}\n\nPage sample ({ext}):\n{sample}"

CHECK_CAPABILITY = """{name}: Parse the file at `path` — a {ext} page fetched from {url} — and return a
dict describing the current state.

Return a dict with:
- "value": {value_description} ({value_kind})
- "summary": a short human-readable string (<=120 chars) describing the current state
{condition_line}
The function's only parameter is `path` (a string). If the expected content is not present, raise a
ValueError — never return a placeholder or guessed value.

Name the function exactly `{check_name}`.

Here is a trimmed sample of the real page to target its actual structure (do not assume a different
layout). Your tests may use a small snippet like this:

<<<SAMPLE
{sample}
SAMPLE
"""

CONDITION_LINE = '- "matched": true if {condition_description}, else false'


@dataclass
class CompileResult:
    ok: bool
    watch_id: str | None
    message: str
    baseline: str | None = None
    matched: bool | None = None


class WatchPlan:
    def __init__(self, data: dict[str, Any]):
        self.name = str(data.get("name") or "").strip()[:40]
        self.value_description = str(data.get("value_description") or "the value on the page").strip()
        self.value_kind = "number" if str(data.get("value_kind")) == "number" else "text"
        self.needs_condition = bool(data.get("needs_condition"))
        self.condition_description = str(data.get("condition_description") or "").strip()
        try:
            self.interval_seconds = max(30, int(data.get("interval_seconds") or 300))
        except (TypeError, ValueError):
            self.interval_seconds = 300

    @staticmethod
    def parse(text: str) -> WatchPlan:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        return WatchPlan(json.loads(match.group()) if match else {})


def _provider(providers: dict[str, Provider] | None, role: str) -> Provider:
    if providers and role in providers:
        return providers[role]
    return get_provider(getattr(settings, f"{role}_model"))


async def compile_watch(
    db: DB,
    sandbox: Sandbox,
    *,
    request: str,
    url: str,
    every: int | None = None,
    name: str | None = None,
    fetcher: str = "static",
    listeners: list[Listener] | None = None,
    providers: dict[str, Provider] | None = None,
) -> CompileResult:
    """Create a new watch from a plain-language request and build its check tool."""
    wid = db.create_watch(
        request=request, url=url, fetcher=fetcher, interval_seconds=every or 300, name=name or "",
    )  # fmt: skip
    return await _run_compile(
        db, sandbox, wid, request, url, every, name, fetcher, "compile", listeners, providers
    )


async def recompile_watch(
    db: DB,
    sandbox: Sandbox,
    wid: str,
    *,
    listeners: list[Listener] | None = None,
    providers: dict[str, Provider] | None = None,
) -> CompileResult:
    """Rebuild an existing watch's check tool — the manual self-heal for when a page changed and
    the old checker broke. Keeps the watch's id, url, interval and name."""
    w = db.get_watch(wid)
    if not w:
        return CompileResult(False, wid, "no such watch")
    return await _run_compile(
        db, sandbox, wid, w["request"], w["url"], w["interval_seconds"], w["name"], w["fetcher"],
        "recompile", listeners, providers,
    )  # fmt: skip


async def _run_compile(
    db: DB,
    sandbox: Sandbox,
    wid: str,
    request: str,
    url: str,
    every: int | None,
    name: str | None,
    fetcher: str,
    label: str,
    listeners: list[Listener] | None,
    providers: dict[str, Provider] | None,
) -> CompileResult:
    db.update_watch(wid, status="compiling")
    db.create_conversation("watches", cid=WATCHES_CONVERSATION)
    run_id = db.create_run(WATCHES_CONVERSATION, f"{label} watch: {request}")
    emitter = Emitter(db, run_id, listeners or [])
    emitter.emit("run_started", task=f"{label}: {request}")
    workspace = settings.workspace_for(f"watch-{wid}")

    try:
        result = await _compile(
            db, sandbox, wid, request, url, every, name, fetcher, workspace, run_id, emitter, providers
        )
    except Exception as e:  # noqa: BLE001 — surface any compile failure as a result, not a crash
        db.update_watch(wid, status="error", last_error=f"{type(e).__name__}: {e}")
        db.update_run(run_id, status="failed", error=f"{type(e).__name__}: {e}")
        emitter.emit("run_failed", error=f"{type(e).__name__}: {e}")
        return CompileResult(False, wid, f"could not set up the watch: {e}")

    status = "completed" if result.ok else "failed"
    db.update_run(run_id, status=status, answer=result.message)
    emitter.emit("run_completed", answer=result.message)
    return result


async def _compile(
    db: DB,
    sandbox: Sandbox,
    wid: str,
    request: str,
    url: str,
    every: int | None,
    name: str | None,
    fetcher: str,
    workspace: Path,
    run_id: str,
    emitter: Emitter,
    providers: dict[str, Provider] | None,
) -> CompileResult:
    # 1. Fetch the page once, so we build (and later verify) against the real thing.
    if fetcher == "rendered":
        fetched = await fetch.fetch_rendered(url)
    else:
        import asyncio

        fetched = await asyncio.to_thread(fetch.fetch_static, url)
    page_path = f"page.{fetched.ext}"
    (workspace / page_path).write_bytes(fetched.body)
    sample = fetched.body.decode(errors="replace")[:SAMPLE_CHARS]

    # 2. Plan what to track.
    planner = _provider(providers, "planner")
    completion = await planner.complete(
        [Message.system(PLAN_SYSTEM), Message.user(PLAN_USER.format(
            request=request, url=url, ext=fetched.ext, sample=sample))],
        json_mode=True,
    )  # fmt: skip
    db.add_usage(run_id, completion.usage.input_tokens, completion.usage.output_tokens)
    emitter.emit("llm_called", model=planner.model, tokens_in=completion.usage.input_tokens,
                 tokens_out=completion.usage.output_tokens, tool_calls=[], role="planner")  # fmt: skip
    plan = WatchPlan.parse(completion.text)

    # 3. Build the check tool via the ToolSmith.
    from sea.agents import toolsmith

    # A unique name so a recompile builds a fresh tool instead of colliding with the old one.
    check_name = f"check_{wid}"
    if db.latest_tool(check_name):
        check_name = f"check_{wid}_{new_id()[:4]}"
    condition_line = CONDITION_LINE.format(condition_description=plan.condition_description) \
        if plan.needs_condition else ""  # fmt: skip
    capability = CHECK_CAPABILITY.format(
        name=plan.name or "watch check", ext=fetched.ext, url=url,
        value_description=plan.value_description, value_kind=plan.value_kind,
        condition_line=condition_line, check_name=check_name, sample=sample,
    )  # fmt: skip
    registry = Registry(db, sandbox)
    build = await toolsmith.build_tool(
        provider=_provider(providers, "toolsmith"), registry=registry, workspace=workspace,
        emitter=emitter, db=db, run_id=run_id, capability=capability, reserved=set(builtin_tools()),
    )  # fmt: skip
    if not build.ok or not build.name:
        db.update_watch(wid, status="error", last_error=build.message[:500])
        return CompileResult(False, wid, f"couldn't build the page checker: {build.message}")

    # 4. Verify against the real page to get a baseline value.
    try:
        data = await poll.run_check(db, sandbox, build.name, page_path, workspace)
    except Exception as e:  # noqa: BLE001
        db.update_watch(wid, status="error", last_error=str(e)[:500])
        return CompileResult(
            False, wid,
            f"the checker built but failed on the live page: {e}. Try rephrasing, or --rendered.",
        )  # fmt: skip
    summary, _value_key, matched = poll.interpret(data)
    is_condition = matched is not None

    # 5. Store the compiled watch and its baseline.
    db.update_watch(
        wid,
        name=name or plan.name or request[:40],
        interval_seconds=every or plan.interval_seconds,
        extractor_tool=build.name,
        predicate_tool=build.name if is_condition else None,
        last_value=summary,
        last_hash=poll.hash_value(data),
        last_notified_value=("1" if matched else "0") if is_condition else None,
        etag=fetched.etag,
        last_checked=None,
        status="active",
    )
    db.add_watch_event(wid, "checked", value=summary, detail="baseline")
    state = f" · condition currently {'MET' if matched else 'not met'}" if is_condition else ""
    return CompileResult(
        True, wid, f"watch {wid} ready — now: {summary}{state}", baseline=summary, matched=matched
    )
