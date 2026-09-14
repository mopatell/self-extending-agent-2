"""Agent-written tools: validate, store (versioned, in SQL), and load them for the runtime.

Everything an LLM proposes goes through `validate()` before it can be tested,
and through the sandbox tests before it can be registered.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import BaseModel, Field, ValidationError

from sea.config import settings
from sea.db import DB
from sea.llm import ToolSpec
from sea.sandbox import allowlist
from sea.sandbox.runner import Result, Sandbox, call_tool, run_tool_tests
from sea.tools.base import Tool, ToolContext

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


class ToolDraft(BaseModel):
    """What the ToolSmith must produce."""

    name: str = Field(pattern=NAME_RE.pattern)
    description: str = Field(min_length=10, max_length=300)
    input_schema: dict[str, Any]
    code: str = Field(min_length=10)
    test_code: str = Field(min_length=5)
    deps: list[str] = []
    network: bool = False

    @staticmethod
    def parse(text: str) -> ToolDraft:
        """Lenient JSON extraction (models sometimes wrap JSON in prose or fences)."""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("no JSON object in response")
        try:
            return ToolDraft.model_validate_json(match.group())
        except ValidationError as e:
            raise ValueError(
                "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            ) from e


def validate(draft: ToolDraft, reserved: set[str]) -> list[str]:
    """Static checks. Returns a list of problems (empty = ok)."""
    problems: list[str] = []

    if draft.name in reserved:
        problems.append(f"name {draft.name!r} is already taken by a built-in tool")

    try:
        jsonschema.Draft202012Validator.check_schema(draft.input_schema)
        if draft.input_schema.get("type") != "object":
            problems.append("input_schema.type must be 'object'")
    except jsonschema.SchemaError as e:
        problems.append(f"input_schema is not valid JSON Schema: {e.message}")

    for dep in draft.deps:
        if dep not in allowlist.ALLOWED_PACKAGES:
            problems.append(
                f"dependency {dep!r} is not allowed; allowed: {sorted(allowlist.ALLOWED_PACKAGES)}"
            )

    try:
        tree = ast.parse(draft.code)
    except SyntaxError as e:
        return problems + [f"code has a syntax error: {e}"]

    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == draft.name), None)
    if fn is None:
        problems.append(f"code must define a function named {draft.name!r}")
    else:
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        declared = set(draft.input_schema.get("properties", {}))
        if params != declared:
            problems.append(f"function parameters {sorted(params)} != schema properties {sorted(declared)}")
        required = {a.arg for a in fn.args.args[: len(fn.args.args) - len(fn.args.defaults)]}
        missing = required - set(draft.input_schema.get("required", []))
        if missing:
            problems.append(f"parameters without defaults must be in schema.required: {sorted(missing)}")

    imported = _imports(tree)
    allowed_imports = set().union(
        *(allowlist.ALLOWED_PACKAGES[d] for d in draft.deps if d in allowlist.ALLOWED_PACKAGES)
    )
    for mod in imported & allowlist.FORBIDDEN_MODULES:
        problems.append(f"import of {mod!r} is not allowed in tools")
    for mod in imported & allowlist.NETWORK_MODULES:
        if not draft.network:
            problems.append(f"{mod!r} needs the network; set network=true or separate fetching from parsing")
    third_party = {m for m in imported if m in set().union(*allowlist.ALLOWED_PACKAGES.values())}
    for mod in third_party - allowed_imports:
        problems.append(f"import of {mod!r} requires declaring its package in deps")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in allowlist.FORBIDDEN_CALLS:
                problems.append(f"call to {f.id}() is not allowed")
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                if (f.value.id, f.attr) in allowlist.FORBIDDEN_ATTRS:
                    problems.append(f"call to {f.value.id}.{f.attr}() is not allowed")

    if "assert" not in draft.test_code:
        problems.append("test_code must contain assert statements")
    return problems


def _imports(tree: ast.AST) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


class Registry:
    def __init__(self, db: DB, sandbox: Sandbox, mirror_dir: Path | None = None):
        self.db = db
        self.sandbox = sandbox
        self.mirror_dir = mirror_dir if mirror_dir is not None else settings.db_path.parent / "tools"

    def exists(self, name: str) -> dict[str, Any] | None:
        return self.db.latest_tool(name)

    async def test(self, draft: ToolDraft, workspace: Path, extra_tests: str = "") -> Result:
        tests = draft.test_code + ("\n\n# previous version's tests\n" + extra_tests if extra_tests else "")
        return await run_tool_tests(self.sandbox, draft.code, tests, workspace)

    def register(self, draft: ToolDraft, created_by_run: str | None) -> int:
        """Stores a new version (1 if new, previous+1 if editing). Caller must have validated + tested."""
        previous = self.db.latest_tool(draft.name)
        version = previous["version"] + 1 if previous else 1
        self.db.insert_tool(
            name=draft.name,
            version=version,
            description=draft.description,
            input_schema=draft.input_schema,
            source=draft.code,
            test_code=draft.test_code,
            deps=draft.deps,
            risk="approval" if draft.network else "safe",
            created_by_run=created_by_run,
        )
        self.mirror_dir.mkdir(parents=True, exist_ok=True)
        header = f'"""{draft.description}\n\nversion {version} · deps {draft.deps or "none"}\n"""\n\n'
        (self.mirror_dir / f"{draft.name}.py").write_text(
            header + draft.code + "\n\n\nif __name__ == '__main__':\n" + _indent(draft.test_code) + "\n"
        )
        return version

    def load(self) -> dict[str, Tool]:
        """Active generated tools as runtime Tool objects that execute in the sandbox."""
        tools: dict[str, Tool] = {}
        for row in self.db.active_tools():
            tools[row["name"]] = self._to_tool(row)
        return tools

    def _to_tool(self, row: dict[str, Any]) -> Tool:
        source, name, network = row["source"], row["name"], row["risk"] == "approval"
        sandbox = self.sandbox

        async def fn(ctx: ToolContext, **arguments: Any) -> str:
            result = await call_tool(sandbox, source, name, arguments, ctx.workspace, network)
            if not result.ok:
                raise RuntimeError(result.output)
            return result.output

        return Tool(
            ToolSpec(name, row["description"], row["input_schema"]),
            fn,
            risk=row["risk"],
            version=row["version"],
            generated=True,
        )


def _indent(code: str) -> str:
    return "\n".join("    " + line if line.strip() else line for line in code.splitlines())
