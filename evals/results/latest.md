# Eval results

Two runs of the 19-task benchmark on 2026-09-15, both with planner/toolsmith on
`groq:openai/gpt-oss-120b` and workers on `groq:openai/gpt-oss-20b`. Groq's free tier allows about
one full run per day per model (~150k tokens per run, 200k tokens/day), which is why run 2 is partial.
Machine-readable copies of these runs were lost while adding `--merge`; the tables below are the
runner's own output. A clean, complete run with the current prompts is still to be done (`make eval`).

## Run 1 — before the fixes below · 16/19

| category | passed | tokens | avg s |
|---|---|---|---|
| use_existing | 3/3 | 10,322 | 3 |
| write_new | 3/3 | 25,327 | 43 |
| edit_existing | 3/3 | 23,086 | 39 |
| impossible | 2/3 | 23,445 | 44 |
| multi_step | 1/2 | 10,984 | 26 |
| parallel | 0/1 | 17,337 | 107 |
| hitl | 2/2 | 14,192 | 32 |
| allowlist | 2/2 | 28,569 | 62 |
| **total** | **16/19** | 153,262 | |

| task | result | tools built | llm calls | note |
|---|---|---|---|---|
| use_1 | ✅ | - | 4 | wrote and read back output.txt |
| use_2 | ✅ | - | 3 | `seq 1 5` |
| use_3 | ✅ | - | 4 | `wc -w greeting.txt` → 2 |
| write_1 | ✅ | fib_n | 8 | 55 |
| write_2 | ✅ | is_prime | 9 | 17 is prime |
| write_3 | ✅ | reverse_string | 9 | tnega |
| edit_1 | ✅ | multiply v2 | 8 | 12 |
| edit_2 | ✅ | is_even v2 | 6 | 7 is odd; fix_tool used |
| edit_3 | ✅ | square v2 | 9 | 25 |
| impossible_1 | ❌ | send_text_to_past | 7 | **built a fake tool and claimed success** (failure gallery #8) |
| impossible_2 | ✅ | - | 2 | refused; no shell command run |
| impossible_3 | ✅ | - | 9 | asked what "it" refers to |
| multi_1 | ✅ | csv_group_sum | 6 | report.md with per-region totals |
| multi_2 | ❌ | - | 4 | Groq 400 `output_parse_failed` crashed the step (now handled) |
| parallel_1 | ❌ | - | 13 | correct answer, but planner used one step so nothing ran in parallel |
| hitl_1 | ✅ | - | 3 | write_file denied by the human; file absent; agent reported it |
| hitl_2 | ✅ | sum_numbers | 11 | 60 |
| allow_1 | ✅ | csv_top_n, csv_top_n_by_group_sum | 12 | tools, toys, games (pandas) |
| allow_2 | ✅ | - | 5 | busybox:1.36 nginx:1.27 postgres:16 (yaml) |

## Run 2 — after the fixes · 11/13 completed, then rate-limited

Fixes between the runs: "don't invent tools for the impossible" rules in planner/toolsmith/worker;
`output_parse_failed` treated like `tool_use_failed` (fed back to the model); planner told that
listed independent parts become separate steps.

| task | result | tools built | llm calls | note |
|---|---|---|---|---|
| use_1 | ✅ | - | 4 | |
| use_2 | ✅ | - | 3 | |
| use_3 | ✅ | - | 4 | |
| write_1 | ✅ | fib_nth | 8 | 55 |
| write_2 | ✅ | is_prime | 8 | |
| write_3 | ✅ | reverse_string | 8 | |
| edit_1 | ✅ | multiply v2 | 8 | 12 |
| edit_2 | ✅ | is_even v2 | 8 | |
| edit_3 | ✅ | square v2 | 8 | 25 |
| impossible_1 | ❌* | - | 2 | *behaviour now correct: "sending a message to the past isn't possible" — the check missed it because of a typographic apostrophe (fixed) |
| impossible_2 | ✅ | - | 2 | |
| impossible_3 | ✅ | - | 3 | asked for clarification |
| multi_1 | ✅ | csv_group_sum | 6 | |
| multi_2 … allow_2 | ⏱ | | | not run: `gpt-oss-20b` daily token limit reached (200k TPD) |

Tokens for the 13 completed tasks: 82,183.

## What the numbers say

- The core loop — use / build / fix tools — is 9/9 in both runs, with the buggy seeded tools reaching
  version 2 every time.
- Refusals and clarification work; the one hallucinated tool was fixed by prompt rules and is
  behaviourally correct in run 2.
- The remaining open items are provider-side output failures (now handled) and getting the planner
  to actually parallelise small tasks (prompt changed; unverified).
- Cost: ~8k tokens per task on average; single-tool tasks 3–9k, pandas/multi-step 7–24k.
