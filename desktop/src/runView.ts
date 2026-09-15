/** Folds a run's event stream into what the page renders. Pure; no I/O. */

import type { Event, Plan } from "./api";

export type ToolCallRow = {
  callId: string;
  name: string;
  args: Record<string, any>;
  output?: string;
  error?: boolean;
  pending: boolean;
};

export type StepView = {
  id: string;
  description: string;
  status: "running" | "done" | "failed" | "blocked";
  calls: ToolCallRow[];
  result?: string;
  error?: string;
  llmCalls: number;
};

export type ToolBuild = {
  name: string;
  mode: "write" | "edit";
  description: string;
  attempts: { attempt: number; passed: boolean; output: string }[];
  registered?: { version: number; description: string };
  failed?: string;
};

export type Pending = {
  approvalId: string;
  kind: "plan" | "tool_call" | "question";
  key: string;
  payload: Record<string, any>;
};

export type RunView = {
  id: string;
  task: string;
  status: string;
  plan?: Plan;
  planStatus?: "proposed" | "approved";
  planRejections: { reason: string; replans: number }[];
  toolBuilds: ToolBuild[];
  steps: Record<string, StepView>;
  stepOrder: string[];
  pending?: Pending;
  answer?: string;
  error?: string;
  tokensIn: number;
  tokensOut: number;
  llmCalls: number;
  lastSeq: number;
};

export function emptyView(id: string, task: string, status = "pending"): RunView {
  return {
    id, task, status, planRejections: [], toolBuilds: [], steps: {}, stepOrder: [],
    tokensIn: 0, tokensOut: 0, llmCalls: 0, lastSeq: 0,
  };
}

const PAUSE_STATUS: Record<string, string> = {
  plan: "awaiting_plan_approval",
  tool_call: "awaiting_approval",
  question: "awaiting_input",
};

/** Returns a new view with the event applied (the input is not mutated). */
export function apply(view: RunView, e: Event): RunView {
  const v: RunView = { ...view, steps: { ...view.steps }, lastSeq: Math.max(view.lastSeq, e.seq) };
  const p = e.payload;
  const step = (id: string): StepView =>
    (v.steps[id] = { ...(v.steps[id] ?? { id, description: "", status: "running", calls: [], llmCalls: 0 }) });
  const build = () => {
    const last = v.toolBuilds[v.toolBuilds.length - 1];
    v.toolBuilds = [...v.toolBuilds.slice(0, -1), { ...last, attempts: [...last.attempts] }];
    return v.toolBuilds[v.toolBuilds.length - 1];
  };

  switch (e.type) {
    case "run_started":
      v.task = p.task;
      v.status = "running";
      break;
    case "plan_proposed":
      v.plan = p.plan;
      v.planStatus = "proposed";
      break;
    case "plan_approved":
      v.plan = p.plan;
      v.planStatus = "approved";
      break;
    case "plan_rejected":
      v.planRejections = [...v.planRejections, { reason: p.reason, replans: p.replans }];
      v.plan = undefined;
      v.planStatus = undefined;
      break;

    case "tool_gap_found":
      v.toolBuilds = [
        ...v.toolBuilds,
        { name: p.name === "?" ? "" : p.name, mode: p.mode ?? "write", description: p.description, attempts: [] },
      ];
      break;
    case "tool_synthesized": {
      const b = build();
      if (!b.name) b.name = p.name;
      break;
    }
    case "tool_test_result": {
      const b = build();
      b.attempts.push({ attempt: p.attempt, passed: !!p.passed, output: p.output ?? "" });
      break;
    }
    case "tool_registered": {
      const b = build();
      b.name = p.name;
      b.registered = { version: p.version, description: p.description };
      break;
    }
    case "tool_build_failed": {
      const b = build();
      b.failed = p.error;
      break;
    }

    case "step_started": {
      const s = step(p.step_id);
      s.description = p.description;
      s.status = "running";
      if (!v.stepOrder.includes(p.step_id)) v.stepOrder = [...v.stepOrder, p.step_id];
      break;
    }
    case "step_blocked": {
      const s = step(p.step_id);
      s.description = p.description;
      s.status = "blocked";
      if (!v.stepOrder.includes(p.step_id)) v.stepOrder = [...v.stepOrder, p.step_id];
      break;
    }
    case "step_completed": {
      const s = step(p.step_id);
      s.status = "done";
      s.result = p.result;
      break;
    }
    case "step_failed": {
      const s = step(p.step_id);
      s.status = "failed";
      s.error = p.error;
      break;
    }

    case "llm_called":
      v.llmCalls += 1;
      v.tokensIn += p.tokens_in ?? 0;
      v.tokensOut += p.tokens_out ?? 0;
      if (p.step_id) step(p.step_id).llmCalls += 1;
      break;
    case "tool_called": {
      const s = step(p.step_id);
      s.calls = [...s.calls, { callId: p.call_id, name: p.name, args: p.arguments ?? {}, pending: true }];
      break;
    }
    case "tool_result": {
      const s = step(p.step_id);
      s.calls = s.calls.map((c) =>
        c.callId === p.call_id ? { ...c, output: p.output, error: !!p.error, pending: false } : c,
      );
      break;
    }

    case "approval_requested":
    case "question_asked":
      v.pending = { approvalId: p.approval_id, kind: p.kind, key: p.key, payload: p };
      break;
    case "approval_resolved":
      if (!v.pending || v.pending.key === p.key) v.pending = undefined;
      break;
    case "run_paused":
      v.status = PAUSE_STATUS[p.kind] ?? "awaiting_approval";
      break;
    case "run_resumed":
      v.pending = undefined;
      v.status = "running";
      break;

    case "run_completed":
      v.answer = p.answer;
      v.status = "completed";
      v.pending = undefined;
      break;
    case "run_failed":
      v.error = p.error;
      v.status = p.error === "cancelled by user" ? "cancelled" : "failed";
      v.pending = undefined;
      break;
  }
  return v;
}

export function fold(id: string, task: string, events: Event[]): RunView {
  return events.reduce(apply, emptyView(id, task));
}
