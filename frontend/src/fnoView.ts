// frontend/src/fnoView.ts - pure helpers of the F&O pages (no network, no React). Covered by fnoView.test.mjs.
import type { FnoPosition, FnoLocks } from "./fnoApi";

export const IST_OFFSET_MIN = 330;
/** epoch seconds -> "YYYY-MM-DD" in IST (whatever the browser's timezone is). */
export function istDay(ts: number | null | undefined): string {
  if (!ts) return "-";
  return new Date(ts * 1000 + IST_OFFSET_MIN * 60000).toISOString().slice(0, 10);
}
export function istHm(ts: number | null | undefined): string {
  if (!ts) return "-";
  return new Date(ts * 1000 + IST_OFFSET_MIN * 60000).toISOString().slice(11, 16);
}
export const inr = (v: number | null | undefined, dp = 0): string => (v == null || Number.isNaN(v) ? "-" : `${v < 0 ? "-" : ""}₹${Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp })}`);
export const signed = (v: number | null | undefined, dp = 2): string => (v == null || Number.isNaN(v) ? "-" : `${v > 0 ? "+" : ""}${v.toFixed(dp)}`);
export const pctOf = (a: number, b: number): number | null => (b ? ((a - b) / b) * 100 : null);

/** The one-line plain-language state of the four REAL locks (nothing is ever sent unless all four are open). */
export function lockSummary(l: FnoLocks | null | undefined): { tone: "off" | "demo" | "real-blocked" | "real"; text: string } {
  if (!l) return { tone: "off", text: "Waiting for the service" };
  if (!l.engine_enabled) return { tone: "off", text: "Engine OFF: no signals, no trades" };
  if (l.mode !== "REAL") return { tone: "demo", text: "DEMO: virtual trades only" };
  if (l.real_orders_possible) return { tone: "real", text: "REAL: orders go to Dhan today" };
  return { tone: "real-blocked", text: `REAL mode, but blocked: ${l.closed_locks.join(", ") || "a lock is closed"}` };
}

/** Rows for the open-position cards: where the premium sits between stop and target (0..100), and the P&L in rupees. */
export function positionProgress(p: FnoPosition): { pct: number; pnl: number | null } {
  const prem = p.premium ?? null;
  const span = p.target_price - p.sl_price;
  const pct = prem == null || span <= 0 ? 0 : Math.max(0, Math.min(100, ((prem - p.sl_price) / span) * 100));
  const pnl = prem == null ? (p.unrealized ?? null) : (prem - p.entry_premium) * p.lots * p.lot_size;
  return { pct: Math.round(pct), pnl: pnl == null ? null : Math.round(pnl * 100) / 100 };
}

/** Day totals of closed positions, grouped by IST day, newest first. Net is used when present, else gross. */
export function dayTotals(ps: FnoPosition[]): { day: string; trades: number; wins: number; net: number }[] {
  const by = new Map<string, { day: string; trades: number; wins: number; net: number }>();
  for (const p of ps) {
    if (p.status !== "CLOSED") continue;
    const d = istDay(p.exit_ts ?? p.entry_ts);
    const net = p.net_pnl ?? p.gross_pnl ?? 0;
    const r = by.get(d) || { day: d, trades: 0, wins: 0, net: 0 };
    r.trades += 1; r.wins += net > 0 ? 1 : 0; r.net = Math.round((r.net + net) * 100) / 100;
    by.set(d, r);
  }
  return [...by.values()].sort((a, b) => (a.day < b.day ? 1 : -1));
}

export const CSV_HEAD = "id,day,underlying,expiry,strike,type,lots,entry_time,entry,exit_time,exit,reason,gross,charges,net,mode";
export function tradesCsv(ps: FnoPosition[]): string {
  const rows = ps.map((p) => [p.id ?? "", istDay(p.entry_ts), p.underlying, p.expiry, p.strike, p.opt_type, p.lots, istHm(p.entry_ts), p.entry_premium, istHm(p.exit_ts), p.exit_premium ?? "", p.exit_reason, p.gross_pnl ?? "", p.charges ?? "", p.net_pnl ?? "", p.mode].join(","));
  return [CSV_HEAD, ...rows].join("\n");
}

/** Reliability label per backtest kind - the page must never present a SIM result like a replay of recorded data. */
export const BT_RELIABILITY: Record<string, { label: string; text: string }> = {
  fno_replay: { label: "RECORDED", text: "Real recorded chains run through the live engine. Most trustworthy; needs recorded days." },
  fno_eod: { label: "COARSE", text: "Real end-of-day prices only: entries and exits are approximated from the daily range." },
  fno_rolling: { label: "REAL PREMIUMS", text: "Dhan's recorded expired-option candles (5-minute path) with the strategies run on the index candles. Real prices, synthetic spread; the limits are in the result's notes." },
  fno_sim: { label: "SIMULATED", text: "Black-Scholes prices, not real quotes. Shows the logic, not the money." },
};
export function btLabel(kind: string | undefined): { label: string; text: string } {
  const k = (kind || "").toLowerCase();
  return BT_RELIABILITY[k] || BT_RELIABILITY[`fno_${k}`] || { label: "UNKNOWN", text: "Unknown kind of backtest." };   // the service writes kind as SIM / EOD / REPLAY
}

/** One journal event split into the strategy that raised it (a column of its own on the Signals page) and the remaining detail. */
export function splitEvent(e: Record<string, any>): { strategy: string; rest: Record<string, any> } {
  const { id: _id, ts: _ts, kind: _kind, underlying: _u, strategy, ...rest } = e || {};
  const sg = typeof strategy === "string" && strategy ? strategy : "";
  return { strategy: sg, rest };
}

/** Put/call ratio style reading for the chain header. */
export function pcrReading(pcr: number | null | undefined): string {
  if (pcr == null || Number.isNaN(pcr)) return "-";
  return pcr >= 1.3 ? "bullish tilt (many puts written)" : pcr <= 0.7 ? "bearish tilt (many calls written)" : "balanced";
}
