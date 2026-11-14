// group 442: tiny shared pieces of the Real Money Calibration page.
import type { ReactNode } from "react";
import type { RunStatus } from "../../calibrationApi";
import { Button, Card as KitCard, CardHeader, StatusPill as KitPill } from "../kit";
import type { PillState } from "../kit";

// run status -> kit pill state. Each state has its own dot shape (kit), and the label says the same in words, so it is never colour only.
const RUN_STATE: Record<string, PillState> = {
  success: "live", attention: "paused", failed: "offline", timeout: "offline", blocked: "paused",
  cancelled: "closed", running: "info", queued: "closed", never: "closed",
};
const LABEL: Record<string, string> = { success: "OK", attention: "LOOK", failed: "FAILED", timeout: "TIMEOUT", blocked: "BLOCKED", cancelled: "CANCELLED", running: "RUNNING", queued: "QUEUED", never: "NEVER RUN" };

const MEANING: Record<string, string> = {
  success: "OK: it ran and found nothing to worry about.",
  attention: "LOOK: the test ran properly, but its own verdict says a person should look (FAIL / NO_GO / NOT_ENOUGH_DATA), or it needs data from a test that has not run yet. Not a crash: open the output.",
  failed: "FAILED: the script or request itself broke (non-zero exit / HTTP error).",
  timeout: "TIMEOUT: it hit its time limit and was stopped.",
  blocked: "BLOCKED: not started (market-hours rule). Confirm to run it anyway.",
};

export function StatusPill({ status }: { status: RunStatus | "never" | null | undefined }) {
  const s = status || "never";
  return <KitPill state={RUN_STATE[s] || "closed"} title={MEANING[s]}>{LABEL[s] || s}</KitPill>;
}

// E547 (round 8b): Card, Btn and the pills are now thin wrappers over the kit (Card, CardHeader, Button, StatusPill); Chip and Msg use cx- classes in styles/control.css.
export function Card({ title, right, children, tone }: { title?: ReactNode; right?: ReactNode; children: ReactNode; tone?: "warn" | "bad" | "good" }) {
  const kitTone = tone === "bad" ? "sell" : tone === "warn" ? "warn" : "default";
  return (
    <KitCard tone={kitTone} className={`text-[12px]${tone === "good" ? " cx-good" : ""}`}>
      {(title || right) && <CardHeader title={title} action={right} />}
      {children}
    </KitCard>
  );
}

/** A small pill that can act as a toggle (filter chips). */
export function Chip({ children, onClick, active, tone, title }: { children: ReactNode; onClick?: () => void; active?: boolean; tone?: "good" | "warn" | "bad"; title?: string }) {
  const base = `cx-chip${tone ? ` ${tone}` : ""}${active ? " on" : ""}`;
  return onClick
    ? <button type="button" title={title} aria-pressed={!!active} onClick={onClick} className={base}>{children}</button>
    : <span title={title} className={base}>{children}</span>;
}

export function Btn({ children, onClick, disabled, primary, small, title }: { children: ReactNode; onClick?: () => void; disabled?: boolean; primary?: boolean; small?: boolean; title?: string }) {
  return (
    <Button variant={primary ? "primary" : "ghost"} size={small ? "sm" : "md"} title={title} onClick={() => onClick?.()} disabled={disabled}>
      {children}
    </Button>
  );
}

export function Msg({ kind, children }: { kind: "err" | "ok" | "info" | "warn"; children: ReactNode }) {
  return <div className={`cx-msg ${kind}`} role={kind === "err" ? "alert" : "status"}>{children}</div>;
}

export function money(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "-";
  return `${n < 0 ? "-" : n > 0 ? "+" : ""}₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}
export const pnlClass = (n: number | null | undefined) => (n == null ? "text-mist" : n > 0 ? "text-signal-buy" : n < 0 ? "text-signal-sell" : "text-mist");
