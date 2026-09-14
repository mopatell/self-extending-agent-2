"""What a tool looks like to the runtime, whether built-in or agent-written."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sea.llm import ToolSpec

if TYPE_CHECKING:
    from sea.interrupts import Human
    from sea.sandbox.runner import Sandbox


@dataclass
class ToolContext:
    """Everything a tool function may need besides its own arguments."""

    workspace: Path
    sandbox: Sandbox
    human: Human
    step_id: str | None = None
    tools: dict[str, Tool] | None = None  # the live tool set; build_tool adds to it
    toolsmith: ToolSmithFn | None = None  # set by the orchestrator


# async toolsmith(capability=..., edit_name=..., problem=...) -> message for the model
ToolSmithFn = Callable[..., Awaitable[str]]


ToolFn = Callable[..., Awaitable[str]]


@dataclass
class Tool:
    spec: ToolSpec
    fn: ToolFn  # async fn(ctx: ToolContext, **arguments) -> str
    risk: str = "safe"  # safe | approval
    version: int | None = None  # set for agent-written tools
    generated: bool = False

    @property
    def name(self) -> str:
        return self.spec.name

    def catalog_line(self) -> str:
        """One line for the planner's compact tool list."""
        tag = f" (v{self.version})" if self.generated else ""
        return f"- {self.name}{tag}: {self.spec.description}"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def json_default(o: Any) -> str:
    return str(o)
