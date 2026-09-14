# sea — a self-extending agent

An AI agent that **builds the tools it is missing**. Give it a task; it plans the steps, checks
which tools it already has, writes and tests the ones it doesn't (in a Docker sandbox), runs the
steps — in parallel where they're independent — and asks a human before doing anything risky.
Everything it does is saved in SQLite, so a run can be paused, resumed and replayed.

```
$ sea run "Is 104729 a prime number?"
▶ task: Is 104729 a prime number?
s1 ● step: Check whether 104729 is prime
s1 ⚙ run_shell("python3 -c 'import sympy…'")      ← runs in the sandbox; sympy isn't there
s1   ↳ ModuleNotFoundError: No module named 'sympy'
s1 ⚙ build_tool("check if an integer is prime …")
⚒ missing tool: is_prime
  tests passed (attempt 1)
✔ tool registered: is_prime v1
s1 ⚙ is_prime({"n": 104729})
s1   ↳ true
╭─ Answer ─────────────────────────────────────────╮
│ 104,729 is a prime number.                       │
╰──────────────────────────────────────────────────╯
```

The next time any task needs a primality check, `is_prime` is already there.

## How it works

```
  task ──► PLANNER ──► human approves plan ──► TOOLSMITH builds missing tools ──► WORKERS run
   │        (LLM)      (edit / reject → replan)   (write · test in Docker · register)   steps in waves
   │                                                                                       │
   │                                     risky tool call? ──► human allows / denies ◄──────┘
   ▼
 SQLite: conversations · runs · events · approvals · tools (versioned)
```

Three LLM roles share one agent loop and differ only in prompt, tools and model:

| Role | Job | Default model |
|---|---|---|
| Planner | task → steps with dependencies; which tools exist, which are missing | `groq:openai/gpt-oss-120b` |
| ToolSmith | write a tool + tests, fix until the tests pass in the sandbox, register it | `groq:openai/gpt-oss-120b` |
| Worker | complete one step with tools; can build/fix tools itself if the plan missed a gap | `groq:openai/gpt-oss-20b` |

Human-in-the-loop gates: **plan approval**, **risky actions** (writing files, shell, network),
and **clarifying questions**. A pause is a row in the `approvals` table; `sea resume` or
`POST /runs/{id}/resume` continues the run exactly where it stopped.

Full walkthrough: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Why it's built this way:
[docs/DECISIONS.md](docs/DECISIONS.md). What went wrong along the way:
[docs/FAILURE_GALLERY.md](docs/FAILURE_GALLERY.md).

## Quickstart

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker, and an API key for any
OpenAI-compatible host (Groq's free tier is the default).

```bash
git clone https://github.com/mopatell/self-extending-agent-2 && cd self-extending-agent-2
uv sync
cp .env.example .env          # add GROQ_API_KEY (or point the *_MODEL vars elsewhere)
make sandbox                  # builds the Docker image agent-written tools run in

sea run "Build a tool that reverses a string, then reverse 'agent'"
sea chat                      # multi-turn; earlier answers are remembered
sea tools                     # what the agent has built so far
sea runs <run_id>             # replay a run event by event
sea serve                     # HTTP API on :8000 (SSE event stream, resume, tools)
```

Models are configuration, not code — any `provider:model` string works:

```bash
WORKER_MODEL=ollama:qwen2.5-coder:14b sea run "…"      # local
PLANNER_MODEL=anthropic:claude-opus-5 sea run "…"      # uv sync --extra anthropic
```

## What makes it production-shaped

- **Generated code never runs on the host.** Tests *and* calls execute in a locked-down
  container (no network, non-root, memory/pid limits). `run_shell` too.
- **Tools are versioned rows in SQL** with provenance, tests, call and failure counts. Editing a
  tool re-runs the previous version's tests. Name collisions are rejected.
- **Static validation before any test runs:** JSON-Schema validity, function-signature-matches-
  schema, dependency allowlist, forbidden imports (`subprocess`, `socket`, …), forbidden calls
  (`eval`, `os.system`, …), network use must be declared.
- **Everything is an event.** CLI rendering, the SSE API, `sea runs` replay and the evals all read
  the same append-only stream.
- **Pause / resume is one code path.** A human decision is keyed by stable ids; resuming re-enters
  the run with the decision pre-filled. Works for plan approval, tool approval and questions,
  including inside a parallel wave.
- **Failure isolation:** a failed step blocks only the steps that depend on it; a failed tool
  build is reported to the worker rather than killing the run; a malformed tool call from the
  model is fed back as a hint.
- **Testable without a network.** `FakeProvider` scripts model replies, so the loop, planner,
  toolsmith, orchestrator, CLI and API are all covered by fast unit tests (`make test`, ~2 s).
- **Free-tier aware:** capped concurrent LLM calls, compact tool catalogue in prompts, capped
  tool-result size, tokens recorded per run.

## Evals

`make eval` runs 19 tasks against real models and the real sandbox — v1's benchmark
(use existing tools / build new / fix buggy / refuse the impossible) plus multi-step, parallel,
human-in-the-loop and allowlisted-package tasks. Checks look at events, files and registry rows,
not just the answer text. Latest results: [evals/results/latest.md](evals/results/latest.md).

## Layout

```
src/sea/
  config.py  llm.py  db.py  events.py  loop.py  policy.py  interrupts.py  api.py  cli.py
  agents/     planner.py  toolsmith.py  worker.py  orchestrator.py
  tools/      base.py  builtin.py  registry.py
  sandbox/    Dockerfile  allowlist.py  runner.py
  migrations/ 001_init.sql
tests/        unit + integration (FakeProvider, in-memory SQLite); tests marked `docker` need the image
evals/        tasks.py  run_evals.py  results/
docs/         ARCHITECTURE.md  DECISIONS.md  FAILURE_GALLERY.md
```

Dependencies: `openai` (one adapter for every OpenAI-compatible host), `pydantic`, `fastapi`,
`uvicorn`, `jsonschema`, `rich`, `python-dotenv`. Nothing else.

## Lineage

This is a rebuild of [self-extending-agent](https://github.com/mopatell/self-extending-agent)
(v1), which proved the core idea — write a missing tool, test it, use it — and documented what a
real version would need: sandboxed *execution* (not just testing), persistence, human approval,
multi-agent planning, resumability. v2 is those things.
