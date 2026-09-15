import { describe, expect, it } from "vitest";
import type { Event } from "./api";
import { apply, emptyView, fold } from "./runView";

const ev = (type: string, payload: Record<string, any> = {}, seq = 0): Event => ({ seq, type, payload });
const plan = { steps: [{ id: "s1", description: "do it", depends_on: [], tools: [], missing_tools: [] }] };

describe("fold", () => {
  it("follows a run that builds a tool, pauses for approval, resumes and completes", () => {
    const events: Event[] = [
      ev("run_started", { task: "count words" }, 1),
      ev("llm_called", { model: "m", tokens_in: 10, tokens_out: 5, tool_calls: [], role: "planner" }, 2),
      ev("plan_proposed", { plan }, 3),
      ev("approval_requested", { approval_id: "a1", kind: "plan", key: "plan:0", plan }, 4),
      ev("run_paused", { approval_id: "a1", kind: "plan", run_id: "r" }, 5),
    ];
    let v = fold("r", "count words", events);
    expect(v.status).toBe("awaiting_plan_approval");
    expect(v.pending?.approvalId).toBe("a1");
    expect(v.planStatus).toBe("proposed");
    expect(v.lastSeq).toBe(5);

    const more: Event[] = [
      ev("run_resumed", { approval_id: "a1", kind: "plan" }, 6),
      ev("plan_approved", { plan, edited: false }, 7),
      ev("tool_gap_found", { name: "?", description: "word_count: counts words", mode: "write" }, 8),
      ev("tool_synthesized", { name: "word_count", attempt: 1 }, 9),
      ev("tool_test_result", { name: "word_count", attempt: 1, passed: false, output: "AssertionError" }, 10),
      ev("tool_synthesized", { name: "word_count", attempt: 2 }, 11),
      ev("tool_test_result", { name: "word_count", attempt: 2, passed: true, output: "" }, 12),
      ev("tool_registered", { name: "word_count", version: 1, description: "Counts words." }, 13),
      ev("step_started", { step_id: "s1", description: "do it" }, 14),
      ev("llm_called", { step_id: "s1", model: "m", tokens_in: 100, tokens_out: 20, tool_calls: ["word_count"] }, 15),
      ev("tool_called", { step_id: "s1", call_id: "c1", name: "word_count", arguments: { text: "a b" } }, 16),
      ev("tool_result", { step_id: "s1", call_id: "c1", name: "word_count", output: "2", error: false }, 17),
      ev("step_completed", { step_id: "s1", result: "2 words", llm_steps: 1 }, 18),
      ev("run_completed", { answer: "2 words" }, 19),
    ];
    v = more.reduce(apply, v);
    expect(v.status).toBe("completed");
    expect(v.pending).toBeUndefined();
    expect(v.planStatus).toBe("approved");
    expect(v.toolBuilds).toHaveLength(1);
    expect(v.toolBuilds[0].name).toBe("word_count");
    expect(v.toolBuilds[0].attempts.map((a) => a.passed)).toEqual([false, true]);
    expect(v.toolBuilds[0].registered?.version).toBe(1);
    expect(v.stepOrder).toEqual(["s1"]);
    expect(v.steps.s1.calls[0]).toMatchObject({ name: "word_count", output: "2", pending: false, error: false });
    expect(v.steps.s1.status).toBe("done");
    expect(v.answer).toBe("2 words");
    expect(v.tokensIn).toBe(110);
    expect(v.llmCalls).toBe(2);
    expect(v.steps.s1.llmCalls).toBe(1);
  });

  it("tracks a tool-call approval inside a step, a denial, and a cancelled run", () => {
    const events: Event[] = [
      ev("run_started", { task: "t" }, 1),
      ev("step_started", { step_id: "s1", description: "write" }, 2),
      ev("tool_called", { step_id: "s1", call_id: "w1", name: "write_file", arguments: { path: "a" } }, 3),
      ev("approval_requested", { approval_id: "a2", kind: "tool_call", key: "tool:s1:w1", name: "write_file", arguments: { path: "a" }, step_id: "s1" }, 4),
      ev("run_paused", { approval_id: "a2", kind: "tool_call", run_id: "r" }, 5),
    ];
    let v = fold("r", "t", events);
    expect(v.status).toBe("awaiting_approval");
    expect(v.pending?.kind).toBe("tool_call");
    expect(v.steps.s1.calls[0].pending).toBe(true);

    v = [
      ev("run_resumed", { approval_id: "a2", kind: "tool_call" }, 6),
      ev("tool_called", { step_id: "s1", call_id: "w1", name: "write_file", arguments: { path: "a" } }, 6),
      ev("approval_resolved", { step_id: "s1", key: "tool:s1:w1", name: "write_file", approved: false }, 7),
      ev("tool_result", { step_id: "s1", call_id: "w1", name: "write_file", output: "The user declined", error: true }, 8),
      ev("run_failed", { error: "cancelled by user" }, 9),
    ].reduce(apply, v);
    expect(v.pending).toBeUndefined();
    expect(v.steps.s1.calls).toHaveLength(1); // re-announced after resume, not duplicated
    expect(v.steps.s1.calls[0]).toMatchObject({ error: true, pending: false });
    expect(v.status).toBe("cancelled");
  });

  it("handles blocked steps, failed builds and plan rejection", () => {
    const v = fold("r", "t", [
      ev("run_started", { task: "t" }, 1),
      ev("plan_proposed", { plan }, 2),
      ev("plan_rejected", { reason: "no", replans: 1 }, 3),
      ev("plan_proposed", { plan }, 4),
      ev("plan_approved", { plan, edited: true }, 5),
      ev("tool_gap_found", { name: "x", description: "x: thing", mode: "write" }, 6),
      ev("tool_build_failed", { name: "x", error: "validation: bad", attempts: 3 }, 7),
      ev("step_started", { step_id: "s1", description: "a" }, 8),
      ev("step_failed", { step_id: "s1", error: "boom" }, 9),
      ev("step_blocked", { step_id: "s2", description: "b" }, 10),
      ev("run_failed", { error: "every step failed" }, 11),
    ]);
    expect(v.planRejections).toEqual([{ reason: "no", replans: 1 }]);
    expect(v.planStatus).toBe("approved");
    expect(v.toolBuilds[0].failed).toBe("validation: bad");
    expect(v.steps.s1.status).toBe("failed");
    expect(v.steps.s2.status).toBe("blocked");
    expect(v.stepOrder).toEqual(["s1", "s2"]);
    expect(v.status).toBe("failed");
  });

  it("does not mutate the previous view", () => {
    const a = emptyView("r", "t");
    const b = apply(a, ev("step_started", { step_id: "s1", description: "d" }, 1));
    expect(a.stepOrder).toEqual([]);
    expect(b.stepOrder).toEqual(["s1"]);
  });
});
