/** The few primitives the whole UI is built from. */

import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "default" | "primary" | "danger" | "ghost";

const styles: Record<Variant, string> = {
  default: "border border-line bg-page hover:bg-hover",
  primary: "bg-accent text-white hover:brightness-110",
  danger: "border border-line text-danger hover:bg-danger-bg",
  ghost: "text-muted hover:bg-hover hover:text-ink",
};

export function Button({
  variant = "default",
  size = "md",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" }) {
  const pad = size === "sm" ? "h-7 px-2 text-[13px]" : "h-8 px-3 text-[14px]";
  return (
    <button
      className={`inline-flex items-center gap-1.5 rounded-md font-medium transition-colors ${pad} ${styles[variant]} ${className}`}
      {...props}
    />
  );
}

export function IconButton({ className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={`inline-flex h-6 w-6 items-center justify-center rounded text-muted hover:bg-hover hover:text-ink ${className}`}
      {...props}
    />
  );
}

export function Badge({ children, tone = "muted" }: { children: ReactNode; tone?: "muted" | "ok" | "warn" | "danger" | "accent" }) {
  const tones = {
    muted: "bg-hover text-muted",
    ok: "bg-ok-bg text-ok",
    warn: "bg-warn-bg text-warn",
    danger: "bg-danger-bg text-danger",
    accent: "bg-accent-bg text-accent",
  };
  return <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[12px] font-medium ${tones[tone]}`}>{children}</span>;
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="rounded border border-line bg-code px-1 font-mono text-[11px] text-muted">{children}</kbd>;
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-faint border-t-transparent ${className}`}
    />
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <div className="mb-1 text-[13px] font-medium text-muted">{label}</div>
      {children}
      {hint && <div className="mt-1 text-[12px] text-faint">{hint}</div>}
    </label>
  );
}

export const inputClass =
  "w-full rounded-md border border-line bg-page px-2.5 py-1.5 text-[14px] outline-none focus:border-accent focus:ring-2 focus:ring-accent-bg";

/** Status dot colours shared by the sidebar and blocks. */
export function statusTone(status: string | null | undefined): "muted" | "ok" | "warn" | "danger" | "accent" {
  if (!status) return "muted";
  if (status === "completed") return "ok";
  if (status.startsWith("awaiting")) return "warn";
  if (status === "failed" || status === "cancelled") return "danger";
  return "accent";
}
