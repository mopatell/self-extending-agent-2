/** Thin typed client for the sea backend. Every call is a plain fetch; runs are followed over SSE. */

export const API = "http://127.0.0.1:8765";

export type Page = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_task: string | null;
  last_status: string | null;
  run_count: number;
};

export type Step = {
  id: string;
  description: string;
  depends_on: string[];
  tools: string[];
  missing_tools: { name: string; description: string }[];
};
export type Plan = { steps: Step[] };

export type Approval = {
  id: string;
  run_id: string;
  kind: "plan" | "tool_call" | "question";
  status: string;
  payload: Record<string, any> & { key: string };
  task?: string;
  conversation_id?: string;
};

export type Run = {
  id: string;
  conversation_id: string;
  task: string;
  status: string;
  plan: Plan | null;
  answer: string | null;
  error: string | null;
  tokens_in: number;
  tokens_out: number;
  created_at: string;
  updated_at: string;
  pending_approvals?: Approval[];
};

export type Event = { seq: number; type: string; payload: Record<string, any> };

export type Tool = {
  name: string;
  version: number;
  description: string;
  input_schema: Record<string, any>;
  source?: string;
  test_code?: string;
  deps: string[];
  risk: string;
  status: string;
  created_by_run: string | null;
  calls: number;
  failures: number;
  created_at: string;
};

export type Setting = { key: string; kind: string; label: string; value: string };

export const TERMINAL = new Set(["completed", "failed", "cancelled"]);
export const WAITING = new Set(["awaiting_plan_approval", "awaiting_approval", "awaiting_input"]);

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(API + path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers || {}) },
  });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      /* not json */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return r.status === 204 ? (undefined as T) : r.json();
}

export const api = {
  health: () => req<{ ok: boolean; version: string; sandbox: string }>("/health"),

  pages: () => req<Page[]>("/conversations"),
  createPage: (title = "") =>
    req<{ id: string }>(`/conversations?title=${encodeURIComponent(title)}`, { method: "POST" }),
  renamePage: (id: string, title: string) =>
    req<Page>(`/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  deletePage: (id: string) => req<void>(`/conversations/${id}`, { method: "DELETE" }),
  pageRuns: (id: string) => req<Run[]>(`/conversations/${id}/runs`),

  startRun: (pageId: string, task: string) =>
    req<{ run_id: string }>(`/conversations/${pageId}/runs`, { method: "POST", body: JSON.stringify({ task }) }),
  run: (id: string) => req<Run>(`/runs/${id}`),
  resume: (id: string, approval_id: string, decision: Record<string, any>) =>
    req(`/runs/${id}/resume`, { method: "POST", body: JSON.stringify({ approval_id, decision }) }),
  cancel: (id: string) => req<Run>(`/runs/${id}/cancel`, { method: "POST" }),
  approvals: () => req<Approval[]>("/approvals"),

  tools: () => req<Tool[]>("/tools"),
  tool: (name: string) => req<Tool>(`/tools/${name}`),
  setToolStatus: (name: string, status: "deprecate" | "restore") =>
    req<Tool>(`/tools/${name}/${status}`, { method: "POST" }),

  settings: () => req<Setting[]>("/settings"),
  saveSettings: (values: Record<string, string>) =>
    req<Setting[]>("/settings", { method: "PUT", body: JSON.stringify(values) }),
};

/**
 * Follow a run's events over SSE. Calls `onEvent` for each event and `onEnd(status)` when the
 * server closes the stream (run finished or paused). Returns a function that stops listening.
 */
export function follow(
  runId: string,
  after: number,
  onEvent: (e: Event) => void,
  onEnd: (status: string) => void,
): () => void {
  const es = new EventSource(`${API}/runs/${runId}/events?after=${after}`);
  const handler = (ev: MessageEvent) => {
    const seq = Number((ev as any).lastEventId || 0);
    onEvent({ seq, type: ev.type, payload: JSON.parse(ev.data) });
  };
  // EventSource only dispatches named events to listeners registered by name.
  for (const t of EVENT_TYPES) es.addEventListener(t, handler as EventListener);
  es.addEventListener("end", (ev) => {
    es.close();
    onEnd(JSON.parse((ev as MessageEvent).data).status);
  });
  es.onerror = () => {
    es.close();
    onEnd("disconnected");
  };
  return () => es.close();
}

export const EVENT_TYPES = [
  "run_started", "run_completed", "run_failed", "run_paused", "run_resumed",
  "plan_proposed", "plan_approved", "plan_rejected",
  "tool_gap_found", "tool_synthesized", "tool_test_result", "tool_registered", "tool_build_failed",
  "step_started", "step_completed", "step_failed", "step_blocked",
  "llm_called", "tool_called", "tool_result",
  "approval_requested", "approval_resolved", "question_asked", "question_answered",
];
