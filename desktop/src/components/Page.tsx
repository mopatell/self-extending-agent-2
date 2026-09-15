import { ArrowUp, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { TERMINAL } from "../api";
import type { RunView } from "../runView";
import { renamePage, toast, useStore } from "../store";
import { usePage } from "../usePage";
import { AnswerBlock, ErrorBlock, PlanBlock, StepBlock, TaskBlock, ToolBuildBlock } from "./blocks/Blocks";
import { Button, Kbd } from "./ui";

export function Page({ id }: { id: string }) {
  const page = useStore((s) => s.pages.find((p) => p.id === id));
  const { runs, loading, send, decide, cancel } = usePage(id);
  const bottom = useRef<HTMLDivElement>(null);
  const live = runs.some((r) => !TERMINAL.has(r.status));

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [runs.length, runs[runs.length - 1]?.lastSeq]);

  if (!page) return <Empty />;

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-x-hidden overflow-y-auto">
        <div className="mx-auto max-w-[760px] min-w-0 px-12 pt-16 pb-6">
          <Title id={id} title={page.title} />
          <div className="mt-1 text-[12px] text-faint select-text">
            files for this page: <code className="!bg-transparent !p-0 !text-faint">workspaces/{id}/</code>
          </div>
          {loading && <div className="mt-8 text-faint">loading…</div>}
          {!loading && runs.length === 0 && (
            <div className="mt-8 text-[15px] text-faint">
              Type a task below. The agent plans it, builds any tools it needs, and asks before doing anything risky.
            </div>
          )}
          {runs.map((r) => (
            <RunBlocks key={r.id} run={r} decide={(aid, d) => decide(r.id, aid, d)} cancel={() => cancel(r.id)} />
          ))}
          <div ref={bottom} />
        </div>
      </div>
      <Composer onSend={send} busy={live} />
    </div>
  );
}

function RunBlocks({ run, decide, cancel }: { run: RunView; decide: (a: string, d: Record<string, any>) => Promise<void>; cancel: () => void }) {
  const live = !TERMINAL.has(run.status);
  return (
    <section className="border-t border-line first:border-0">
      <div className="flex items-start justify-between gap-4">
        <TaskBlock run={run} />
        {live && (
          <Button variant="ghost" size="sm" className="mt-9" onClick={cancel}><Square size={12} /> Stop</Button>
        )}
      </div>
      {run.plan && <PlanBlock run={run} decide={decide} />}
      {run.toolBuilds.map((b, i) => <ToolBuildBlock key={i} build={b} />)}
      {run.stepOrder.map((sid) => <StepBlock key={sid} step={run.steps[sid]} run={run} decide={decide} />)}
      {run.answer !== undefined && <AnswerBlock text={run.answer} />}
      {run.error && <ErrorBlock text={run.error} cancelled={run.status === "cancelled"} />}
    </section>
  );
}

function Title({ id, title }: { id: string; title: string }) {
  const [value, setValue] = useState(title);
  useEffect(() => setValue(title), [title]);
  const commit = () => {
    const t = value.trim() || "Untitled";
    if (t !== title) renamePage(id, t).catch((e) => toast(e.message));
  };
  return (
    <input
      className="w-full bg-transparent text-[40px] leading-tight font-bold outline-none placeholder:text-faint"
      value={value}
      placeholder="Untitled"
      onChange={(e) => setValue(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}

function Composer({ onSend, busy }: { onSend: (t: string) => Promise<void>; busy: boolean }) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  const engine = useStore((s) => s.engine);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        ref.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const submit = async () => {
    const t = text.trim();
    if (!t || engine !== "ready") return;
    setText("");
    try {
      await onSend(t);
    } catch (e: any) {
      toast(e.message);
      setText(t);
    }
  };

  return (
    <div className="px-12 pb-6">
      <div className="mx-auto max-w-[760px]">
        <div className="flex items-end gap-2 rounded-lg border border-line bg-page p-2 shadow-sm focus-within:border-accent">
          <textarea
            ref={ref}
            rows={Math.min(6, Math.max(1, text.split("\n").length))}
            className="flex-1 resize-none bg-transparent px-1.5 py-1 outline-none placeholder:text-faint"
            placeholder={busy ? "A run is in progress — you can queue another task" : "What should the agent do?"}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey || !e.shiftKey)) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <Button variant="primary" size="sm" disabled={!text.trim() || engine !== "ready"} onClick={submit} title="Send (Enter)">
            <ArrowUp size={14} />
          </Button>
        </div>
        <div className="mt-1.5 text-[11.5px] text-faint">
          <Kbd>Enter</Kbd> send · <Kbd>Shift+Enter</Kbd> newline · <Kbd>⌘K</Kbd> focus
        </div>
      </div>
    </div>
  );
}

function Empty() {
  return (
    <div className="flex h-full items-center justify-center text-faint">
      Create a page to start.
    </div>
  );
}
