# Failure gallery

Things that went wrong while building and running this agent, and what was done about them.
Collected as they happened, not staged. v1's entries are at the bottom.

## v2

### 1. Tool tested against its own misreading of the spec
**Task:** "find which month had the biggest month-over-month drop in total revenue" on a small CSV.
**What happened:** the planner described the missing tool as computing the biggest drop "across any
region"; the ToolSmith implemented exactly that, wrote tests for it, and they passed. The tool returned
$800 (south: 1500 → 700) where the task wanted $600 (total: 2400 → 1800). The answer-synthesis step
noticed the discrepancy; the report file did not.
**Lesson:** sandbox tests prove the code matches the *spec*, not that the spec matches the *task*.
Ambiguity in a capability description flows straight into a "verified" tool.
**Status:** open. Candidate fixes: have the worker (which sees the data) write the capability spec instead
of the planner; or a verifier that checks step output against the task, not the tool.

### 2. Planner asked the user for something it could have looked up
**What happened:** after being told to build general tools with column-name parameters, the planner's
first step became "ask the user which columns in sales.csv correspond to region, date, revenue". The
worker called `ask_human` and the run paused - correct behaviour, wrong plan.
**Fix:** planner rule "discover, don't ask": read the file first; ask only when the answer can't be found.

### 3. Workers doing other workers' steps
**What happened:** step s1 was "read sales.csv"; the worker read it, computed the stats, and wrote the
report - then s2 and s3 redid the same work, re-reading and re-writing report.md several times. 23k
tokens for a 6-row CSV.
**Fix:** worker rule "do only your step"; planner rule "never split read → compute → write".

### 4. Provider rejected a tool call the model produced
**What happened:** gpt-oss-20b emitted a `run_shell` call whose arguments contained a multi-line Python
heredoc; Groq returned HTTP 400 `tool_use_failed` and the step crashed.
**Fix:** the provider raises `MalformedToolCall`; the loop tells the model its call was dropped and to
use short arguments (write long scripts to a file first). Regression test: `test_malformed_tool_call_is_fed_back`.

### 5. Tool output that looked wrong but wasn't
**What happened:** the CLI printed `{"region_totals": {}, "worst_drop": }`. The tool had actually returned
`[null, 0.0]` - Rich interpreted `[null, 0.0]` as a markup tag and swallowed it.
**Fix:** escape all dynamic text in the CLI.

### 6. Interactive prompt with no terminal
**What happened:** a scripted run hit `ask_human`; `ConsoleHuman` blocked on stdin forever.
**Fix:** when stdin is not a TTY the run pauses (detached mode) instead of prompting.

### 7. Stale model name
**What happened:** `llama-3.3-70b-versatile` no longer exists on Groq; the first live run 404'd.
**Fix:** models are config, not code; defaults now point at `openai/gpt-oss-*`. Worth remembering that
free-tier model catalogues churn.

## v1 (carried over)

1. **Duplicate tools instead of editing** - asked to fix a buggy tool, the agent wrote five near-duplicates.
   v2: name collisions are rejected at registration; `fix_tool` is a first-class action.
2. **Shortcut answering** - the agent computed the answer itself instead of using/fixing the tool. v2: worker
   prompt forbids it; evals check events, not just answer text.
3. **Malformed schemas registered** - `required` nested in `properties`, invalid constraints, schema/signature
   mismatch. v2: all three are regression-tested in `test_validation_catches`.
4. **Impossible capability** ("time_travel tool") - correctly refused.
5. **Destructive request** ("delete all files") - refused by model judgement only; no hard guardrail. v2:
   policy deny-list plus Docker sandbox for all generated code and shell commands.
6. **Underspecified request** ("make it better") - asked for clarification. v2: `ask_human` pauses the run.
