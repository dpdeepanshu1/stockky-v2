// E122: pure helpers behind the Real Trade "Trade History" tab (no React, no fetch, so node can test them).
// Input: the closed-trade records of GET /positions/{mode}/records. Output: numbers and words for the screen.
import type { TradeRecord } from "../../realTradeApi";

export const HISTORY_RANGES = [
  { id: "today", label: "Today", days: 1 },
  { id: "7d", label: "7 days", days: 7 },
  { id: "30d", label: "30 days", days: 30 },
  { id: "60d", label: "60 days", days: 60 },
] as const;
export type HistoryRange = (typeof HISTORY_RANGES)[number]["id"];

const fin = (n: unknown): n is number => typeof n === "number" && isFinite(n);

/** Net P&L when the ledger knows it, gross otherwise, null when neither is known (such a trade is listed but never counted as a win or loss). */
export function pnlOf(r: TradeRecord): number | null {
  if (fin(r.net_pnl)) return r.net_pnl;
  if (fin(r.gross_pnl)) return r.gross_pnl;
  return null;
}

export interface HistorySummary {
  trades: number; counted: number; wins: number; losses: number; breakeven: number;
  winRate: number | null; net: number; charges: number;
  best: number | null; worst: number | null;
  avgWin: number | null; avgLoss: number | null; payoff: number | null;
}

export function summarize(records: TradeRecord[]): HistorySummary {
  let wins = 0, losses = 0, breakeven = 0, net = 0, charges = 0, sumW = 0, sumL = 0;
  let best: number | null = null, worst: number | null = null;
  for (const r of records) {
    if (fin(r.charges)) charges += r.charges;
    const p = pnlOf(r);
    if (p == null) continue;
    net += p;
    best = best == null ? p : Math.max(best, p);
    worst = worst == null ? p : Math.min(worst, p);
    if (p > 0.5) { wins += 1; sumW += p; } else if (p < -0.5) { losses += 1; sumL += p; } else breakeven += 1;
  }
  const counted = wins + losses + breakeven;
  const avgWin = wins ? sumW / wins : null;
  const avgLoss = losses ? sumL / losses : null;
  return {
    trades: records.length, counted, wins, losses, breakeven,
    winRate: counted ? (wins / counted) * 100 : null,
    net, charges, best, worst, avgWin, avgLoss,
    payoff: avgWin != null && avgLoss != null && avgLoss !== 0 ? avgWin / Math.abs(avgLoss) : null,
  };
}

/** Oldest first; records without a close time go first. */
const oldestFirst = (records: TradeRecord[]) =>
  records.slice().sort((a, b) => String(a.closed_at ?? "").localeCompare(String(b.closed_at ?? "")));
/** Newest closed trade first (the table order). Records without a close time go last. */
export const newestFirst = (records: TradeRecord[]): TradeRecord[] =>
  records.slice().sort((a, b) => String(b.closed_at ?? "").localeCompare(String(a.closed_at ?? "")));

/** Running total of P&L in close order. Trades with no known P&L add nothing. */
export function equityCurve(records: TradeRecord[]): number[] {
  let run = 0;
  const out: number[] = [];
  for (const r of oldestFirst(records)) {
    const p = pnlOf(r);
    if (p == null) continue;
    run += p;
    out.push(Math.round(run * 100) / 100);
  }
  return out;
}

/** IST calendar day ("YYYY-MM-DD") of an ISO timestamp: the IST day rolls over at 18:30 UTC, not at midnight UTC. */
export function istDay(ts: string | null | undefined): string | null {
  if (!ts) return null;
  const t = Date.parse(ts);
  if (!isFinite(t)) return null;
  return new Date(t + 5.5 * 3600_000).toISOString().slice(0, 10);
}

/** "today" keeps only today's IST day; the other ranges are already cut by the server (days=N). */
export function inRange(records: TradeRecord[], range: HistoryRange, now: Date = new Date()): TradeRecord[] {
  if (range !== "today") return records;
  const today = istDay(now.toISOString());
  return records.filter((r) => (r.exit_day_ist ?? istDay(r.closed_at)) === today);
}

export const daysFor = (range: HistoryRange): number => HISTORY_RANGES.find((x) => x.id === range)?.days ?? 7;

/** "+₹1,234" / "-₹1,234" / "₹0": the sign is always in the text so a gain or loss never depends on colour. */
export function signedInr(n: number | null | undefined): string {
  if (typeof n !== "number" || !isFinite(n)) return "—";
  const v = Math.round(n);
  return `${v > 0 ? "+" : v < 0 ? "-" : ""}₹${Math.abs(v).toLocaleString("en-IN")}`;
}
export const priceLabel = (n: number | null | undefined): string => (fin(n) ? `₹${n.toFixed(2)}` : "—");
export const pctLabel = (n: number | null | undefined, digits = 2): string => (fin(n) ? `${n > 0 ? "+" : ""}${n.toFixed(digits)}%` : "—");

/** "42 min" / "3h 05m" / "2 days". */
export function heldLabel(r: Pick<TradeRecord, "held_minutes" | "held_days">): string {
  const m = r.held_minutes;
  if (fin(m)) {
    if (m < 60) return `${Math.max(0, Math.round(m))} min`;
    if (m < 60 * 24) return `${Math.floor(m / 60)}h ${String(Math.round(m % 60)).padStart(2, "0")}m`;
  }
  if (fin(r.held_days)) return r.held_days === 0 ? "same day" : r.held_days === 1 ? "1 day" : `${r.held_days} days`;
  return "—";
}

const REASONS: Record<string, string> = {
  TARGET: "Target hit", STOP: "Stop hit", STOP_LOSS: "Stop hit", TRAIL: "Trailing stop", TRAILING_STOP: "Trailing stop",
  EOD: "End-of-day square-off", EOD_SQUAREOFF: "End-of-day square-off", MANUAL: "Closed by you", TIME: "Time exit",
};
/** An exit reason as words ("STOP_LOSS" -> "Stop hit"); unknown codes are shown tidied, never hidden. */
export function reasonLabel(code: string | null | undefined): string {
  if (!code) return "Reason not recorded";
  const k = String(code).toUpperCase();
  if (REASONS[k]) return REASONS[k];
  const t = k.replace(/_/g, " ").toLowerCase();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

export interface Insight { title: string; body: string; tone: "good" | "warn" | "info" }

/**
 * "Pattern read": plain-words observations about what already happened. It needs 3+ counted trades, names a group only when it has 3+
 * trades, and never calls anything a forecast or advice. Pure arithmetic over the records.
 */
export function insights(records: TradeRecord[]): Insight[] {
  const s = summarize(records);
  if (s.counted < 3) return [];
  const out: Insight[] = [];
  if (s.winRate != null) {
    out.push({ title: "Hit rate", tone: s.winRate >= 50 ? "good" : "info",
      body: `${s.wins} of ${s.counted} closed trades were winners (${s.winRate.toFixed(0)}%).` });
  }
  if (s.payoff != null) {
    out.push({ title: "Win size vs loss size", tone: s.payoff >= 1 ? "good" : "warn",
      body: `The average winner was ${s.payoff.toFixed(2)}× the average loser (${signedInr(s.avgWin)} vs ${signedInr(s.avgLoss)}).` });
  }
  const byReason = new Map<string, { n: number; net: number }>();
  for (const r of records) {
    const p = pnlOf(r);
    if (p == null || !r.exit_reason) continue;
    const g = byReason.get(r.exit_reason) ?? { n: 0, net: 0 };
    g.n += 1; g.net += p;
    byReason.set(r.exit_reason, g);
  }
  const groups = [...byReason.entries()].filter(([, g]) => g.n >= 3).sort((a, b) => a[1].net - b[1].net);
  if (groups.length) {
    const [code, g] = groups[0];
    if (g.net < 0) out.push({ title: "Costliest exit", tone: "warn", body: `${reasonLabel(code)} closed ${g.n} trades for ${signedInr(g.net)} in total.` });
    const [bc, bg] = groups[groups.length - 1];
    if (bg.net > 0) out.push({ title: "Best exit", tone: "good", body: `${reasonLabel(bc)} closed ${bg.n} trades for ${signedInr(bg.net)} in total.` });
  }
  if (s.charges > 0 && s.net !== 0) {
    out.push({ title: "Charges", tone: "info", body: `Brokerage and taxes on these trades came to ₹${Math.round(s.charges).toLocaleString("en-IN")}.` });
  }
  return out;
}

const csvCell = (v: unknown): string => {
  const t = v == null ? "" : String(v);
  return /[",\n\r]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
};
/** The shown rows as CSV (one header line, escaped commas / quotes / new lines). */
export function toCsv(records: TradeRecord[]): string {
  const cols: [string, (r: TradeRecord) => unknown][] = [
    ["symbol", (r) => r.symbol], ["closed_at", (r) => r.closed_at], ["quantity", (r) => r.quantity],
    ["entry_price", (r) => r.entry_price], ["exit_price", (r) => r.exit_price], ["exit_reason", (r) => r.exit_reason],
    ["held", (r) => heldLabel(r)], ["gross_pnl", (r) => r.gross_pnl], ["charges", (r) => r.charges], ["net_pnl", (r) => r.net_pnl],
    ["return_pct", (r) => r.return_pct], ["tier", (r) => r.tier_name], ["source", (r) => r.source_tab],
  ];
  return [cols.map(([h]) => h).join(","), ...records.map((r) => cols.map(([, f]) => csvCell(f(r))).join(","))].join("\n");
}
