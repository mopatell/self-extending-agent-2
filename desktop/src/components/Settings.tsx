import { useEffect, useState } from "react";
import { api, type Setting } from "../api";
import { setTheme, toast, useStore, type Theme } from "../store";
import { Button, Field, inputClass } from "./ui";

const GROUPS: [string, string[]][] = [
  ["Models", ["PLANNER_MODEL", "TOOLSMITH_MODEL", "WORKER_MODEL"]],
  ["API keys", ["GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]],
  ["Behaviour", ["AUTO_APPROVE", "SANDBOX"]],
  ["Limits", ["MAX_CONCURRENT_LLM_CALLS", "MAX_PARALLEL_STEPS", "MAX_LOOP_STEPS", "TOOL_RESULT_MAX_CHARS"]],
];

const HINTS: Record<string, string> = {
  PLANNER_MODEL: "provider:model — e.g. groq:openai/gpt-oss-120b, ollama:qwen2.5-coder:14b, anthropic:claude-opus-5",
  AUTO_APPROVE: "none = ask for everything · safe = auto-approve plans · all = never ask",
  SANDBOX: "docker runs agent code in a container; local runs it on this machine (unsafe, dev only)",
};

export function Settings() {
  const [items, setItems] = useState<Setting[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const theme = useStore((s) => s.theme);

  const load = () =>
    api.settings().then((s) => {
      setItems(s);
      setValues(Object.fromEntries(s.map((x) => [x.key, x.value])));
    });
  useEffect(() => {
    load().catch((e) => toast(e.message));
  }, []);

  const dirty = items.some((i) => values[i.key] !== i.value);
  const save = async () => {
    setSaving(true);
    try {
      const changed = Object.fromEntries(items.filter((i) => values[i.key] !== i.value).map((i) => [i.key, values[i.key]]));
      const s = await api.saveSettings(changed);
      setItems(s);
      setValues(Object.fromEntries(s.map((x) => [x.key, x.value])));
      toast("Saved. Changes apply to the next run.", "info");
    } catch (e: any) {
      toast(e.message);
    } finally {
      setSaving(false);
    }
  };

  const byKey = Object.fromEntries(items.map((i) => [i.key, i]));
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[760px] px-12 pt-16 pb-10">
        <h1 className="text-[40px] leading-tight font-bold">Settings</h1>
        <p className="mt-1 mb-6 text-muted">Stored in <code>.env</code> next to the project. Keys are never shown once saved.</p>

        <Group title="Appearance">
          <Field label="Theme">
            <div className="flex gap-1">
              {(["system", "light", "dark"] as Theme[]).map((t) => (
                <Button key={t} size="sm" variant={theme === t ? "primary" : "default"} onClick={() => setTheme(t)}>{t}</Button>
              ))}
            </div>
          </Field>
        </Group>

        {GROUPS.map(([title, keys]) => (
          <Group key={title} title={title}>
            {keys.map((k) => byKey[k] && (
              <Field key={k} label={byKey[k].label} hint={HINTS[k]}>
                {byKey[k].kind.startsWith("choice:") ? (
                  <select className={inputClass} value={values[k]} onChange={(e) => setValues({ ...values, [k]: e.target.value })}>
                    {byKey[k].kind.slice(7).split(",").map((o) => <option key={o}>{o}</option>)}
                  </select>
                ) : (
                  <input
                    className={inputClass + " font-mono text-[13px]"}
                    type={byKey[k].kind === "secret" ? "password" : byKey[k].kind === "int" ? "number" : "text"}
                    value={values[k] ?? ""}
                    placeholder={byKey[k].kind === "secret" ? "not set" : ""}
                    onChange={(e) => setValues({ ...values, [k]: e.target.value })}
                  />
                )}
              </Field>
            ))}
          </Group>
        ))}

        <div className="sticky bottom-0 flex items-center gap-3 border-t border-line bg-page py-3">
          <Button variant="primary" disabled={!dirty || saving} onClick={save}>{saving ? "Saving…" : "Save"}</Button>
          {dirty && <Button variant="ghost" onClick={() => load()}>Discard</Button>}
        </div>
      </div>
    </div>
  );
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-7">
      <h2 className="mb-3 text-[16px] font-semibold">{title}</h2>
      <div className="space-y-4">{children}</div>
    </section>
  );
}
