// frontend/src/reconcileView.ts  (E144)
// Pure helpers for the "Reconciliation" section of the Real Money Calibration page (no runtime imports, no network, never throws).
//  - Real Trade: GET /reconcile-report/REAL = the hourly broker-vs-book drift report (execution/hourly_reconcile.py). It changes nothing in the book.
//  - Position Stocks: GET /reconcile/pending = exits still waiting for their real fill price (orders/reconcile.py list_pending_reconcile).
// Covered by reconcile.test.mjs.
import type { ReconcileDriftRow, ReconcileReportDoc } from "./realTradeApi";
import type { PendingReconcileDoc, PendingReconcileRow } from "./positionStocksApi";

export type Tone = "good" | "warn" | "bad" | undefined;

/** "2026-10-25T09:30:00+00:00" -> "2026-10-25 15:00 IST". Fixed +05:30, no locale, so it reads the same everywhere. "-" when unreadable. */
export function istStamp(iso: unknown): string {
  if (typeof iso !== "string" || !iso) return "-";
  const t = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);   // a stamp with no zone is UTC on the backend
  if (!isFinite(t)) return "-";
  const d = new Date(t + 5.5 * 3600 * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} IST`;
}

const isNum = (v: unknown): v is number => typeof v === "number" && isFinite(v);

/** One plain sentence per drift row, in the words the Telegram alert uses. */
export function driftText(d: ReconcileDriftRow): string {
  switch (d.kind) {
    case "missing_at_broker": return `${d.symbol}: the book holds ${d.book_qty}, the broker holds none`;
    case "book_over_broker": return `${d.symbol}: the book holds ${d.book_qty}, the broker holds ${d.broker_qty}`;
    case "broker_over_book": return `${d.symbol}: the broker holds ${d.broker_qty}, the book ${d.book_qty} (the account is shared with Position Stocks, so this alone is not an alarm)`;
    case "broker_only": return `${isNum(d.count) ? d.count : (d.symbols || []).length} broker holding(s) with no open book row: ${(d.symbols || []).slice(0, 12).join(", ")}${(d.symbols || []).length > 12 ? ", …" : ""} (shared account: not an alarm)`;
    case "stale_order": return `order #${d.order_id} ${d.symbol} ${d.side} is still ${d.status} after ${d.age_minutes} min`;
    default: return `${d.kind}${d.symbol ? ` ${d.symbol}` : ""}`;
  }
}

export interface ReconcileView {
  kind: "unavailable" | "error" | "clean" | "drift";
  tone: Tone;
  headline: string;
  warns: string[];
  infos: string[];
  ranAt: string;
}

export function reconcileView(doc: ReconcileReportDoc | null | undefined): ReconcileView {
  const none: ReconcileView = { kind: "unavailable", tone: "warn", headline: "No reconcile report is available.", warns: [], infos: [], ranAt: "-" };
  if (!doc || typeof doc !== "object") return none;
  const ranAt = istStamp(doc.ran_at);
  if (doc.error) return { ...none, kind: "error", tone: "bad", headline: `The last hourly check could not finish (${String(doc.error).slice(0, 160)}). The book was not changed.`, ranAt };
  const drift = Array.isArray(doc.drift) ? doc.drift : [];
  const warns = drift.filter((d) => d && d.severity === "warn").map(driftText);
  const infos = drift.filter((d) => d && d.severity !== "warn").map(driftText);
  const n = isNum(doc.book_positions) ? doc.book_positions : null;
  const book = n == null ? "" : ` ${n} open position${n === 1 ? "" : "s"} in the book.`;
  if (warns.length === 0) return { kind: "clean", tone: "good", headline: `Book and broker agree.${book}`, warns, infos, ranAt };
  return { kind: "drift", tone: "warn", headline: `${warns.length} warning${warns.length === 1 ? "" : "s"}: the book and the broker disagree. Nothing was changed.${book}`, warns, infos, ranAt };
}

/** The position-stocks sweep gives up (rewrites the sentinel to *_UNRESOLVED) after PENDING_RECONCILE_MAX_AGE_DAYS (default 3). */
export const PENDING_GIVE_UP_DAYS = 3;

export interface PendingLine { id: number; symbol: string; status: string; closedDay: string; age: string; how: string; pnl: string; tone: Tone }
export interface PendingView { kind: "unavailable" | "clean" | "stuck"; tone: Tone; headline: string; lines: PendingLine[] }

export function pendingView(doc: PendingReconcileDoc | null | undefined): PendingView {
  if (!doc || typeof doc !== "object" || !Array.isArray(doc.rows)) return { kind: "unavailable", tone: "warn", headline: "The pending-fill list is not available.", lines: [] };
  const lines: PendingLine[] = doc.rows.map((r: PendingReconcileRow) => {
    const age = isNum(r.age_days) ? r.age_days : null;
    return {
      id: r.id, symbol: r.symbol, status: r.status, closedDay: r.closed_day_ist || "-",
      age: age == null ? "-" : age === 0 ? "today" : `${age} day${age === 1 ? "" : "s"}`,
      how: r.resolvable_via || "-", pnl: isNum(r.realized_pnl) ? `${r.realized_pnl < 0 ? "-" : ""}₹${Math.abs(Math.round(r.realized_pnl)).toLocaleString("en-IN")}` : "-",
      tone: age != null && age >= PENDING_GIVE_UP_DAYS ? "bad" : age != null && age >= 1 ? "warn" : undefined,
    };
  });
  if (lines.length === 0) return { kind: "clean", tone: "good", headline: "Nothing is stuck: every exit has its real fill price.", lines };
  const old = lines.filter((l) => l.tone).length;
  return {
    kind: "stuck", tone: old ? "warn" : undefined,
    headline: `${lines.length} exit${lines.length === 1 ? "" : "s"} still waiting for the real fill price${old ? `; ${old} from an earlier day` : " (normal for a few minutes after an exit)"}.`,
    lines,
  };
}
