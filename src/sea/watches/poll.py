"""One poll cycle for a watch. Deterministic — no LLM calls.

Fetch the page, run the compiled check tool on it, decide whether to alert (edge-triggered), send
the Telegram message, and persist the new baseline + a watch_event. All failures are caught and
recorded against the watch; a poll never raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sea import notify
from sea.config import settings
from sea.db import DB, _now
from sea.sandbox.runner import Sandbox, call_tool
from sea.watches import fetch
from sea.watches.store import short

# Consecutive failures before a watch is flagged 'error' and (once) reported over Telegram.
ERROR_THRESHOLD = 5

SendFn = Any  # callable(text, *, chat_id=None)


@dataclass
class PollResult:
    status: str  # checked | changed | matched | notified | not_modified | error
    value: str | None = None
    notified: bool = False
    detail: str | None = None


def interpret(data: Any) -> tuple[str, str, bool | None]:
    """A check tool's output -> (summary, canonical value key for hashing, matched|None)."""
    if isinstance(data, dict):
        value = data.get("value", {k: v for k, v in data.items() if k not in ("summary", "matched")})
        summary = data.get("summary") or short(value)
        matched = data.get("matched")
        matched = None if matched is None else bool(matched)
    else:
        value, summary, matched = data, short(data), None
    return summary, json.dumps(value, sort_keys=True, default=str), matched


def hash_value(data: Any) -> str:
    _summary, value_key, _matched = interpret(data)
    return hashlib.sha256(value_key.encode()).hexdigest()


async def run_check(db: DB, sandbox: Sandbox, name: str, path: str, workspace: Any) -> Any:
    """Run a compiled check tool on a saved page file; return its parsed result."""
    tool = db.latest_tool(name)
    if not tool:
        raise RuntimeError(f"check tool {name!r} is missing from the registry")
    result = await call_tool(
        sandbox, tool["source"], name, {"path": path}, workspace, network=tool["risk"] == "approval"
    )
    if not result.ok:
        raise RuntimeError(result.output.strip() or "check tool produced no output")
    try:
        return json.loads(result.output)
    except json.JSONDecodeError:
        return {"value": result.output.strip(), "summary": short(result.output)}


async def poll_watch(db: DB, sandbox: Sandbox, watch: dict[str, Any], *, send: SendFn = None) -> PollResult:
    send = send or notify.send_telegram
    wid = watch["id"]
    workspace = settings.workspace_for(f"watch-{wid}")

    # 1. Fetch.
    try:
        if watch["fetcher"] == "rendered":
            fetched = await fetch.fetch_rendered(watch["url"])
        else:
            fetched = await asyncio.to_thread(fetch.fetch_static, watch["url"], watch["etag"])
    except Exception as e:  # noqa: BLE001
        return _record_error(db, wid, watch, f"fetch failed: {e}", send)

    if fetched.not_modified:
        db.update_watch(wid, last_checked=_now(), consecutive_errors=0, last_error=None)
        db.add_watch_event(wid, "checked", detail="not modified (304)")
        return PollResult("not_modified")

    page_path = f"page.{fetched.ext}"
    (workspace / page_path).write_bytes(fetched.body)

    # 2. Run the compiled check tool.
    try:
        data = await run_check(db, sandbox, watch["extractor_tool"], page_path, workspace)
    except Exception as e:  # noqa: BLE001
        return _record_error(db, wid, watch, f"check failed: {e}", send)

    summary, _value_key, matched = interpret(data)
    new_hash = hash_value(data)
    is_condition = watch["predicate_tool"] is not None
    changed = watch["last_hash"] is not None and new_hash != watch["last_hash"]
    db.add_watch_event(wid, "checked", value=summary)

    # Fetch and extract both worked: the watch is healthy again regardless of the value.
    db.update_watch(
        wid, etag=fetched.etag, last_checked=_now(), consecutive_errors=0,
        last_error=None, status="active",
    )  # fmt: skip

    # 3. Decide whether to alert (edge-triggered).
    fire, reason = False, ""
    if is_condition:
        was_matched = watch["last_notified_value"] == "1"
        fire, reason = bool(matched and not was_matched), "condition met"
    elif changed:
        fire, reason = True, "value changed"
    if changed:
        db.add_watch_event(wid, "changed", value=summary)

    # 4. Notify. The trigger state (edge flag / value hash) only advances once the alert is
    # actually delivered, so a failed send is retried on the next poll (at-least-once).
    if not fire:
        _advance(db, wid, summary, new_hash, is_condition, matched)
        return PollResult("changed" if changed else "checked", value=summary)

    text = _message(watch, summary, reason, watch["last_value"])
    try:
        send(text, chat_id=watch["chat_id"])
    except notify.TelegramError as e:
        db.add_watch_event(wid, "error", detail=notify.redact(f"notify failed: {e}"))
        return PollResult("error", value=summary, detail=str(e))
    _advance(db, wid, summary, new_hash, is_condition, matched)
    db.add_watch_event(wid, "notified", value=summary, detail=reason)
    return PollResult("notified", value=summary, notified=True, detail=reason)


def _advance(db: DB, wid: str, summary: str, new_hash: str, is_condition: bool, matched: bool | None) -> None:
    """Move the baseline forward: the stored value, its hash, and (condition mode) the edge flag."""
    fields: dict[str, Any] = {"last_value": summary, "last_hash": new_hash}
    if is_condition:
        fields["last_notified_value"] = "1" if matched else "0"
    db.update_watch(wid, **fields)


def _message(watch: dict[str, Any], summary: str, reason: str, old_value: str | None) -> str:
    lines = [f"🔔 {watch['name'] or 'watch'}", watch["url"]]
    if reason == "value changed" and old_value:
        lines.append(f"changed\n  was: {short(old_value)}\n  now: {summary}")
    else:
        lines.append(summary)
    return "\n".join(lines)


def _record_error(db: DB, wid: str, watch: dict[str, Any], message: str, send: SendFn) -> PollResult:
    current = db.get_watch(wid) or watch
    count = (current["consecutive_errors"] or 0) + 1
    status = "error" if count >= ERROR_THRESHOLD else "active"
    db.update_watch(
        wid, consecutive_errors=count, last_error=message[:500], last_checked=_now(), status=status
    )
    db.add_watch_event(wid, "error", detail=message[:500])
    if count == ERROR_THRESHOLD:  # tell the user once that the watch has gone quiet-broken
        try:
            send(
                f"⚠️ watch '{watch['name'] or wid}' is failing:\n{short(message, 200)}",
                chat_id=watch["chat_id"],
            )
        except Exception:  # noqa: BLE001 — a failing error-notify must not mask the original error
            pass
    return PollResult("error", detail=message)
