# Decisions

Short records of the choices that shaped this project, in the style of ADRs. Each one says what
was decided, what it was weighed against, and what it cost.

## 1. Build the runtime, don't adopt a framework

**Decision:** no LangGraph / LangChain / CrewAI. The loop, planner, checkpointing and
interrupt/resume are ~600 lines of our own code.

**Why:** the point of the project is to understand and be able to explain every part of an agent
runtime. Frameworks hide exactly the parts that matter (how state is persisted, what a resume
really does, where a tool result goes). The concepts map one-to-one onto what LangGraph calls a
checkpointer and `interrupt()`, so the knowledge transfers.

**Cost:** more code to test. Mitigated by `FakeProvider`, which lets the entire runtime be
exercised without a network.

## 2. One OpenAI-compatible adapter, models as configuration

**Decision:** `llm.py` has one adapter that talks to any `chat.completions` host (Groq, Ollama,
OpenRouter, OpenAI) plus an optional Anthropic adapter. The model for each role is an `.env`
string like `groq:openai/gpt-oss-120b`.

**Why:** the budget was "Groq free tier", but free-tier catalogues churn (the first live run
404'd on a model that had been retired), and the planner and toolsmith benefit from a stronger
model than the workers. Provider-per-file adapters (v1 had four) were where v1's schema bugs
lived.

**Cost:** JSON mode and tool-call semantics differ slightly per host; the adapter normalises what
it can and the loop treats a malformed tool call as a recoverable error.

## 3. SQLite via the standard library

**Decision:** `sqlite3`, WAL mode, plain SQL, numbered `.sql` migrations. No ORM.

**Why:** zero infrastructure for a project that has to run on a laptop and in CI; the data model
is seven tables; and every query in the project fits on one line. Postgres would be a one-file
change (`db.py`) if it were ever needed.

**Cost:** one writer at a time. Fine for a single-process server; not a multi-node design.

## 4. Event sourcing for runs

**Decision:** everything a run does is an append-only event; the CLI, API, replay and evals all
consume the same stream. A separate `state_json` checkpoint on the run row is what resume reads.

**Why:** one source of truth for "what happened" removes a whole category of bugs (the UI shows
one thing, the DB another). It also gives free observability: `sea runs <id>` is a full replay,
and the SSE endpoint is just a poll of the events table.

**Cost:** two representations of the run (events + checkpoint). Accepted because replaying events
to rebuild in-flight LLM message lists would be more code and more fragile.

## 5. Agent-written code never runs on the host

**Decision:** tests *and* calls of generated tools, plus `run_shell`, execute in a Docker
container: no network by default, memory/CPU/pid limits, non-root, workspace mounted. `SANDBOX=local`
exists for machines without Docker and is documented as unsafe.

**Why:** v1 only sandboxed the tests and ran the tools in-process with `exec`. That is the gap its
own failure gallery called out. Static validation (forbidden imports, no `eval`) is defence in
depth, not the defence.

**Cost:** ~0.5 s per tool call for `docker run`. A warm container with `docker exec` would remove
most of it and is the obvious next optimisation.

## 6. A single `Human` interface with pre-filled decisions

**Decision:** every human decision (approve a plan, allow a tool call, answer a question) goes
through `human.decide(kind, key, payload)`. The detached implementation records the request and
raises; resume re-enters the same code with the decision pre-filled under the same key.

**Why:** it makes the interactive CLI, the detached API and the evals identical from the
runtime's point of view, and it makes resume a re-run rather than a special path. Keys are
derived from stable ids (step id + tool call id), so a parallel sibling that pauses on the same
key reuses the pending approval.

**Cost:** the run is re-entered from the checkpoint rather than continued in memory. Cheap here
because the checkpoint *is* the in-memory state.

## 7. Planner declares gaps; ToolSmith fills them before workers start

**Decision:** the planner tags each step with existing tools and `missing_tools`. Missing tools
are built (and tested) before any worker runs. Workers can still call `build_tool` / `fix_tool`
mid-step for gaps the planner missed.

**Why:** building tools up-front means a plan is either fully equipped or the user hears about a
failed build before work starts, and it keeps expensive synthesis out of the parallel section.

**Cost:** the planner writes capability descriptions before anyone has looked at the data, so it
can bake wrong assumptions into a "verified" tool (failure gallery #1). The mitigations are prompt
rules ("general tools, layout-dependent things are parameters") and the worker's `fix_tool`.

## 8. Tools should be general, and fetching is separate from computing

**Decision:** the planner and toolsmith are told to ask for `csv_group_sum(csv_text, group_col,
value_col)` rather than `compute_sales_stats(csv)`, and tools take data rather than URLs.

**Why:** a general tool is reusable across tasks (the registry is meant to grow into a library,
not a junk drawer), and a pure tool can be tested offline in a network-less sandbox. The
built-in `http_get` handles fetching.

## 9. Keep the free tier in mind everywhere

**Decision:** a global cap on concurrent LLM calls, a compact tool catalogue in prompts, tool
results capped in size, at most two parallel steps, and the cheapest reasonable model per role.

**Why:** Groq's free tier is rate-limited per minute; an agent that fans out aggressively just
spends its time in `retry-after`. Tokens per run are recorded on the run row so cost is visible.
