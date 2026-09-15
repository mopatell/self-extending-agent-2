import { useEffect, useState } from "react";
import { api, type Tool } from "../api";
import { toast } from "../store";
import { Badge, Button } from "./ui";

export function Tools() {
  const [tools, setTools] = useState<Tool[]>([]);
  const [selected, setSelected] = useState<Tool | null>(null);

  const load = () => api.tools().then(setTools).catch((e) => toast(e.message));
  useEffect(() => {
    load();
  }, []);

  const pick = (name: string) => api.tool(name).then(setSelected).catch((e) => toast(e.message));
  const setStatus = async (t: Tool, status: "deprecate" | "restore") => {
    await api.setToolStatus(t.name, status);
    await load();
    await pick(t.name);
  };

  return (
    <div className="flex h-full">
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[760px] px-12 pt-16 pb-10">
          <h1 className="text-[40px] leading-tight font-bold">Tools</h1>
          <p className="mt-1 mb-6 text-muted">Everything the agent has built. Tools are tested in the sandbox before they are registered.</p>
          {tools.length === 0 && <div className="text-faint">Nothing built yet. Give the agent a task that needs a new capability.</div>}
          <div className="divide-y divide-line">
            {tools.map((t) => (
              <button
                key={t.name}
                className={`flex w-full items-center gap-3 px-2 py-2 text-left hover:bg-hover ${selected?.name === t.name ? "bg-hover" : ""}`}
                onClick={() => pick(t.name)}
              >
                <code className="!bg-transparent !p-0 !text-ink">{t.name}</code>
                <Badge>v{t.version}</Badge>
                {t.risk === "approval" && <Badge tone="warn">network</Badge>}
                <span className="flex-1 truncate text-[13px] text-muted">{t.description}</span>
                <span className="text-[12px] text-faint">{t.calls} calls{t.failures ? ` · ${t.failures} failed` : ""}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
      {selected && (
        <div className="w-[440px] shrink-0 overflow-y-auto border-l border-line bg-side px-5 pt-14 pb-10 text-[13px]">
          <div className="mb-1 flex items-center gap-2">
            <code className="!bg-transparent !p-0 !text-[15px] !text-ink">{selected.name}</code>
            <Badge>v{selected.version}</Badge>
            {selected.status === "deprecated" && <Badge tone="danger">deprecated</Badge>}
          </div>
          <p className="mb-3 text-muted">{selected.description}</p>
          <div className="mb-3 flex gap-2">
            {selected.status === "active" ? (
              <Button size="sm" variant="danger" onClick={() => setStatus(selected, "deprecate")}>Deprecate</Button>
            ) : (
              <Button size="sm" onClick={() => setStatus(selected, "restore")}>Restore</Button>
            )}
            <Button size="sm" variant="ghost" onClick={() => setSelected(null)}>Close</Button>
          </div>
          <Section title="Parameters">
            <pre>{JSON.stringify(selected.input_schema.properties ?? {}, null, 1)}</pre>
          </Section>
          <Section title="Source"><pre className="max-h-96">{selected.source}</pre></Section>
          <Section title="Tests"><pre className="max-h-64">{selected.test_code}</pre></Section>
          <div className="mt-3 text-faint">
            deps: {selected.deps.length ? selected.deps.join(", ") : "none"} · built by run {selected.created_by_run ?? "?"} · {selected.created_at.slice(0, 10)}
          </div>
        </div>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-3">
      <div className="mb-1 text-[12px] font-medium text-faint uppercase">{title}</div>
      {children}
    </div>
  );
}
