"""ToolSmith: writes a missing tool (or fixes a broken one), proves it works, registers it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sea.db import DB
from sea.events import Emitter
from sea.llm import Message, Provider
from sea.sandbox import allowlist
from sea.tools.registry import Registry, ToolDraft, validate

MAX_ATTEMPTS = 3

SYSTEM = """You write small, reliable Python tools for an AI agent. Reply with ONE JSON object, no prose:

{{
  "name": "snake_case_name",
  "description": "one sentence: what it does and what it returns",
  "input_schema": {{"type": "object", "properties": {{...}}, "required": [...]}},
  "code": "def snake_case_name(...):\\n    ...",
  "test_code": "assert snake_case_name(...) == ...\\n...",
  "deps": [],
  "network": false
}}

Rules:
- `code` defines exactly one top-level function named `name`; parameters must match input_schema properties.
- Return JSON-serialisable values (str, int, float, bool, list, dict). Return a value, don't print it.
- Prefer the standard library. Allowed extra packages (list them in deps): {packages}.
- Never import {forbidden}. Never call eval/exec/os.system.
- Separate fetching from computing: the agent already has an http_get tool, so tools should take data
  (text, a file path in the working directory, numbers) rather than URLs. Only set network=true if there is
  no other way, and then tests must not touch the network.
- test_code: 3-6 assert statements on real inputs with known outputs, including one edge case.
  Tests run in the same working directory as the tool; they may create temp files there.
- Keep it short and readable. No classes, no globals, no argparse.
"""

WRITE = "Write a tool for this capability:\n\n{capability}"

EDIT = """Fix an existing tool. Keep the same name and input_schema unless the problem requires otherwise.

Tool: {name}
Problem reported: {problem}

Current code:
{code}

Current tests (these must still pass):
{tests}
"""

FEEDBACK = "That attempt failed:\n\n{error}\n\nReturn a corrected JSON object."


@dataclass
class BuildResult:
    ok: bool
    name: str | None
    version: int | None
    message: str


async def build_tool(
    *,
    provider: Provider,
    registry: Registry,
    workspace: Path,
    emitter: Emitter,
    db: DB,
    run_id: str,
    capability: str | None = None,
    edit_name: str | None = None,
    problem: str | None = None,
    reserved: set[str],
) -> BuildResult:
    """Synthesize (capability) or edit (edit_name + problem) a tool. Exactly one mode."""
    previous = registry.exists(edit_name) if edit_name else None
    if edit_name and not previous:
        return BuildResult(False, edit_name, None, f"no tool named {edit_name!r} to edit")

    if previous:
        request = EDIT.format(
            name=edit_name, problem=problem, code=previous["source"], tests=previous["test_code"]
        )
    else:
        request = WRITE.format(capability=capability)
    messages = [
        Message.system(
            SYSTEM.format(
                packages=", ".join(sorted(allowlist.ALLOWED_PACKAGES)),
                forbidden=", ".join(sorted(allowlist.FORBIDDEN_MODULES)),
            )
        ),
        Message.user(request),
    ]
    label = edit_name or capability or ""
    emitter.emit(
        "tool_gap_found", name=edit_name or "?", description=label, mode="edit" if previous else "write"
    )

    error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        completion = await provider.complete(messages, json_mode=True)
        db.add_usage(run_id, completion.usage.input_tokens, completion.usage.output_tokens)
        emitter.emit(
            "llm_called",
            model=provider.model,
            tokens_in=completion.usage.input_tokens,
            tokens_out=completion.usage.output_tokens,
            tool_calls=[],
            role="toolsmith",
        )
        messages.append(completion.as_message())

        try:
            draft = ToolDraft.parse(completion.text)
            if previous and draft.name != edit_name:
                raise ValueError(f"keep the name {edit_name!r} when editing")
            problems = validate(draft, reserved)
            if problems:
                raise ValueError("\n".join(f"- {p}" for p in problems))
            if not previous and registry.exists(draft.name):
                raise ValueError(f"a tool named {draft.name!r} already exists; pick a more specific name")
        except ValueError as e:
            error = f"validation: {e}"
            emitter.emit("tool_test_result", name=None, attempt=attempt, passed=False, output=error)
            messages.append(Message.user(FEEDBACK.format(error=error)))
            continue

        emitter.emit(
            "tool_synthesized", name=draft.name, attempt=attempt, deps=draft.deps, network=draft.network
        )
        result = await registry.test(draft, workspace, extra_tests=previous["test_code"] if previous else "")
        emitter.emit(
            "tool_test_result",
            name=draft.name,
            attempt=attempt,
            passed=result.ok,
            output=result.output[-1500:],
        )
        if not result.ok:
            error = f"tests failed:\n{result.output[-1500:]}"
            messages.append(Message.user(FEEDBACK.format(error=error)))
            continue

        version = registry.register(draft, created_by_run=run_id)
        emitter.emit("tool_registered", name=draft.name, version=version, description=draft.description)
        return BuildResult(
            True, draft.name, version, f"Tool {draft.name!r} v{version} is ready: {draft.description}"
        )

    emitter.emit("tool_build_failed", name=edit_name or "?", error=error, attempts=MAX_ATTEMPTS)
    return BuildResult(
        False, edit_name, None, f"Could not build the tool after {MAX_ATTEMPTS} attempts. Last error: {error}"
    )
