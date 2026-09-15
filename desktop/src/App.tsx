import { useEffect, useState } from "react";
import { api } from "./api";

/** D0: prove the shell works - wait for the backend, then show a placeholder. Replaced in D1. */
export default function App() {
  const [engine, setEngine] = useState<"starting" | "ready" | "failed">("starting");
  const [version, setVersion] = useState("");

  useEffect(() => {
    let tries = 0;
    const timer = setInterval(async () => {
      try {
        const h = await api.health();
        setVersion(h.version);
        setEngine("ready");
        clearInterval(timer);
      } catch {
        if (++tries > 60) {
          setEngine("failed");
          clearInterval(timer);
        }
      }
    }, 500);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="flex h-full items-center justify-center text-muted" data-tauri-drag-region>
      {engine === "starting" && <span>starting engine…</span>}
      {engine === "ready" && <span>sea {version} · engine ready</span>}
      {engine === "failed" && <span className="text-danger">could not reach the engine on :8765</span>}
    </div>
  );
}
