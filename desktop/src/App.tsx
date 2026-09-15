import { useEffect } from "react";
import { Page } from "./components/Page";
import { Settings } from "./components/Settings";
import { Sidebar } from "./components/Sidebar";
import { Tools } from "./components/Tools";
import { boot, newPage, refreshPending, toast, useStore } from "./store";

export default function App() {
  const engine = useStore((s) => s.engine);
  const view = useStore((s) => s.view);
  const toastMsg = useStore((s) => s.toast);

  useEffect(() => {
    boot();
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "n") {
        e.preventDefault();
        newPage().catch((err) => toast(err.message));
      }
    };
    window.addEventListener("keydown", onKey);
    const poll = setInterval(refreshPending, 5000);
    return () => {
      window.removeEventListener("keydown", onKey);
      clearInterval(poll);
    };
  }, []);

  if (engine !== "ready") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-muted" data-tauri-drag-region>
        <div className="text-[28px] font-bold text-ink">sea</div>
        {engine === "starting" ? (
          <div className="text-[14px]">starting the engine…</div>
        ) : (
          <div className="max-w-sm text-center text-[14px] text-danger">
            Could not reach the engine on port 8765. Start it with <code>uv run sea serve --port 8765</code> and relaunch.
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex h-full">
      <Sidebar />
      <main className="relative h-full flex-1 bg-page">
        <div className="absolute inset-x-0 top-0 z-10 h-11" data-tauri-drag-region />
        {view.kind === "page" && <Page id={view.id} />}
        {view.kind === "tools" && <Tools />}
        {view.kind === "settings" && <Settings />}
      </main>
      {toastMsg && (
        <div
          className={`fixed bottom-4 left-1/2 z-50 -translate-x-1/2 rounded-md px-3 py-2 text-[13px] text-white shadow-lg ${toastMsg.kind === "error" ? "bg-danger" : "bg-ink"}`}
        >
          {toastMsg.text}
        </div>
      )}
    </div>
  );
}
