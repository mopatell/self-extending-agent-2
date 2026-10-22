"""The watch scheduler: a standalone loop that polls each active watch when it is due.

Runs as its own process (`sea watch daemon`), so a misbehaving watch can't take down the API.
State lives in SQLite, so stopping and restarting the daemon resumes cleanly — a watch's next
poll is derived from its stored last_checked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sea.db import DB
from sea.sandbox.runner import Sandbox
from sea.watches.poll import PollResult, poll_watch

BACKOFF_CAP = 16  # a repeatedly failing watch waits at most this many × its interval


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def effective_interval(watch: dict[str, Any]) -> int:
    """The normal interval, backed off exponentially while the watch is erroring."""
    errors = watch["consecutive_errors"] or 0
    return watch["interval_seconds"] * min(2**errors, BACKOFF_CAP)


def is_due(watch: dict[str, Any], now: datetime) -> bool:
    last = _parse_ts(watch["last_checked"])
    if last is None:
        return True
    return (now - last).total_seconds() >= effective_interval(watch)


class WatchDaemon:
    def __init__(self, db: DB, sandbox: Sandbox, *, tick: int = 5, on_poll: Callable | None = None):
        self.db = db
        self.sandbox = sandbox
        self.tick = tick
        self.on_poll = on_poll  # callback(watch, PollResult) for logging/UI
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self, *, max_cycles: int | None = None) -> None:
        """Poll due watches forever (or for max_cycles, used by tests)."""
        cycles = 0
        while not self._stop.is_set():
            await self.cycle()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick)
            except TimeoutError:
                pass

    async def cycle(self) -> list[PollResult]:
        now = datetime.now(UTC)
        results: list[PollResult] = []
        for watch in self.db.list_watches("active"):
            if not is_due(watch, now):
                continue
            result = await self._poll(watch)
            results.append(result)
        return results

    async def _poll(self, watch: dict[str, Any]) -> PollResult:
        try:
            result = await poll_watch(self.db, self.sandbox, watch)
        except Exception as e:  # noqa: BLE001 — one bad watch must never kill the loop
            result = PollResult("error", detail=f"{type(e).__name__}: {e}")
        if self.on_poll:
            self.on_poll(watch, result)
        return result
