/** Loads a page's runs, follows the live ones over SSE, and exposes send / decide / cancel. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, follow, TERMINAL } from "./api";
import { refreshPending, toast } from "./store";
import { apply, emptyView, type RunView } from "./runView";

export function usePage(pageId: string) {
  const [runs, setRuns] = useState<RunView[]>([]);
  const [loading, setLoading] = useState(true);
  const closers = useRef(new Map<string, () => void>());

  const update = useCallback((id: string, fn: (v: RunView) => RunView) => {
    setRuns((rs) => rs.map((r) => (r.id === id ? fn(r) : r)));
  }, []);

  /** Subscribe to a run from `after`; re-subscribes on disconnect while the run is live. */
  const watch = useCallback(
    (id: string, after: number) => {
      closers.current.get(id)?.();
      let last = after;
      const stop = follow(
        id,
        after,
        (e) => {
          last = e.seq;
          update(id, (v) => apply(v, e));
        },
        (status) => {
          closers.current.delete(id);
          if (status === "disconnected") {
            setTimeout(() => {
              setRuns((rs) => {
                const r = rs.find((x) => x.id === id);
                if (r && !TERMINAL.has(r.status)) watch(id, last);
                return rs;
              });
            }, 1500);
          } else {
            refreshPending();
          }
        },
      );
      closers.current.set(id, stop);
    },
    [update],
  );

  useEffect(() => {
    let cancelled = false;
    setRuns([]);
    setLoading(true);
    if (!pageId) return;
    api
      .pageRuns(pageId)
      .then((list) => {
        if (cancelled) return;
        setRuns(list.map((r) => emptyView(r.id, r.task, r.status)));
        setLoading(false);
        list.forEach((r) => watch(r.id, 0));
      })
      .catch((e) => toast(String(e.message)));
    return () => {
      cancelled = true;
      closers.current.forEach((stop) => stop());
      closers.current.clear();
    };
  }, [pageId, watch]);

  const send = useCallback(
    async (task: string) => {
      const { run_id } = await api.startRun(pageId, task);
      setRuns((rs) => [...rs, emptyView(run_id, task)]);
      watch(run_id, 0);
    },
    [pageId, watch],
  );

  const decide = useCallback(
    async (runId: string, approvalId: string, decision: Record<string, any>) => {
      const run = runs.find((r) => r.id === runId);
      await api.resume(runId, approvalId, decision);
      update(runId, (v) => ({ ...v, pending: undefined, status: "running" }));
      watch(runId, run?.lastSeq ?? 0);
    },
    [runs, update, watch],
  );

  const cancel = useCallback(
    async (runId: string) => {
      await api.cancel(runId);
      const run = runs.find((r) => r.id === runId);
      watch(runId, run?.lastSeq ?? 0);
    },
    [runs, watch],
  );

  return { runs, loading, send, decide, cancel };
}
