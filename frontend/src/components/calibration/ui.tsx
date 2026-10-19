// group 442: tiny shared pieces of the Real Money Calibration page.
import type { ReactNode } from "react";
import type { RunStatus } from "../../calibrationApi";

const PILL: Record<string, string> = {
  success: "border-signal-buy text-signal-buy",
  attention: "border-signal-hold text-signal-hold",
  failed: "border-signal-sell text-signal-sell",
  timeout: "border-signal-sell text-signal-sell",
  blocked: "border-signal-hold text-signal-hold",
  cancelled: "border-slate text-mist",
  running: "border-signal-buy text-signal-buy animate-pulse",
  queued: "border-slate text-mist",
  never: "border-slate text-mist",
};
const LABEL: Record<string, string> = { success: "OK", attention: "LOOK", failed: "FAILED", timeout: "TIMEOUT", blocked: "BLOCKED", cancelled: "CANCELLED", running: "RUNNING", queued: "QUEUED", never: "NEVER RUN" };

export function StatusPill({ status }: { status: RunStatus | "never" | null | undefined }) {
  const s = status || "never";
  return <span className={`inline-block rounded-lg border px-1.5 py-0.5 text-[10px] font-bold tracking-wide ${PILL[s] || PILL.never}`}>{LABEL[s] || s}</span>;
}

export function Card({ title, right, children, tone }: { title?: ReactNode; right?: ReactNode; children: ReactNode; tone?: "warn" | "bad" | "good" }) {
  const border = tone === "bad" ? "border-signal-sell" : tone === "warn" ? "border-signal-hold" : tone === "good" ? "border-signal-buy" : "border-slate";
  return (
    <section className={`rounded-2xl border ${border} bg-graphite p-3 text-[12px]`}>
      {(title || right) && (
        <header className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-[11px] font-bold uppercase tracking-widest text-mist">{title}</h3>
          <div className="flex flex-wrap items-center gap-2">{right}</div>
        </header>
      )}
      {children}
    </section>
  );
}

/** A small pill that can act as a toggle (filter chips). */
export function Chip({ children, onClick, active, tone, title }: { children: ReactNode; onClick?: () => void; active?: boolean; tone?: "good" | "warn" | "bad"; title?: string }) {
  const c = tone === "bad" ? "border-signal-sell text-signal-sell" : tone === "warn" ? "border-signal-hold text-signal-hold" : tone === "good" ? "border-signal-buy text-signal-buy" : "border-slate text-mist";
  const base = `inline-flex items-center gap-1 rounded-lg border px-2 py-0.5 text-[11px] ${c} ${active ? "bg-white/10 font-bold" : ""}`;
  return onClick
    ? <button type="button" title={title} aria-pressed={!!active} onClick={onClick} className={`${base} hover:bg-white/5`}>{children}</button>
    : <span title={title} className={base}>{children}</span>;
}

export function Btn({ children, onClick, disabled, primary, small, title }: { children: ReactNode; onClick?: () => void; disabled?: boolean; primary?: boolean; small?: boolean; title?: string }) {
  return (
    <button type="button" title={title} onClick={onClick} disabled={disabled} className={`${primary ? "btn-primary-term" : "btn-terminal"} ${small ? "!px-2 !py-1 !text-[11px]" : ""}`}>
      {children}
    </button>
  );
}

export function Msg({ kind, children }: { kind: "err" | "ok" | "info" | "warn"; children: ReactNode }) {
  const c = kind === "err" ? "border-signal-sell text-signal-sell" : kind === "ok" ? "border-signal-buy text-signal-buy" : kind === "warn" ? "border-signal-hold text-signal-hold" : "border-slate text-mist";
  return <div className={`rounded-xl border ${c} px-3 py-2 text-[12px]`} role={kind === "err" ? "alert" : "status"}>{children}</div>;
}

export function money(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "-";
  return `${n < 0 ? "-" : n > 0 ? "+" : ""}₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}
export const pnlClass = (n: number | null | undefined) => (n == null ? "text-mist" : n > 0 ? "text-signal-buy" : n < 0 ? "text-signal-sell" : "text-mist");
