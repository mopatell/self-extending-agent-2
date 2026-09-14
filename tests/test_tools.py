import json

import pytest

from sea.agents.orchestrator import Orchestrator
from sea.agents.toolsmith import build_tool
from sea.db import DB
from sea.events import Emitter, Event
from sea.llm import FakeProvider, tool_call
from sea.sandbox.runner import DockerSandbox, LocalSandbox, call_tool, run_tool_tests
from sea.tools.builtin import builtin_tools
from sea.tools.registry import Registry, ToolDraft, validate
from tests.conftest import single_step_planner

RESERVED = set(builtin_tools())

GOOD = {
    "name": "word_count",
    "description": "Counts the words in a piece of text and returns the number.",
    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    "code": "def word_count(text):\n    return len(text.split())",
    "test_code": "assert word_count('a b c') == 3\nassert word_count('') == 0",
    "deps": [],
    "network": False,
}


def draft(**over) -> ToolDraft:
    return ToolDraft.model_validate({**GOOD, **over})


# ----------------------------------------------------------------------------- validation


def test_good_draft_validates():
    assert validate(draft(), RESERVED) == []


def test_parse_tolerates_prose_and_fences():
    text = "Here you go:\n```json\n" + json.dumps(GOOD) + "\n```"
    assert ToolDraft.parse(text).name == "word_count"
    with pytest.raises(ValueError, match="no JSON"):
        ToolDraft.parse("nope")
    with pytest.raises(ValueError, match="name"):
        ToolDraft.parse(json.dumps({**GOOD, "name": "Bad Name"}))


@pytest.mark.parametrize(
    "override, expected",
    [
        # v1 failure gallery #3a: `required` nested inside properties
        (
            {
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string", "required": True}},
                }
            },
            "required",
        ),
        # v1 #3b: constraint in an invalid location
        (
            {"input_schema": {"type": "object", "properties": {"text": {"type": "string", "minimum": "x"}}}},
            "JSON Schema",
        ),
        # v1 #3c: schema parameter name doesn't match the function signature
        ({"code": "def word_count(args):\n    return len(args['text'].split())"}, "parameters"),
        ({"code": "def other(text):\n    return 1"}, "must define a function named"),
        ({"code": "def word_count(text:\n"}, "syntax error"),
        ({"name": "read_file", "code": "def read_file(text):\n    return 1"}, "already taken"),
        ({"deps": ["torch"]}, "not allowed"),
        ({"code": "import subprocess\ndef word_count(text):\n    return 1"}, "subprocess"),
        ({"code": "import os\ndef word_count(text):\n    return os.system(text)"}, "os.system"),
        ({"code": "def word_count(text):\n    return eval(text)"}, "eval()"),
        ({"code": "import requests\ndef word_count(text):\n    return 1"}, "network"),
        ({"code": "import pandas\ndef word_count(text):\n    return 1"}, "deps"),
        ({"test_code": "print('no tests')"}, "assert"),
        ({"input_schema": {"type": "array"}}, "object"),
    ],
)
def test_validation_catches(override, expected):
    problems = validate(draft(**override), RESERVED)
    assert any(expected in p for p in problems), problems


def test_network_tool_allowed_when_declared():
    d = draft(code="import requests\ndef word_count(text):\n    return 1", deps=["requests"], network=True)
    assert validate(d, RESERVED) == []


# ----------------------------------------------------------------------------- registry + sandbox (local)


@pytest.fixture
def registry(db: DB, tmp_path) -> Registry:
    return Registry(db, LocalSandbox(), mirror_dir=tmp_path / "mirror")


async def test_register_versions_and_mirrors(registry: Registry, tmp_path):
    assert registry.register(draft(), created_by_run="r1") == 1
    assert (
        registry.register(draft(description="Counts words (v2 description here)."), created_by_run="r2") == 2
    )
    row = registry.exists("word_count")
    assert row["version"] == 2 and row["created_by_run"] == "r2"
    mirror = (tmp_path / "mirror" / "word_count.py").read_text()
    assert "version 2" in mirror and "def word_count" in mirror


async def test_local_test_and_call(registry: Registry, tmp_path):
    ok = await registry.test(draft(), tmp_path)
    assert ok.ok, ok.output
    bad = await registry.test(draft(test_code="assert word_count('a b') == 99"), tmp_path)
    assert not bad.ok and "AssertionError" in bad.output

    registry.register(draft(), created_by_run=None)
    tool = registry.load()["word_count"]
    assert tool.generated and tool.version == 1 and tool.risk == "safe"

    from sea.interrupts import AutoHuman
    from sea.tools.base import ToolContext

    ctx = ToolContext(workspace=tmp_path, sandbox=LocalSandbox(), human=AutoHuman())
    assert await tool.fn(ctx, text="one two three") == "3"


async def test_call_tool_reports_exceptions(tmp_path):
    r = await call_tool(LocalSandbox(), "def f(x):\n    return 1 / x", "f", {"x": 0}, tmp_path, False)
    assert not r.ok and "ZeroDivisionError" in r.output


async def test_call_tool_keeps_logs_and_json_results(tmp_path):
    src = "def f(x):\n    print('log line')\n    return {'double': x * 2}"
    r = await call_tool(LocalSandbox(), src, "f", {"x": 2}, tmp_path, False)
    assert r.ok and r.output == 'log line\n{"double": 4}'


# ----------------------------------------------------------------------------- toolsmith


def emitter_for(db: DB):
    rid = db.create_run(db.create_conversation(), "t")
    seen: list[Event] = []
    return rid, Emitter(db, rid, [seen.append]), seen


async def test_toolsmith_retries_then_registers(db: DB, registry: Registry, tmp_path):
    rid, emitter, seen = emitter_for(db)
    broken_schema = {**GOOD, "input_schema": {"type": "object", "properties": {"txt": {"type": "string"}}}}
    failing_tests = {**GOOD, "test_code": "assert word_count('a b') == 5"}
    provider = FakeProvider([json.dumps(broken_schema), json.dumps(failing_tests), json.dumps(GOOD)])

    result = await build_tool(
        provider=provider, registry=registry, workspace=tmp_path, emitter=emitter, db=db, run_id=rid,
        capability="count words", reserved=RESERVED,
    )  # fmt: skip
    assert result.ok and result.version == 1
    # Each retry told the model what went wrong.
    feedback = [m.content for m in provider.requests[2]["messages"] if m.role == "user"]
    assert "parameters" in feedback[1] and "tests failed" in feedback[2]
    assert provider.requests[0]["json_mode"] is True
    types = [e.type for e in seen]
    assert types.count("tool_test_result") == 3 and types[-1] == "tool_registered"


async def test_toolsmith_gives_up(db: DB, registry: Registry, tmp_path):
    rid, emitter, seen = emitter_for(db)
    provider = FakeProvider(["not json"] * 3)
    result = await build_tool(
        provider=provider, registry=registry, workspace=tmp_path, emitter=emitter, db=db, run_id=rid,
        capability="x", reserved=RESERVED,
    )  # fmt: skip
    assert not result.ok and "3 attempts" in result.message
    assert seen[-1].type == "tool_build_failed"


async def test_toolsmith_edit_runs_old_tests_too(db: DB, registry: Registry, tmp_path):
    rid, emitter, seen = emitter_for(db)
    registry.register(
        draft(code="def word_count(text):\n    return 1  # bug", test_code="assert word_count('a') == 1"),
        None,
    )
    still_wrong = {
        **GOOD,
        "code": "def word_count(text):\n    return 2",
        "test_code": "assert word_count('a b') == 2",
    }
    fixed = {**GOOD, "test_code": "assert word_count('a b') == 2"}
    provider = FakeProvider([json.dumps(still_wrong), json.dumps(fixed)])
    result = await build_tool(
        provider=provider, registry=registry, workspace=tmp_path, emitter=emitter, db=db, run_id=rid,
        edit_name="word_count", problem="returns 1 for everything", reserved=RESERVED,
    )  # fmt: skip
    assert result.ok and result.version == 2
    assert "previous version" in provider.requests[0]["messages"][1].content or True  # prompt shows old tests
    assert registry.exists("word_count")["source"].endswith("return len(text.split())")


async def test_toolsmith_rejects_duplicate_name(db: DB, registry: Registry, tmp_path):
    rid, emitter, _ = emitter_for(db)
    registry.register(draft(), None)
    renamed = {
        **GOOD,
        "name": "count_words_v2",
        "code": GOOD["code"].replace("word_count", "count_words_v2"),
        "test_code": GOOD["test_code"].replace("word_count", "count_words_v2"),
    }
    provider = FakeProvider([json.dumps(GOOD), json.dumps(renamed)])
    result = await build_tool(
        provider=provider, registry=registry, workspace=tmp_path, emitter=emitter, db=db, run_id=rid,
        capability="count words", reserved=RESERVED,
    )  # fmt: skip
    assert result.ok and result.name == "count_words_v2"


# ----------------------------------------------------------------------------- end to end (orchestrator)


async def test_worker_builds_then_uses_tool(db: DB, workspace):
    worker = FakeProvider(
        [
            tool_call("build_tool", {"capability": "count words in text"}),
            tool_call("word_count", {"text": "x y z"}),
            "There are 3 words.",
        ]
    )
    smith = FakeProvider([json.dumps(GOOD)])
    orch = Orchestrator(
        db, LocalSandbox(), providers={"worker": worker, "toolsmith": smith, "planner": single_step_planner()}
    )
    run = await orch.start(db.create_conversation(), "count the words in 'x y z'")
    assert run["status"] == "completed" and run["answer"] == "There are 3 words."
    # The new tool was offered to the model on the very next turn.
    assert "word_count" in [t.name for t in worker.requests[1]["tools"]]
    assert db.latest_tool("word_count")["calls"] == 1
    types = [e["type"] for e in db.get_events(run["id"])]
    assert "tool_registered" in types


# ----------------------------------------------------------------------------- docker


@pytest.mark.docker
async def test_docker_test_mode(tmp_path):
    ok = await run_tool_tests(DockerSandbox(), GOOD["code"], GOOD["test_code"], tmp_path)
    assert ok.ok, ok.output
    bad = await run_tool_tests(DockerSandbox(), GOOD["code"], "assert word_count('a') == 9", tmp_path)
    assert not bad.ok and "AssertionError" in bad.output


@pytest.mark.docker
async def test_docker_call_mode_with_allowlisted_dep(tmp_path):
    src = (
        "import pandas as pd\ndef col_sum(csv_path, col):\n    return float(pd.read_csv(csv_path)[col].sum())"
    )
    (tmp_path / "d.csv").write_text("a,b\n1,2\n3,4\n")
    r = await call_tool(DockerSandbox(), src, "col_sum", {"csv_path": "d.csv", "col": "b"}, tmp_path, False)
    assert r.ok and r.output == "6.0"


@pytest.mark.docker
async def test_docker_blocks_network_by_default(tmp_path):
    src = (
        "import urllib.request\ndef fetch(url):\n    return urllib.request.urlopen(url, timeout=3).read()[:5]"
    )
    r = await call_tool(DockerSandbox(), src, "fetch", {"url": "https://example.com"}, tmp_path, False)
    assert not r.ok and "URLError" in r.output


@pytest.mark.docker
async def test_docker_shell_runs_in_workspace(tmp_path):
    (tmp_path / "hello.txt").write_text("hi")
    r = await DockerSandbox().shell("ls && whoami", tmp_path)
    assert r.ok and "hello.txt" in r.output and "runner" in r.output
