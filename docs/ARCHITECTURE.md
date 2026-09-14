# Architecture

`sea` (self-extending agent) takes a task, plans it, builds any tools the plan needs, runs the
steps - in parallel where possible - with a human approving anything risky, and remembers
everything in SQLite. This document explains the moving parts and why they fit together the way
they do. For the *reasoning* behind the bigger choices see [DECISIONS.md](DECISIONS.md).

## One picture

```
                         ┌──────────────────────────────────────────────┐
  user task ──────────►  │                ORCHESTRATOR                   │
  (CLI / API)            │  runs the phases below, checkpoints to SQLite │
                         └──────┬───────────────────────────────────────┘
                                │
                 1. plan        ▼
                         ┌─────────────┐    "s1 needs read_file (have it)
                         │   PLANNER   │───► s2 needs csv_group_sum (missing)
                         │   (LLM)     │     s3 depends on s1 and s2"
                         └─────────────┘
                                │
                 2. approve     ▼
                         ┌─────────────┐
                         │   HUMAN     │  ✔ approve / ✎ edit a step / ✘ reject with reason → replan
                         └─────────────┘
                                │
                 3. build       ▼          for every missing tool:
                  missing  ┌─────────────┐   write code + tests ──► run tests in the
                  tools    │  TOOLSMITH  │   DOCKER SANDBOX ──► pass? register (versioned)
                           │   (LLM)     │   fail? feed the error back, retry (max 3)
                           └─────────────┘
                                │
                 4. run waves   ▼          steps with no unmet dependencies run together
                    ┌────────────┬────────────┐
                    │  WORKER    │  WORKER    │   each worker = the agent loop with a
                    │   (LLM)    │   (LLM)    │   system prompt, a tool set and a model
                    └─────┬──────┴─────┬──────┘
                          │            │
                          ▼            ▼
                    ┌─────────────────────────┐
                    │  TOOLS                  │  built-in: read/write/list files, run_shell,
                    │  built-in + generated   │  http_get, ask_human, build_tool, fix_tool
                    └───────────┬─────────────┘  generated: executed inside the sandbox
                                │
                       policy: allow / ask a human / deny
                                │
                                ▼
                         ┌─────────────┐
                         │   HUMAN     │  ✔ allow / ✘ deny   (run pauses; resume any time)
                         └─────────────┘
                                │
                 5. answer      ▼
                          final answer (one step: its result; several: a synthesis call)

  ┌────────────────────────────────────────────────────────────────────┐
  │ SQLITE  conversations · messages · runs · events · approvals ·      │
  │         tools(name, version).  Append-only events + a state         │
  │         checkpoint per run make pause / resume / replay possible.   │
  └────────────────────────────────────────────────────────────────────┘
```

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Every knob, from `.env`: model per role, limits, `AUTO_APPROVE`, paths |
| `llm.py` | Provider-neutral `Message` / `ToolCall` / `Completion`. One adapter for every OpenAI-compatible host (Groq, Ollama, OpenRouter, OpenAI), an optional Anthropic adapter, and `FakeProvider` for tests. A global semaphore caps concurrent LLM calls |
| `db.py` + `migrations/` | SQLite via the standard library. Plain SQL, numbered migrations, one small class |
| `events.py` | The event vocabulary and `Emitter` (persist + fan out to listeners) |
| `loop.py` | The agent loop: model → tool calls → results → repeat. Re-enterable after an interrupt |
| `policy.py` | Risk classification of a tool call: `allow`, `approval`, `deny` |
| `interrupts.py` | The `Human` interface and `DetachedHuman`, which pauses the run |
| `agents/planner.py` | Task → `Plan` (steps, dependencies, tools needed, tools missing) |
| `agents/toolsmith.py` | Capability → validated, sandbox-tested, registered tool (or a fix to an existing one) |
| `agents/worker.py` | One step, with tools |
| `agents/orchestrator.py` | The state machine that strings the above together; checkpointing; pause/resume |
| `tools/base.py`, `tools/builtin.py` | The `Tool` shape and the fixed toolbelt |
| `tools/registry.py` | Static validation of drafts; versioned storage; loading generated tools as sandbox-backed `Tool`s |
| `sandbox/` | `Dockerfile` (allowlisted packages), the runner, test and call drivers |
| `api.py` | FastAPI: runs as background tasks, SSE event streaming, resume |
| `cli.py` | `sea run / chat / resume / approvals / cancel / runs / tools / serve` |

## The agent loop

`loop.run_loop()` is the only place the model is asked to act. Every role - worker, and in the
future anything else - calls it with a system prompt, a tool dict and a provider:

```
loop:
  finish any tool calls left unanswered by a previous attempt   (resume support)
  completion = provider.complete(messages, tools)
  append assistant message
  if no tool calls: return text
  for each call:
      policy.classify(tool, args) → deny? return "Refused by policy"
                                  → approval? human.decide(...)  (may raise Interrupt)
      run the tool; errors become tool results the model can read
      append tool message
```

Things worth noticing:

- **Tool results are real `tool` messages** with the provider's call id, not text pasted into a
  user turn (v1 did that, and models handled it badly).
- **Errors are data.** A tool that throws, a missing tool, bad arguments, a declined approval - all
  come back as tool results so the model can adapt. Only `Interrupt` propagates.
- **Tool list is recomputed every turn**, so a tool built by `build_tool` mid-step is offered on the
  very next turn.
- **Malformed tool calls from the provider** (Groq returns HTTP 400 for these) are turned into a
  hint message and the loop continues.

## Human in the loop

There is one interface:

```python
class Human(Protocol):
    async def decide(self, kind, key, payload) -> dict   # kind: plan | tool_call | question
```

and three implementations: the CLI's `ConsoleHuman` (prompts in the terminal), `DetachedHuman`
(records a pending approval and raises `Interrupt`), and `AutoHuman` / `EvalHuman` for tests
and evals. The `key` is stable across attempts (`plan:0`, `tool:s2:<call_id>`, ...), which is what
makes resume work:

```
run → decide("tool_call", "tool:s2:abc", …) → Interrupt
    → orchestrator checkpoints state, run.status = awaiting_approval
    … later …
resume(run, approval, decision)
    → DetachedHuman(prefilled={"tool:s2:abc": decision})
    → same code path re-enters; the loop finds the unanswered call and continues
```

Parallel steps that pause on the same key reuse the pending approval row instead of duplicating
it. The planner's proposal is checkpointed *before* the plan gate so a resume never re-plans.

## Run state machine

```
pending → planning → awaiting_plan_approval ⇄ planning (rejected: replan with feedback, max 2)
        → building_tools → running ⇄ (awaiting_approval | awaiting_input)
        → completed | failed | cancelled
```

`runs.state_json` holds everything needed to continue: the plan, per-step status + messages,
tool-build outcomes, replan count. `runs.plan_json` holds the approved plan for display.

## Events

Every meaningful thing emits an event into the `events` table (`run_id, seq, type, payload`).
The CLI renders them live, `sea runs <id>` replays them, the API streams them over SSE, and the
evals assert on them. Types:

```
run_started run_completed run_failed run_paused run_resumed
plan_proposed plan_approved plan_rejected
tool_gap_found tool_synthesized tool_test_result tool_registered tool_build_failed
step_started step_completed step_failed step_blocked
llm_called tool_called tool_result
approval_requested approval_resolved question_asked question_answered
```

## Tool lifecycle

```
LLM draft (JSON) ─► ToolDraft (pydantic)
                 ─► validate():  JSON-Schema valid · function named `name` exists · parameters ==
                                 schema properties · required params declared · deps ⊆ allowlist ·
                                 no subprocess/socket/ctypes · no eval/exec/os.system ·
                                 network imports ⇒ network=true · has asserts
                 ─► sandbox test (docker run --network none …), previous version's tests too on edit
                 ─► register: tools(name, version+1, …); mirror to data/tools/<name>.py
```

At call time a generated tool runs in the sandbox: JSON arguments on stdin, JSON result after a
marker on stdout, workspace mounted at `/workspace`, network only if declared *and* approved.
Usage counters (`calls`, `failures`) are updated per call.

## Data model

```
conversations(id, title)
messages(conversation_id, role, content)                      -- user-visible chat history
runs(id, conversation_id, task, status, plan_json, state_json, answer, error, tokens_in, tokens_out)
events(run_id, seq, type, payload_json)                       -- append-only
approvals(id, run_id, kind, payload_json, status, decision_json)
tools(name, version, description, input_schema_json, source, test_code, deps_json, risk, status,
      created_by_run, calls, failures)                        -- PK (name, version)
```

## Testing strategy

- **Unit / integration (CI):** the whole runtime - loop, orchestrator, planner, toolsmith, CLI,
  API - runs against `FakeProvider` (scripted completions), `LocalSandbox` and an in-memory or
  temp SQLite. No network, no Docker, ~2 seconds.
- **Docker (`-m docker`):** the sandbox image, test/call drivers, network blocking, non-root user.
- **Evals (`make eval`):** 19 tasks against real models and the real sandbox; checks look at
  events, files and registry rows. Results are committed under `evals/results/`.
