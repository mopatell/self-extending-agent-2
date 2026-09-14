import json

import pytest

from sea.db import DB
from sea.llm import FakeProvider


@pytest.fixture
def db() -> DB:
    d = DB(":memory:")
    yield d
    d.close()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point workspaces and the DB mirror dir at a temp folder; auto-approve plans."""
    monkeypatch.setattr("sea.config.settings.workspaces_dir", tmp_path)
    monkeypatch.setattr("sea.config.settings.db_path", tmp_path / "db.sqlite")
    monkeypatch.setattr("sea.config.settings.auto_approve", "safe")
    return tmp_path


def plan_json(*steps: dict) -> str:
    """Build a planner reply. plan_json({"id": "s1", "description": "..."}) - other fields default."""
    full = [{"depends_on": [], "tools": [], "missing_tools": [], **s} for s in steps]
    return json.dumps({"steps": full})


def single_step_planner(task: str = "do it") -> FakeProvider:
    return FakeProvider([plan_json({"id": "s1", "description": task})])
