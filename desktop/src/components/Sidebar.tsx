import { Hammer, Plus, Settings as SettingsIcon, Trash2 } from "lucide-react";
import { useState } from "react";
import { deletePage, newPage, open, toast, useStore, type View } from "../store";
import { IconButton, statusTone } from "./ui";

const DOT = { muted: "bg-faint", ok: "bg-ok", warn: "bg-warn", danger: "bg-danger", accent: "bg-accent" };

export function Sidebar() {
  const pages = useStore((s) => s.pages);
  const view = useStore((s) => s.view);
  const pending = useStore((s) => s.pendingCount);
  const pendingPage = useStore((s) => s.pendingPage);
  const version = useStore((s) => s.version);
  const [confirm, setConfirm] = useState<string | null>(null);

  const is = (v: View) =>
    v.kind === view.kind && (v.kind !== "page" || (view.kind === "page" && v.id === view.id));
  const item = "flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-[14px] text-muted hover:bg-hover hover:text-ink";
  const active = "bg-hover text-ink";

  return (
    <aside className="flex h-full w-[240px] shrink-0 flex-col bg-side text-ink select-none">
      <div className="h-11 shrink-0" data-tauri-drag-region />
      <div className="px-3 pb-2">
        <div className="flex items-center gap-2 px-2 text-[14px] font-semibold">
          <span className="flex h-5 w-5 items-center justify-center rounded bg-ink text-[11px] text-page">s</span>
          sea
          <span className="ml-auto text-[11px] font-normal text-faint">{version}</span>
        </div>
      </div>

      <nav className="px-3">
        <button className={`${item} ${is({ kind: "tools" }) ? active : ""}`} onClick={() => open({ kind: "tools" })}>
          <Hammer size={15} /> Tools
        </button>
        <button className={`${item} ${is({ kind: "settings" }) ? active : ""}`} onClick={() => open({ kind: "settings" })}>
          <SettingsIcon size={15} /> Settings
        </button>
      </nav>

      <div className="mt-4 flex items-center justify-between px-5 text-[12px] font-medium text-faint">
        Pages
        <IconButton title="New page (⌘N)" onClick={() => newPage().catch((e) => toast(e.message))}><Plus size={14} /></IconButton>
      </div>
      <div className="flex-1 overflow-y-auto px-3 pb-3">
        {pages.map((p) => (
          <div key={p.id} className="group relative">
            <button
              className={`${item} pr-7 ${is({ kind: "page", id: p.id }) ? active : ""}`}
              onClick={() => open({ kind: "page", id: p.id })}
              title={p.last_task ?? undefined}
            >
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${DOT[statusTone(p.last_status)]}`} />
              <span className="truncate">{p.title || "Untitled"}</span>
            </button>
            <div className="absolute top-1 right-1 hidden group-hover:block">
              {confirm === p.id ? (
                <button className="rounded bg-danger px-1.5 py-0.5 text-[11px] text-white" onClick={() => { setConfirm(null); deletePage(p.id).catch((e) => toast(e.message)); }}>
                  delete?
                </button>
              ) : (
                <IconButton title="Delete page" onClick={() => setConfirm(p.id)} onBlur={() => setConfirm(null)}><Trash2 size={13} /></IconButton>
              )}
            </div>
          </div>
        ))}
        {pages.length === 0 && <div className="px-2 py-1 text-[13px] text-faint">No pages yet.</div>}
      </div>

      {pending > 0 && (
        <button
          className="mx-3 mb-3 rounded-md bg-warn-bg px-2.5 py-1.5 text-left text-[13px] text-warn hover:brightness-95"
          onClick={() => pendingPage && open({ kind: "page", id: pendingPage })}
        >
          ⏸ {pending} waiting for you
        </button>
      )}
    </aside>
  );
}
