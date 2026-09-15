/** The blocks a run is rendered as. One file: they are small and share helpers. */

import { AlertCircle, Check, ChevronRight, Hammer, Loader2, MessageCircleQuestion, X } from "lucide-react";
import { useState } from "react";
import type { Plan } from "../../api";
import type { Pending, RunView, StepView, ToolBuild } from "../../runView";
import { Badge, Button, inputClass, Spinner } from "../ui";
import { Markdown } from "./Markdown";

export type Decide = (approvalId: string, decision: Record<string, any>) => Promise<void>;

/** Full-width row with a left gutter marker, like a Notion block. */
function Block({ children, marker, className = "" }: { children: React.ReactNode; marker?: React.ReactNode; className?: string }) {
  return (
    <div className={`group relative py-1.5 pl-7 ${className}`}>
      <div className="absolute top-2 left-0 flex h-5 w-5 items-center justify-center text-faint">{marker}</div>
      {children}
    </div>
  );
}

const short = (s: string, n = 160) => (s.length > n ? s.slice(0, n) + "…" : s);

// ------------------------------------------------------------------ task

export function TaskBlock({ run }: { run: RunView }) {
  return (
    <div className="mt-8 mb-2">
      <div className="text-[17px] font-medium">{run.task}</div>
      <div className="mt-0.5 flex items-center gap-2 text-[12px] text-faint">
        <Badge tone={run.status === "completed" ? "ok" : run.status.startsWith("awaiting") ? "warn" : run.status === "failed" || run.status === "cancelled" ? "danger" : "accent"}>
          {run.status.replace(/_/g, " ")}
        </Badge>
        {run.llmCalls > 0 && <span>{run.llmCalls} model calls · {(run.tokensIn + run.tokensOut).toLocaleString()} tokens</span>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ plan

export function PlanBlock({ run, decide }: { run: RunView; decide: Decide }) {
  const pending = run.pending?.kind === "plan" ? run.pending : undefined;
  const [editing, setEditing] = useState<Plan | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const plan = editing ?? run.plan;
  if (!plan) return null;

  return (
    <Block marker={<ChevronRight size={14} />}>
      <div className="mb-1 flex items-center gap-2 text-[13px] text-muted">
        <span className="font-medium">Plan</span>
        {run.planStatus === "approved" && <Badge tone="ok">approved</Badge>}
        {pending && <Badge tone="warn">waiting for you</Badge>}
        {run.planRejections.length > 0 && <span className="text-faint">· replanned {run.planRejections.length}×</span>}
      </div>
      <div className={`rounded-md border ${pending ? "border-warn" : "border-line"} overflow-hidden`}>
        <table className="w-full text-[14px]">
          <tbody>
            {plan.steps.map((s) => (
              <tr key={s.id} className="border-b border-line last:border-0">
                <td className="w-9 px-2 py-1.5 align-top font-mono text-[12px] text-faint">{s.id}</td>
                <td className="px-2 py-1.5 align-top">
                  {editing ? (
                    <input
                      className={inputClass}
                      value={s.description}
                      onChange={(e) =>
                        setEditing({ steps: editing.steps.map((x) => (x.id === s.id ? { ...x, description: e.target.value } : x)) })
                      }
                    />
                  ) : (
                    s.description
                  )}
                  <div className="mt-0.5 flex flex-wrap gap-1 text-[12px] text-faint">
                    {s.depends_on.length > 0 && <span>after {s.depends_on.join(", ")}</span>}
                    {s.tools.map((t) => <Badge key={t}>{t}</Badge>)}
                    {s.missing_tools.map((m) => <Badge key={m.name} tone="warn">build {m.name}</Badge>)}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {pending && (
          <div className="flex flex-wrap items-center gap-2 border-t border-line bg-side px-2 py-2">
            {rejecting ? (
              <>
                <input className={inputClass + " flex-1"} autoFocus placeholder="Why? The planner will try again." value={reason} onChange={(e) => setReason(e.target.value)} />
                <Button variant="danger" size="sm" onClick={() => decide(pending.approvalId, { approved: false, reason })}>Reject</Button>
                <Button variant="ghost" size="sm" onClick={() => setRejecting(false)}>Back</Button>
              </>
            ) : (
              <>
                <Button variant="primary" size="sm" onClick={() => decide(pending.approvalId, editing ? { approved: true, plan: editing } : { approved: true })}>
                  <Check size={14} /> {editing ? "Approve edited plan" : "Approve"}
                </Button>
                {editing ? (
                  <Button variant="ghost" size="sm" onClick={() => setEditing(null)}>Discard edits</Button>
                ) : (
                  <Button size="sm" onClick={() => setEditing(structuredClone(plan))}>Edit steps</Button>
                )}
                <Button variant="danger" size="sm" onClick={() => setRejecting(true)}><X size={14} /> Reject</Button>
              </>
            )}
          </div>
        )}
      </div>
    </Block>
  );
}

// ------------------------------------------------------------------ tool build

export function ToolBuildBlock({ build }: { build: ToolBuild }) {
  const [open, setOpen] = useState(false);
  const done = build.registered || build.failed;
  return (
    <Block marker={<Hammer size={14} />}>
      <button className="flex w-full items-center gap-2 text-left text-[14px]" onClick={() => setOpen(!open)}>
        <span className="text-muted">{build.mode === "edit" ? "Fixing tool" : "Building tool"}</span>
        <code className="!bg-transparent !p-0 !text-ink">{build.name || "…"}</code>
        {build.registered && <Badge tone="ok">v{build.registered.version} · tests passed</Badge>}
        {build.failed && <Badge tone="danger">failed</Badge>}
        {!done && <Spinner />}
        {build.attempts.length > 1 && <span className="text-[12px] text-faint">{build.attempts.length} attempts</span>}
      </button>
      {open && (
        <div className="mt-1 space-y-1 text-[13px] text-muted">
          <div>{short(build.description, 400)}</div>
          {build.attempts.map((a) => (
            <div key={a.attempt} className="flex gap-2">
              <span className={a.passed ? "text-ok" : "text-danger"}>{a.passed ? "✓" : "✗"} attempt {a.attempt}</span>
              {!a.passed && a.output && <pre className="max-h-40 flex-1 overflow-auto text-[12px]">{a.output}</pre>}
            </div>
          ))}
          {build.failed && <div className="text-danger">{build.failed}</div>}
        </div>
      )}
    </Block>
  );
}

// ------------------------------------------------------------------ step

export function StepBlock({ step, run, decide }: { step: StepView; run: RunView; decide: Decide }) {
  const marker =
    step.status === "running" ? <Loader2 size={14} className="animate-spin text-accent" /> :
    step.status === "done" ? <Check size={14} className="text-ok" /> :
    <AlertCircle size={14} className="text-danger" />;
  const pending = run.pending && run.pending.kind !== "plan" && run.pending.payload.step_id === step.id ? run.pending : undefined;
  return (
    <Block marker={marker}>
      <div className="text-[14px]">
        <span className="mr-1.5 font-mono text-[12px] text-faint">{step.id}</span>
        {step.description}
        {step.status === "blocked" && <Badge tone="muted">blocked</Badge>}
      </div>
      {step.calls.length > 0 && (
        <div className="mt-1 space-y-0.5">
          {step.calls.map((c) => <CallRow key={c.callId} call={c} />)}
        </div>
      )}
      {pending && <ApprovalCard pending={pending} decide={decide} />}
      {step.error && <div className="mt-1 text-[13px] text-danger">{step.error}</div>}
      {step.result && step.status === "done" && run.stepOrder.length > 1 && (
        <div className="mt-1 text-[13px] text-muted">{short(step.result, 300)}</div>
      )}
    </Block>
  );
}

function CallRow({ call }: { call: StepView["calls"][number] }) {
  const [open, setOpen] = useState(false);
  const args = JSON.stringify(call.args);
  return (
    <div className="rounded px-1.5 py-0.5 text-[13px] hover:bg-hover">
      <button className="flex w-full items-start gap-1.5 text-left" onClick={() => setOpen(!open)}>
        <span className="mt-0.5 text-faint">⚙</span>
        <span className="min-w-0 break-all font-mono text-[12.5px]">
          <span className="text-ink">{call.name}</span>
          <span className="text-faint">({short(args, open ? 2000 : 90)})</span>
        </span>
        {call.pending && <Spinner className="mt-1 ml-1" />}
      </button>
      {call.output !== undefined && (
        <div className={`ml-5 break-all whitespace-pre-wrap font-mono text-[12.5px] ${call.error ? "text-danger" : "text-muted"}`}>
          ↳ {open ? call.output : short(call.output.replace(/\s+/g, " "), 140)}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ approvals

export function ApprovalCard({ pending, decide }: { pending: Pending; decide: Decide }) {
  const [reason, setReason] = useState("");
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const go = async (d: Record<string, any>) => {
    setBusy(true);
    try {
      await decide(pending.approvalId, d);
    } finally {
      setBusy(false);
    }
  };
  if (pending.kind === "question") {
    return (
      <div className="mt-2 rounded-md border border-warn bg-warn-bg/40 p-3">
        <div className="mb-2 flex items-center gap-2 text-[13px] font-medium"><MessageCircleQuestion size={15} className="text-warn" /> The agent has a question</div>
        <div className="mb-2 text-[14px]">{pending.payload.question}</div>
        <div className="flex gap-2">
          <input className={inputClass} autoFocus placeholder="Your answer" value={answer} onChange={(e) => setAnswer(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && answer && go({ answer })} />
          <Button variant="primary" size="sm" disabled={busy || !answer} onClick={() => go({ answer })}>Send</Button>
        </div>
      </div>
    );
  }
  const p = pending.payload;
  return (
    <div className="mt-2 rounded-md border border-warn bg-warn-bg/40 p-3">
      <div className="mb-1 text-[13px] font-medium">Allow this action?</div>
      <pre className="mb-2 max-h-48 overflow-auto">{p.name}({JSON.stringify(p.arguments, null, 1)})</pre>
      <div className="mb-2 text-[12px] text-muted">{p.reason}</div>
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" size="sm" disabled={busy} onClick={() => go({ approved: true })}><Check size={14} /> Allow</Button>
        <input className={inputClass + " max-w-xs flex-1"} placeholder="Reason for denying (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
        <Button variant="danger" size="sm" disabled={busy} onClick={() => go({ approved: false, reason })}><X size={14} /> Deny</Button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ answer / error

export function AnswerBlock({ text }: { text: string }) {
  return (
    <Block marker={<Check size={14} className="text-ok" />} className="mt-1">
      <div className="rounded-md border-l-2 border-ok pl-3 text-[15px]"><Markdown text={text} /></div>
    </Block>
  );
}

export function ErrorBlock({ text, cancelled }: { text: string; cancelled?: boolean }) {
  return (
    <Block marker={<AlertCircle size={14} className="text-danger" />}>
      <div className="rounded-md border-l-2 border-danger pl-3 text-[14px] text-danger">{cancelled ? "Cancelled." : text}</div>
    </Block>
  );
}
