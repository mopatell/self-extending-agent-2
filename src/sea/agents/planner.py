"""Planner: turns a task into a small list of steps, each tagged with the tools it needs."""

from __future__ import annotations

import re
from graphlib import CycleError, TopologicalSorter
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from sea.db import DB
from sea.events import Emitter
from sea.llm import Message, Provider


class MissingTool(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")
    description: str = Field(min_length=10)


class Step(BaseModel):
    id: str = Field(pattern=r"^s\d+$")
    description: str = Field(min_length=1)
    depends_on: list[str] = []
    tools: list[str] = []  # existing tools this step will use
    missing_tools: list[MissingTool] = []  # tools that must be built first


class Plan(BaseModel):
    steps: list[Step] = Field(min_length=1, max_length=8)

    def waves(self) -> list[list[Step]]:
        """Groups steps so every step's dependencies are in an earlier wave."""
        by_id = {s.id: s for s in self.steps}
        ts = TopologicalSorter({s.id: set(s.depends_on) for s in self.steps})
        ts.prepare()
        waves: list[list[Step]] = []
        while ts.is_active():
            ready = list(ts.get_ready())
            waves.append([by_id[i] for i in ready])
            ts.done(*ready)
        return waves

    def check(self) -> list[str]:
        ids = [s.id for s in self.steps]
        problems = []
        if len(ids) != len(set(ids)):
            problems.append("step ids must be unique")
        for s in self.steps:
            for d in s.depends_on:
                if d not in ids:
                    problems.append(f"step {s.id} depends on unknown step {d}")
                if d == s.id:
                    problems.append(f"step {s.id} depends on itself")
        if not problems:
            try:
                self.waves()
            except CycleError:
                problems.append("dependencies form a cycle")
        return problems


SYSTEM = """You plan how an AI agent will complete a task. Reply with ONE JSON object:

{{"steps": [{{"id": "s1", "description": "...", "depends_on": [], "tools": ["tool_name"],
             "missing_tools": [{{"name": "snake_case", "description": "what it takes and returns"}}]}}]}}

Guidelines:
- Fewest steps possible. A simple task is ONE step. Never split read -> compute -> write into separate
  steps; one worker does all of that. Split when the task lists independent parts (each becomes its own
  step and they run in parallel) or when a later part needs an earlier result.
- Each step is a self-contained instruction a worker can execute with tools; include concrete values.
- `tools`: names from the list below that the step will use.
- `missing_tools`: only for a pure computation or parsing no listed tool can do (e.g. parse a PDF, compute
  a statistic, transform data). Tools take a FILE PATH for anything bigger than a sentence (http_get saves
  downloads to a file in the workspace and returns the path), never URLs and never large text. Do not
  request tools for things a shell command does easily.
- Ask for GENERAL tools, not one-off ones: "csv_group_sum(csv_text, group_col, value_col)" beats
  "compute_sales_stats(csv)". Anything that depends on the data's layout (column names, key names, date
  format) must be a parameter, because the tool is written before anyone has looked at the data.
- Discover, don't ask: when something about the data is unknown (column names, file layout), the step should
  read the file first and then act. Only plan a question to the user when the answer cannot be found.
- If the task is impossible for software (time travel, physical actions, reading minds), unsafe, or
  destructive, do NOT invent a tool for it: return one step that tells the user plainly why it can't be done.

Available tools:
{catalog}
"""

RETRY = "That plan was invalid: {error}\nReturn a corrected JSON object."


async def make_plan(
    *,
    provider: Provider,
    task: str,
    catalog: str,
    emitter: Emitter,
    db: DB,
    run_id: str,
    feedback: str | None = None,
    history: str | None = None,
) -> Plan:
    user = f"Task: {task}"
    if history:
        user = f"Earlier in this conversation:\n{history}\n\n{user}"
    if feedback:
        user += f"\n\nThe user rejected the previous plan and said: {feedback}"
    messages = [Message.system(SYSTEM.format(catalog=catalog)), Message.user(user)]

    error = ""
    for _attempt in range(2):
        completion = await provider.complete(messages, json_mode=True)
        db.add_usage(run_id, completion.usage.input_tokens, completion.usage.output_tokens)
        emitter.emit(
            "llm_called",
            model=provider.model,
            tokens_in=completion.usage.input_tokens,
            tokens_out=completion.usage.output_tokens,
            tool_calls=[],
            role="planner",
        )
        messages.append(completion.as_message())
        try:
            plan = parse_plan(completion.text)
        except ValueError as e:
            error = str(e)
            messages.append(Message.user(RETRY.format(error=error)))
            continue
        return plan

    # Planner could not produce a valid plan: fall back to a single step so the run still proceeds.
    return Plan(steps=[Step(id="s1", description=task)])


def parse_plan(text: str) -> Plan:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in response")
    try:
        plan = Plan.model_validate_json(match.group())
    except ValidationError as e:
        raise ValueError(
            "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        ) from e
    problems = plan.check()
    if problems:
        raise ValueError("; ".join(problems))
    return plan


def plan_from_dict(d: dict[str, Any]) -> Plan:
    plan = Plan.model_validate(d)
    problems = plan.check()
    if problems:
        raise ValueError("; ".join(problems))
    return plan
