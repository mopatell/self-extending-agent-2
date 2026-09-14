# sea — a self-extending agent

An agent that builds the tools it is missing. Give it a task; it plans the steps, writes and
tests any tool it lacks in a Docker sandbox, runs the steps (in parallel where possible), asks a
human before anything risky, and stores everything in SQLite so runs can be paused, resumed and
replayed.

Docs: [how it works](docs/ARCHITECTURE.md) · [why it's built this way](docs/DECISIONS.md) ·
[what went wrong along the way](docs/FAILURE_GALLERY.md) · [benchmark results](evals/results/latest.md)

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Docker (Docker Desktop is fine) — agent-written code and shell commands run in a container
- An API key for one OpenAI-compatible host. Default is Groq's free tier
  (`GROQ_API_KEY` from https://console.groq.com). Ollama works with no key.

## Setup

```bash
git clone https://github.com/mopatell/self-extending-agent-2 && cd self-extending-agent-2
uv sync                      # installs into .venv
cp .env.example .env         # then put your GROQ_API_KEY in .env
make sandbox                 # builds the `sea-sandbox` Docker image (~1 min, once)
```

Check it works:

```bash
uv run sea run "Reverse the string 'agent' and tell me the result"
```

## Running tasks

```bash
uv run sea run "Is 104729 a prime number?"                # one task, interactive approvals
uv run sea run -c myproject "..."                         # named conversation = its own workspace
uv run sea chat                                           # multi-turn; earlier answers remembered
AUTO_APPROVE=all uv run sea run "..."                     # no prompts (approves plans and tool calls)
```

Files the agent reads and writes live in `workspaces/<conversation-id>/`. Put input files there
(e.g. `workspaces/myproject/sales.csv`) and refer to them by name in the task.

What a run looks like:

```
▶ task: Is 104729 a prime number?
              Proposed plan
┃ id ┃ step                              ┃ after ┃ needs ┃
│ s1 │ Check whether 104729 is prime     │ -     │ + build: is_prime │
Plan: (a)pprove, (e)dit a step, (r)eject [a/e/r] (a):
⚒ missing tool: is_prime
  tests passed (attempt 1)
✔ tool registered: is_prime v1
s1 ● step: Check whether 104729 is prime
s1 ⚙ is_prime({"n": 104729})
s1   ↳ true
╭─ Answer ────────────────────────────╮
│ 104,729 is a prime number.          │
╰─────────────────────────────────────╯
run 51776e260e21 · 4088↑ 886↓ tokens
```

You will be asked to approve: the plan (always), and any tool call that writes files, runs a
shell command or uses the network. Pure/read-only tools and agent-written pure tools run without
asking. Destructive shell commands (`rm -rf`, `sudo`, …) are refused outright.

Other commands:

```bash
uv run sea tools                 # tools the agent has built (version, calls, failures)
uv run sea tools is_prime        # source and tests of one tool
uv run sea runs                  # recent runs
uv run sea runs <run_id>         # replay a run event by event
uv run sea approvals             # runs waiting on a human
uv run sea resume <run_id>       # answer what a paused run is waiting for
uv run sea cancel <run_id>
uv run sea run --detached "..."  # never prompt; pause instead (what the API does)
uv run sea serve                 # HTTP API on http://127.0.0.1:8000  (docs at /docs)
```

API in three calls:

```bash
CID=$(curl -s -X POST localhost:8000/conversations | jq -r .id)
RID=$(curl -s -X POST localhost:8000/conversations/$CID/runs -H 'content-type: application/json' \
      -d '{"task":"Is 7919 prime?"}' | jq -r .run_id)
curl -N localhost:8000/runs/$RID/events            # SSE: replays, then follows the run until it ends or pauses
curl -s localhost:8000/runs/$RID | jq .pending_approvals
curl -X POST localhost:8000/runs/$RID/resume -H 'content-type: application/json' \
     -d '{"approval_id":"<id>","decision":{"approved":true}}'
```

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `PLANNER_MODEL`, `TOOLSMITH_MODEL` | `groq:openai/gpt-oss-120b` | `provider:model`; providers: `groq`, `ollama`, `openrouter`, `openai`, `anthropic` |
| `WORKER_MODEL` | `groq:openai/gpt-oss-20b` | cheaper model for step execution |
| `AUTO_APPROVE` | `none` | `safe` = auto-approve plans; `all` = plans and risky tool calls too |
| `MAX_CONCURRENT_LLM_CALLS` | `2` | keep low on free tiers |
| `MAX_PARALLEL_STEPS` | `2` | steps run at once inside a wave |
| `MAX_LOOP_STEPS` | `12` | tool-call turns per step before the worker is forced to answer |
| `SANDBOX` | `docker` | `local` runs agent code on the host — dev only, unsafe |
| `DB_PATH`, `WORKSPACES_DIR` | `data/sea.db`, `workspaces` | where things are stored |

Anthropic models need `uv sync --extra anthropic` and `ANTHROPIC_API_KEY`.

## Testing

```bash
make test          # 78 unit/integration tests, no network, no Docker  (~3 s)
make test-docker   # 4 tests against the sandbox image                (~5 s)
make lint
make eval          # the 19-task benchmark against real models        (~15 min, ~150k tokens)
```

`make test` covers the whole runtime — loop, planner, toolsmith, orchestrator, pause/resume,
CLI, API — using `FakeProvider` (scripted model replies) and a temp SQLite. If it passes, the
system is wired correctly; only model quality is untested.

`make eval` is the real thing. Expect **≈16–18 of 19** to pass with the default Groq models
(see [latest results](evals/results/latest.md)); the ones that fail are usually the model
choosing a one-step plan for a task that should be parallel, or a provider-side output error.
Each task gets a fresh DB and workspace; results are written to `evals/results/`. Groq's free
tier allows roughly one full run per day per model — if it stops with `rate_limit_exceeded`,
re-run the rest later with `uv run python -m evals.run_evals --merge <task ids>`.

## What to expect

- **Simple tasks**: 3–5k tokens, under 10 s.
- **Tasks that need a new tool**: 7–10k tokens, 30–60 s; the tool is built, tested and reused by
  later runs (`sea tools`).
- **Multi-step / pandas tasks**: 10–25k tokens, 1–2 min.
- Tool builds fail sometimes; the ToolSmith gets three attempts with the test output fed back,
  and a failed build is reported to the worker, which can retry or work around it.
- A run can end in `failed` when every step fails (e.g. the model exhausted its tool-call budget)
  — `sea runs <id>` shows exactly what happened.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Docker not found` / `Cannot connect to the Docker daemon` | start Docker Desktop, or `SANDBOX=local` for a quick look (unsafe) |
| `model ... does not exist` (404) | free-tier catalogues change; `curl https://api.groq.com/openai/v1/models` and update `.env` |
| `rate_limit_exceeded ... tokens per day` | that model's daily budget is spent; switch `WORKER_MODEL` (e.g. `groq:qwen/qwen3.8-27b`) or wait |
| run hangs at a prompt when scripted | pipes have no TTY, so use `--detached` (pauses) or `AUTO_APPROVE=all` |
| `GROQ_API_KEY is not set` | `.env` is read from the current directory; run from the repo root |

## Layout

```
src/sea/
  config.py  llm.py  db.py  events.py  loop.py  policy.py  interrupts.py  api.py  cli.py
  agents/     planner.py  toolsmith.py  worker.py  orchestrator.py
  tools/      base.py  builtin.py  registry.py
  sandbox/    Dockerfile  allowlist.py  runner.py
  migrations/ 001_init.sql
tests/   evals/   docs/   data/ (db + mirrored tool sources)   workspaces/ (per conversation)
```

Dependencies: `openai`, `pydantic`, `fastapi`, `uvicorn`, `jsonschema`, `rich`, `python-dotenv`.

Rebuilt from [self-extending-agent](https://github.com/mopatell/self-extending-agent) (v1), which
proved the idea and listed what a real version needed: sandboxed execution, persistence, human
approval, planning, resumability.
