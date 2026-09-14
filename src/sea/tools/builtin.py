"""The fixed toolbelt every worker gets. Paths are relative to the run's workspace."""

from __future__ import annotations

import asyncio
import urllib.request
from pathlib import Path

from sea.interrupts import QUESTION
from sea.llm import ToolSpec
from sea.tools.base import Tool, ToolContext


def _resolve(ctx: ToolContext, path: str) -> Path:
    target = (ctx.workspace / path).resolve()
    if ctx.workspace not in target.parents and target != ctx.workspace:
        raise ValueError(f"path {path!r} escapes the workspace")
    return target


async def read_file(ctx: ToolContext, path: str) -> str:
    return _resolve(ctx, path).read_text()


async def write_file(ctx: ToolContext, path: str, content: str) -> str:
    target = _resolve(ctx, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"wrote {len(content)} chars to {path}"


async def list_dir(ctx: ToolContext, path: str = ".") -> str:
    target = _resolve(ctx, path)
    entries = sorted(target.iterdir())
    if not entries:
        return "(empty)"
    return "\n".join(f"{e.name}/" if e.is_dir() else f"{e.name} ({e.stat().st_size} B)" for e in entries)


async def run_shell(ctx: ToolContext, command: str) -> str:
    result = await ctx.sandbox.shell(command, ctx.workspace)
    return result.output or ("(no output)" if result.ok else "(command failed with no output)")


async def http_get(ctx: ToolContext, url: str) -> str:
    def fetch() -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "sea-agent/0.2"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read(200_000).decode(errors="replace")

    return await asyncio.to_thread(fetch)


async def ask_human(ctx: ToolContext, question: str) -> str:
    key = f"question:{ctx.step_id}:{abs(hash(question)) % 100_000}"
    decision = await ctx.human.decide(QUESTION, key, {"question": question, "step_id": ctx.step_id})
    return decision.get("answer", "")


async def build_tool(ctx: ToolContext, capability: str) -> str:
    if ctx.toolsmith is None:
        return "Error: tool building is not available in this context"
    return await ctx.toolsmith(capability=capability)


async def fix_tool(ctx: ToolContext, name: str, problem: str) -> str:
    if ctx.toolsmith is None:
        return "Error: tool editing is not available in this context"
    return await ctx.toolsmith(edit_name=name, problem=problem)


def _spec(name: str, description: str, props: dict, required: list[str]) -> ToolSpec:
    return ToolSpec(
        name,
        description,
        {"type": "object", "properties": props, "required": required, "additionalProperties": False},
    )


BUILTIN_TOOLS: list[Tool] = [
    Tool(
        _spec("read_file", "Read a text file from the workspace.", {"path": {"type": "string"}}, ["path"]),
        read_file,
    ),
    Tool(
        _spec("list_dir", "List files in a workspace directory.", {"path": {"type": "string"}}, []),
        list_dir,
    ),
    Tool(
        _spec(
            "write_file",
            "Write text to a file in the workspace (creates or overwrites).",
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
        write_file,
        risk="approval",
    ),
    Tool(
        _spec(
            "run_shell",
            "Run a bash command in the workspace and return its output.",
            {"command": {"type": "string"}},
            ["command"],
        ),
        run_shell,
        risk="approval",
    ),
    Tool(
        _spec(
            "http_get",
            "Fetch a URL and return the response body (first 200KB).",
            {"url": {"type": "string"}},
            ["url"],
        ),
        http_get,
        risk="approval",
    ),
    Tool(
        _spec(
            "ask_human",
            "Ask the user a clarifying question when the task is ambiguous. Use sparingly.",
            {"question": {"type": "string"}},
            ["question"],
        ),
        ask_human,
    ),
    Tool(
        _spec(
            "build_tool",
            "Create a new reusable Python tool when no existing tool can do a needed computation. "
            "Describe precisely what it takes and returns; it is written, tested and made available to you.",
            {"capability": {"type": "string"}},
            ["capability"],
        ),
        build_tool,
    ),
    Tool(
        _spec(
            "fix_tool",
            "Repair an agent-written tool that returns wrong results. Give its name and what is wrong.",
            {"name": {"type": "string"}, "problem": {"type": "string"}},
            ["name", "problem"],
        ),
        fix_tool,
    ),
]


def builtin_tools() -> dict[str, Tool]:
    return {t.name: t for t in BUILTIN_TOOLS}
