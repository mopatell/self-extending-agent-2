"""The agent loop: ask the model, run the tools it asks for, repeat until it answers.

Every role (worker, toolsmith, ...) uses this same function with a different
system prompt, tool set and model. The loop mutates `messages` in place so the
caller can checkpoint it; if the loop is re-entered with unanswered tool calls
at the end of `messages` (after a human interrupt), it picks up from there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sea.config import settings
from sea.db import DB
from sea.events import Emitter
from sea.interrupts import TOOL_CALL, Human
from sea.llm import Message, Provider, ToolCall
from sea.policy import ALLOW, DENY, classify
from sea.tools.base import Tool, ToolContext, truncate


@dataclass
class LoopResult:
    text: str
    steps: int
    stopped_early: bool = False


async def run_loop(
    *,
    provider: Provider,
    messages: list[Message],
    tools: dict[str, Tool],
    ctx: ToolContext,
    emitter: Emitter,
    human: Human,
    db: DB,
    run_id: str,
    max_steps: int | None = None,
) -> LoopResult:
    max_steps = max_steps or settings.max_loop_steps

    # Resume: finish any tool calls the previous attempt didn't get to.
    pending = _unanswered_calls(messages)
    if pending:
        await _handle_calls(pending, messages, tools, ctx, emitter, human, db)

    for step in range(1, max_steps + 1):
        specs = [t.spec for t in tools.values()]  # recomputed: build_tool may add tools mid-run
        completion = await provider.complete(messages, tools=specs)
        db.add_usage(run_id, completion.usage.input_tokens, completion.usage.output_tokens)
        emitter.emit(
            "llm_called",
            model=provider.model,
            tokens_in=completion.usage.input_tokens,
            tokens_out=completion.usage.output_tokens,
            tool_calls=[tc.name for tc in completion.tool_calls],
        )
        messages.append(completion.as_message())

        if not completion.tool_calls:
            return LoopResult(completion.text, step)

        await _handle_calls(completion.tool_calls, messages, tools, ctx, emitter, human, db)

    messages.append(Message.user("You have run out of steps. Reply with your best final answer now."))
    completion = await provider.complete(messages)
    db.add_usage(run_id, completion.usage.input_tokens, completion.usage.output_tokens)
    messages.append(completion.as_message())
    return LoopResult(completion.text, max_steps, stopped_early=True)


def _unanswered_calls(messages: list[Message]) -> list[ToolCall]:
    if not messages or messages[-1].role not in ("assistant", "tool"):
        return []
    # Walk back to the last assistant message and collect calls with no matching tool result.
    answered: set[str] = set()
    for m in reversed(messages):
        if m.role == "tool":
            answered.add(m.tool_call_id or "")
        elif m.role == "assistant":
            return [tc for tc in m.tool_calls if tc.id not in answered]
        else:
            break
    return []


async def _handle_calls(
    calls: list[ToolCall],
    messages: list[Message],
    tools: dict[str, Tool],
    ctx: ToolContext,
    emitter: Emitter,
    human: Human,
    db: DB,
) -> None:
    for call in calls:
        emitter.emit("tool_called", call_id=call.id, name=call.name, arguments=call.arguments)
        output, failed = await _execute(call, tools, ctx, emitter, human)
        tool = tools.get(call.name)
        if tool and tool.generated and tool.version is not None:
            db.record_tool_call(tool.name, tool.version, failed)
        output = truncate(output, settings.tool_result_max_chars)
        emitter.emit("tool_result", call_id=call.id, name=call.name, output=output, error=failed)
        messages.append(Message.tool(call.id, output))


async def _execute(
    call: ToolCall, tools: dict[str, Tool], ctx: ToolContext, emitter: Emitter, human: Human
) -> tuple[str, bool]:
    """Returns (output, failed). Never raises for tool errors - the model should see them."""
    tool = tools.get(call.name)
    if tool is None:
        return f"Error: no tool named {call.name!r}. Available: {', '.join(tools)}", True

    decision, reason = classify(tool, call.arguments)
    if decision == DENY:
        return f"Refused by policy: {reason}", True
    if decision != ALLOW:
        key = f"tool:{ctx.step_id}:{call.id}"
        verdict = await human.decide(
            TOOL_CALL,
            key,
            {"name": call.name, "arguments": call.arguments, "reason": reason, "step_id": ctx.step_id},
        )
        emitter.emit("approval_resolved", key=key, name=call.name, approved=bool(verdict.get("approved")))
        if not verdict.get("approved"):
            why = verdict.get("reason") or "no reason given"
            return f"The user declined this action ({why}). Do not retry it; adapt or explain.", True

    try:
        result = await tool.fn(ctx, **call.arguments)
        return _as_text(result), False
    except TypeError as e:  # wrong/missing arguments
        return f"Error: bad arguments for {call.name}: {e}", True
    except Exception as e:
        return f"Error: {call.name} raised {type(e).__name__}: {e}", True


def _as_text(result: object) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, default=str)
