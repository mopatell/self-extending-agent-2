"""Worker: completes one plan step using tools."""

from __future__ import annotations

from typing import Any

from sea.db import DB
from sea.events import Emitter
from sea.interrupts import Human
from sea.llm import Message, Provider
from sea.loop import LoopResult, run_loop
from sea.tools.base import Tool, ToolContext

SYSTEM = """You are a worker agent completing ONE step of a larger task.

Rules:
- Do ONLY your step, then stop. Other steps are handled by other workers; do not do their work.
- Use the tools. Never pretend to have run a tool or invent its output.
- If the step asks for something software cannot do (time travel, physical actions), say so plainly.
  Do not build a tool that fakes it.
- Do not re-read a file you just wrote to double-check it; trust the tool result.
- If a tool errors, read the error and try a different approach.
- Prefer existing tools. If a computation is needed that no tool can do, call build_tool with a precise
  description of inputs and outputs, then use the new tool. Do not do the computation in your head.
- If an agent-written tool returns a wrong, empty or suspicious result, first check you passed the right
  arguments (read the data first if needed), then call fix_tool. Never compute the result yourself instead.
- If the step is genuinely ambiguous, call ask_human once with a specific question.
- When the step is done, reply with a short, factual summary of what you did and the result.
  Include any values the next steps will need (numbers, file names, findings).

Tools available:
{catalog}
"""


def build_messages(
    step: dict[str, Any],
    task: str,
    upstream: dict[str, str],
    tools: dict[str, Tool],
    notes: list[str] | None = None,
) -> list[Message]:
    catalog = "\n".join(t.catalog_line() for t in tools.values())
    context = ""
    if upstream:
        context = "\n\nResults from earlier steps:\n" + "\n".join(
            f"[{sid}] {result}" for sid, result in upstream.items()
        )
    if notes:
        context += "\n\nNotes:\n" + "\n".join(f"- {n}" for n in notes)
    user = f"Overall task: {task}\n\nYour step ({step['id']}): {step['description']}{context}"
    return [Message.system(SYSTEM.format(catalog=catalog)), Message.user(user)]


async def run_step(
    *,
    provider: Provider,
    messages: list[Message],
    tools: dict[str, Tool],
    ctx: ToolContext,
    emitter: Emitter,
    human: Human,
    db: DB,
    run_id: str,
) -> LoopResult:
    return await run_loop(
        provider=provider,
        messages=messages,
        tools=tools,
        ctx=ctx,
        emitter=emitter,
        human=human,
        db=db,
        run_id=run_id,
    )
