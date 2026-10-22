"""Watches: the deterministic poll/daemon core, and the compile path with scripted models.

No network and no Docker: fetching is monkeypatched and tools run in the LocalSandbox.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

import sea.watches.fetch as fetchmod
from sea import notify
from sea.db import DB
from sea.llm import FakeProvider
from sea.sandbox.runner import LocalSandbox
from sea.watches import poll
from sea.watches.compile import compile_watch, recompile_watch
from sea.watches.daemon import BACKOFF_CAP, WatchDaemon, effective_interval, is_due
from sea.watches.store import parse_duration

CONDITION_TOOL = """def checker(path):
    with open(path) as f:
        n = int(f.read().strip())
    return {"value": n, "summary": f"price {n}", "matched": n < 80}
"""

CHANGE_TOOL = """def checker(path):
    with open(path) as f:
        text = f.read().strip()
    return {"value": text, "summary": text}
"""

SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}


def _register(db: DB, source: str) -> None:
    db.insert_tool(
        name="checker", version=1, description="test check tool", input_schema=SCHEMA,
        source=source, test_code="assert True", deps=[], risk="safe",
    )  # fmt: skip


def _watch(db: DB, *, condition: bool) -> dict:
    wid = db.create_watch(request="r", url="http://x", interval_seconds=60)
    db.update_watch(
        wid, extractor_tool="checker", predicate_tool="checker" if condition else None,
        last_notified_value="0" if condition else None,
    )  # fmt: skip
    return db.get_watch(wid)


@pytest.fixture
def page(monkeypatch):
    """A mutable fake page; set page['body'] to change what the next fetch returns."""
    box = {"body": b"a", "ct": "text/plain", "raise": None}

    def fake_static(url, etag=None, timeout=30):
        if box["raise"]:
            raise RuntimeError(box["raise"])
        return fetchmod.Fetched(box["body"], box["ct"])

    monkeypatch.setattr(fetchmod, "fetch_static", fake_static)
    return box


@pytest.fixture
def sends():
    out = []

    def send(text, chat_id=None):
        out.append((text, chat_id))

    send.log = out
    return send


# ----------------------------------------------------------------------------- helpers


def test_parse_duration():
    assert parse_duration("30s") == 30
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration("90") == 90
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_interpret_and_hash_are_stable():
    a = {"value": 70, "summary": "price 70", "matched": True}
    b = {"value": 70, "summary": "different text", "matched": False}
    # hash tracks the value only, not summary/matched
    assert poll.hash_value(a) == poll.hash_value(b)
    assert poll.hash_value({"value": 71}) != poll.hash_value(a)
    assert poll.interpret(a) == ("price 70", "70", True)


# ----------------------------------------------------------------------------- change mode


async def test_change_mode_fires_only_on_change(db: DB, workspace, page, sends):
    _register(db, CHANGE_TOOL)
    watch = _watch(db, condition=False)
    sandbox = LocalSandbox()

    page["body"] = b"a"
    r1 = await poll.poll_watch(db, sandbox, db.get_watch(watch["id"]), send=sends)
    assert r1.status == "checked" and not sends.log  # first poll is the baseline, never alerts

    r2 = await poll.poll_watch(db, sandbox, db.get_watch(watch["id"]), send=sends)
    assert r2.status == "checked" and not sends.log  # unchanged

    page["body"] = b"b"
    r3 = await poll.poll_watch(db, sandbox, db.get_watch(watch["id"]), send=sends)
    assert r3.status == "notified" and len(sends.log) == 1
    assert "was: a" in sends.log[0][0] and "now: b" in sends.log[0][0]


# ----------------------------------------------------------------------------- condition mode


async def test_condition_mode_is_edge_triggered(db: DB, workspace, page, sends):
    _register(db, CONDITION_TOOL)
    watch = _watch(db, condition=True)
    sandbox = LocalSandbox()

    async def poll_with(value: bytes) -> poll.PollResult:
        page["body"] = value
        return await poll.poll_watch(db, sandbox, db.get_watch(watch["id"]), send=sends)

    await poll_with(b"90")  # not matched -> no alert
    assert not sends.log
    await poll_with(b"70")  # false -> true: alert
    assert len(sends.log) == 1
    await poll_with(b"60")  # still matched: no repeat alert
    assert len(sends.log) == 1
    await poll_with(b"100")  # back to not matched: no alert, but arms the next one
    assert len(sends.log) == 1
    await poll_with(b"50")  # matched again: alert
    assert len(sends.log) == 2


# ----------------------------------------------------------------------------- errors


async def test_errors_count_and_flag_after_threshold(db: DB, workspace, page, sends):
    _register(db, CHANGE_TOOL)
    watch = _watch(db, condition=False)
    sandbox = LocalSandbox()
    page["raise"] = "boom"

    for _ in range(poll.ERROR_THRESHOLD):
        r = await poll.poll_watch(db, sandbox, db.get_watch(watch["id"]), send=sends)
        assert r.status == "error"

    final = db.get_watch(watch["id"])
    assert final["consecutive_errors"] == poll.ERROR_THRESHOLD
    assert final["status"] == "error"
    assert len(sends.log) == 1  # user is told once, when the threshold is crossed


async def test_not_modified_is_a_quiet_check(db: DB, workspace, monkeypatch, sends):
    _register(db, CHANGE_TOOL)
    watch = _watch(db, condition=False)
    monkeypatch.setattr(
        fetchmod,
        "fetch_static",
        lambda url, etag=None, timeout=30: fetchmod.Fetched(b"", "text/html", etag, True),
    )
    r = await poll.poll_watch(db, LocalSandbox(), db.get_watch(watch["id"]), send=sends)
    assert r.status == "not_modified" and not sends.log


# ----------------------------------------------------------------------------- daemon


def test_is_due():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    base = {"interval_seconds": 60, "consecutive_errors": 0}
    assert is_due({"last_checked": None, **base}, now)
    old = (now - timedelta(seconds=120)).isoformat()
    assert is_due({"last_checked": old, **base}, now)
    recent = (now - timedelta(seconds=10)).isoformat()
    assert not is_due({"last_checked": recent, **base}, now)


async def test_daemon_cycle_polls_due_active_watches(db: DB, workspace, page, monkeypatch):
    _register(db, CHANGE_TOOL)
    active = _watch(db, condition=False)
    paused = _watch(db, condition=False)
    db.update_watch(paused["id"], status="paused")

    polled = []
    daemon = WatchDaemon(db, LocalSandbox(), on_poll=lambda w, r: polled.append(w["id"]))
    results = await daemon.cycle()

    assert active["id"] in polled
    assert paused["id"] not in polled
    assert len(results) == 1


# ----------------------------------------------------------------------------- compile


async def test_compile_builds_and_baselines_a_watch(db: DB, workspace, page):
    page["body"] = b"70"
    plan = json.dumps({
        "name": "Price", "value_description": "the number as int", "value_kind": "number",
        "needs_condition": True, "condition_description": "the number is below 80", "interval_seconds": 120,
    })  # fmt: skip
    draft = json.dumps({
        "name": "check_price",
        "description": "read the number in a file and flag when it is below 80",
        "input_schema": SCHEMA,
        "code": (
            "def check_price(path):\n"
            "    with open(path) as f:\n"
            "        n = int(f.read().strip())\n"
            "    return {'value': n, 'summary': 'price ' + str(n), 'matched': n < 80}\n"
        ),
        "test_code": (
            "import tempfile, os\n"
            "p = os.path.join(tempfile.mkdtemp(), 'x')\n"
            "open(p, 'w').write('70')\n"
            "assert check_price(p)['matched'] is True\n"
            "open(p, 'w').write('90')\n"
            "assert check_price(p)['matched'] is False\n"
        ),
        "deps": [], "network": False,
    })  # fmt: skip
    providers = {"planner": FakeProvider([plan]), "toolsmith": FakeProvider([draft])}

    result = await compile_watch(
        db, LocalSandbox(), request="tell me when the number drops below 80",
        url="http://x", providers=providers,
    )  # fmt: skip

    assert result.ok, result.message
    assert result.matched is True  # 70 < 80 at baseline
    watch = db.get_watch(result.watch_id)
    assert watch["status"] == "active"
    assert watch["extractor_tool"] == "check_price"
    assert watch["predicate_tool"] == "check_price"  # condition mode
    assert watch["interval_seconds"] == 120
    assert watch["last_notified_value"] == "1"


# ----------------------------------------------------------------------------- robustness


class FlakySend:
    """A send callable that raises TelegramError its first `fail_times` calls, then succeeds."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self, text, chat_id=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise notify.TelegramError("telegram down")


async def test_failed_send_is_retried_on_the_next_poll(db: DB, workspace, page):
    _register(db, CHANGE_TOOL)
    watch = _watch(db, condition=False)
    sandbox = LocalSandbox()
    flaky = FlakySend(fail_times=1)

    page["body"] = b"a"
    await poll.poll_watch(db, sandbox, db.get_watch(watch["id"]), send=flaky)  # baseline

    page["body"] = b"b"
    r1 = await poll.poll_watch(db, sandbox, db.get_watch(watch["id"]), send=flaky)
    assert r1.status == "error"  # send failed
    assert db.get_watch(watch["id"])["last_value"] == "a"  # baseline NOT advanced

    r2 = await poll.poll_watch(db, sandbox, db.get_watch(watch["id"]), send=flaky)
    assert r2.status == "notified"  # change still pending, delivered on retry
    assert db.get_watch(watch["id"])["last_value"] == "b"


async def test_condition_failed_send_keeps_the_edge_armed(db: DB, workspace, page):
    _register(db, CONDITION_TOOL)
    watch = _watch(db, condition=True)
    sandbox = LocalSandbox()
    flaky = FlakySend(fail_times=1)

    page["body"] = b"70"  # matched -> fire, but send fails
    r1 = await poll.poll_watch(db, sandbox, db.get_watch(watch["id"]), send=flaky)
    assert r1.status == "error"
    assert db.get_watch(watch["id"])["last_notified_value"] == "0"  # edge not advanced

    r2 = await poll.poll_watch(db, sandbox, db.get_watch(watch["id"]), send=flaky)
    assert r2.status == "notified"  # retried
    assert db.get_watch(watch["id"])["last_notified_value"] == "1"


def test_effective_interval_backs_off_and_caps():
    assert effective_interval({"interval_seconds": 60, "consecutive_errors": 0}) == 60
    assert effective_interval({"interval_seconds": 60, "consecutive_errors": 1}) == 120
    assert effective_interval({"interval_seconds": 60, "consecutive_errors": 3}) == 480
    assert effective_interval({"interval_seconds": 60, "consecutive_errors": 99}) == 60 * BACKOFF_CAP


async def test_recompile_rebuilds_the_checker(db: DB, workspace, page):
    page["body"] = b"in stock"
    plan = json.dumps({
        "name": "Stock", "value_description": "availability text", "value_kind": "text",
        "needs_condition": False, "interval_seconds": 300,
    })  # fmt: skip

    def draft(func: str) -> str:
        return json.dumps({
            "name": func, "description": f"read the availability text from a file ({func})",
            "input_schema": SCHEMA,
            "code": f"def {func}(path):\n    return {{'value': open(path).read().strip()}}\n",
            "test_code": (
                "import tempfile, os\n"
                "p = os.path.join(tempfile.mkdtemp(), 'x')\n"
                "open(p, 'w').write('hi')\n"
                f"assert {func}(p) == {{'value': 'hi'}}\n"
            ),
            "deps": [], "network": False,
        })  # fmt: skip

    first = await compile_watch(
        db, LocalSandbox(), request="tell me when stock changes", url="http://x",
        providers={"planner": FakeProvider([plan]), "toolsmith": FakeProvider([draft("check_stock")])},
    )  # fmt: skip
    assert first.ok, first.message
    assert db.get_watch(first.watch_id)["extractor_tool"] == "check_stock"

    second = await recompile_watch(
        db, LocalSandbox(), first.watch_id,
        providers={"planner": FakeProvider([plan]), "toolsmith": FakeProvider([draft("check_stock_v2")])},
    )  # fmt: skip
    assert second.ok, second.message
    watch = db.get_watch(first.watch_id)
    assert watch["extractor_tool"] == "check_stock_v2"  # swapped to the rebuilt tool
    assert watch["status"] == "active"
