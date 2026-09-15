/** One small global store (useSyncExternalStore) - no state library needed for three views. */

import { useSyncExternalStore } from "react";
import { api, type Page } from "./api";

export type View = { kind: "page"; id: string } | { kind: "tools" } | { kind: "settings" };
export type Theme = "system" | "light" | "dark";

type State = {
  engine: "starting" | "ready" | "failed";
  version: string;
  pages: Page[];
  view: View;
  pendingCount: number;
  pendingPage: string | null;
  theme: Theme;
  toast: { text: string; kind: "error" | "info" } | null;
};

let state: State = {
  engine: "starting",
  version: "",
  pages: [],
  view: location.hash === "#tools" ? { kind: "tools" } : location.hash === "#settings" ? { kind: "settings" } : { kind: "page", id: "" },
  pendingCount: 0,
  pendingPage: null,
  theme: (new URLSearchParams(location.search).get("theme") as Theme) || (localStorage.getItem("theme") as Theme) || "system",
  toast: null,
};
const listeners = new Set<() => void>();

export function set(patch: Partial<State> | ((s: State) => Partial<State>)) {
  state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
  listeners.forEach((l) => l());
}
export function get() {
  return state;
}
export function useStore<T>(selector: (s: State) => T): T {
  return useSyncExternalStore(
    (l) => (listeners.add(l), () => listeners.delete(l)),
    () => selector(state),
  );
}

// ------------------------------------------------------------------ actions

export function toast(text: string, kind: "error" | "info" = "error") {
  set({ toast: { text, kind } });
  setTimeout(() => set((s) => (s.toast?.text === text ? { toast: null } : {})), 4000);
}

export async function loadPages() {
  const pages = await api.pages();
  set((s) => ({
    pages,
    view: s.view.kind === "page" && !s.view.id && pages[0] ? { kind: "page", id: pages[0].id } : s.view,
  }));
}

export async function newPage() {
  const { id } = await api.createPage("Untitled");
  await loadPages();
  set({ view: { kind: "page", id } });
}

export async function renamePage(id: string, title: string) {
  await api.renamePage(id, title);
  await loadPages();
}

export async function deletePage(id: string) {
  await api.deletePage(id);
  await loadPages();
  set((s) => ({ view: s.view.kind === "page" && s.view.id === id ? { kind: "page", id: s.pages[0]?.id ?? "" } : s.view }));
}

export function open(view: View) {
  set({ view });
}

/** Sidebar freshness: pending badge and page list/status dots. Polled while the app is open. */
export async function refreshPending() {
  try {
    const [pending, pages] = await Promise.all([api.approvals(), api.pages()]);
    set({ pendingCount: pending.length, pendingPage: pending[0]?.conversation_id ?? null, pages });
  } catch {
    /* engine down; the health poll will notice */
  }
}

export function setTheme(theme: Theme) {
  localStorage.setItem("theme", theme);
  set({ theme });
  applyTheme(theme);
}

export function applyTheme(theme: Theme) {
  const dark = theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
}

/** Poll /health until the backend answers, then load everything. */
export async function boot() {
  applyTheme(state.theme);
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(get().theme));
  for (let i = 0; i < 120; i++) {
    try {
      const h = await api.health();
      set({ engine: "ready", version: h.version });
      await loadPages();
      await refreshPending();
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 500));
    }
  }
  set({ engine: "failed" });
}
