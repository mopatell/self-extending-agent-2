"""SQLite storage. One small class, plain SQL, no ORM.

Calls are synchronous: SQLite writes take microseconds, so wrapping them in
threads would add more complexity than it removes for this project.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from sea.config import settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class DB:
    def __init__(self, path: str | Path | None = None):
        path = Path(path) if path is not None else settings.db_path
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
        applied = {r["name"] for r in self.conn.execute("SELECT name FROM schema_migrations")}
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if sql_file.name in applied:
                continue
            with self.conn:
                self.conn.executescript(sql_file.read_text())
                self.conn.execute("INSERT INTO schema_migrations VALUES (?)", (sql_file.name,))

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------------- helpers

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ----------------------------------------------------------------- conversations

    def create_conversation(self, title: str = "") -> str:
        cid = new_id()
        self.conn.execute("INSERT INTO conversations (id, title) VALUES (?, ?)", (cid, title))
        return cid

    def get_conversation(self, cid: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM conversations WHERE id = ?", (cid,))

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM conversations ORDER BY created_at DESC LIMIT ?", (limit,))

    def add_message(self, cid: str, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)", (cid, role, content)
        )

    def get_messages(self, cid: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (cid,))

    # ----------------------------------------------------------------- runs

    def create_run(self, cid: str, task: str) -> str:
        rid = new_id()
        self.conn.execute("INSERT INTO runs (id, conversation_id, task) VALUES (?, ?, ?)", (rid, cid, task))
        return rid

    def get_run(self, rid: str) -> dict[str, Any] | None:
        run = self._one("SELECT * FROM runs WHERE id = ?", (rid,))
        if run:
            run["plan"] = json.loads(run["plan_json"]) if run["plan_json"] else None
            run["state"] = json.loads(run["state_json"]) if run["state_json"] else None
        return run

    def list_runs(self, cid: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if cid:
            return self._all(
                "SELECT * FROM runs WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?", (cid, limit)
            )
        return self._all("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))

    def update_run(self, rid: str, **fields: Any) -> None:
        """update_run(id, status='running', plan={...}, state={...}, answer='...')."""
        if "plan" in fields:
            fields["plan_json"] = json.dumps(fields.pop("plan"))
        if "state" in fields:
            fields["state_json"] = json.dumps(fields.pop("state"))
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), rid))

    def add_usage(self, rid: str, tokens_in: int, tokens_out: int) -> None:
        self.conn.execute(
            "UPDATE runs SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ? WHERE id = ?",
            (tokens_in, tokens_out, rid),
        )

    # ----------------------------------------------------------------- events

    def add_event(self, rid: str, type_: str, payload: dict[str, Any]) -> int:
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?", (rid,)
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO events (run_id, seq, type, payload_json) VALUES (?, ?, ?, ?)",
            (rid, seq, type_, json.dumps(payload, default=str)),
        )
        return seq

    def get_events(self, rid: str, after_seq: int = 0) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT seq, type, payload_json, created_at FROM events "
            "WHERE run_id = ? AND seq > ? ORDER BY seq",
            (rid, after_seq),
        )
        for r in rows:
            r["payload"] = json.loads(r.pop("payload_json"))
        return rows

    # ----------------------------------------------------------------- approvals

    def create_approval(self, rid: str, kind: str, payload: dict[str, Any]) -> str:
        aid = new_id()
        self.conn.execute(
            "INSERT INTO approvals (id, run_id, kind, payload_json) VALUES (?, ?, ?, ?)",
            (aid, rid, kind, json.dumps(payload, default=str)),
        )
        return aid

    def get_approval(self, aid: str) -> dict[str, Any] | None:
        a = self._one("SELECT * FROM approvals WHERE id = ?", (aid,))
        if a:
            a["payload"] = json.loads(a.pop("payload_json"))
            a["decision"] = json.loads(a["decision_json"]) if a["decision_json"] else None
        return a

    def pending_approvals(self, rid: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM approvals WHERE run_id = ? AND status = 'pending' ORDER BY created_at", (rid,)
        )
        for a in rows:
            a["payload"] = json.loads(a.pop("payload_json"))
        return rows

    def resolve_approval(self, aid: str, status: str, decision: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE approvals SET status = ?, decision_json = ?, resolved_at = ? WHERE id = ?",
            (status, json.dumps(decision), _now(), aid),
        )

    # ----------------------------------------------------------------- tools

    def insert_tool(self, **t: Any) -> None:
        self.conn.execute(
            """INSERT INTO tools (name, version, description, input_schema_json, source, test_code,
                                  deps_json, risk, created_by_run)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t["name"],
                t["version"],
                t["description"],
                json.dumps(t["input_schema"]),
                t["source"],
                t["test_code"],
                json.dumps(t.get("deps", [])),
                t.get("risk", "safe"),
                t.get("created_by_run"),
            ),
        )

    def latest_tool(self, name: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM tools WHERE name = ? ORDER BY version DESC LIMIT 1", (name,))
        return self._tool_row(row) if row else None

    def active_tools(self) -> list[dict[str, Any]]:
        rows = self._all(
            """SELECT t.* FROM tools t
               JOIN (SELECT name, MAX(version) AS v FROM tools GROUP BY name) m
                 ON t.name = m.name AND t.version = m.v
               WHERE t.status = 'active' ORDER BY t.name"""
        )
        return [self._tool_row(r) for r in rows]

    def set_tool_status(self, name: str, status: str) -> None:
        self.conn.execute("UPDATE tools SET status = ? WHERE name = ?", (status, name))

    def record_tool_call(self, name: str, version: int, failed: bool) -> None:
        self.conn.execute(
            "UPDATE tools SET calls = calls + 1, failures = failures + ? WHERE name = ? AND version = ?",
            (1 if failed else 0, name, version),
        )

    @staticmethod
    def _tool_row(row: dict[str, Any]) -> dict[str, Any]:
        row["input_schema"] = json.loads(row.pop("input_schema_json"))
        row["deps"] = json.loads(row.pop("deps_json"))
        return row
