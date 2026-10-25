// frontend/src/swingView.ts  (E144)
// Pure helpers for the "Swing lane" card on the Real Money Calibration page (no runtime imports, no network, never throws). Shape of GET /swing/status and
// GET /swing/ledger on real-trade-service (main.py swing_status / swing_ledger; types in realTradeApi.ts). In SHADOW mode every row is a trade the lane WOULD have
// taken: no order exists. The verdict numbers come from the backend (swing/stats.py); this file only words them. Covered by swing.test.mjs.
import type { SwingLedgerRow, SwingStatusDoc } from "./realTradeApi";

/** The verdict's own thresholds (swing/engine.py report(): min_n=30 closed trades on min_days=20 entry days). */
export const SWING_MIN_TRADES = 30;
export const SWING_MIN_DAYS = 20;

export type SwingKind = "unavailable" | "off" | "shadow" | "live";
export type Tone = "good" | "warn" | "bad" | undefined;

export interface SwingView {
  kind: SwingKind;
  headline: string;
  verdict: { code: string; tone: Tone; text: string };
  progress: { done: number; needed: number; days: number; daysNeeded: number; pct: number };
  problems: string[];                                    // wiring.problems, as the backend words them
  pauses: { mode: string; why: string }[];               // only the modes that are paused
  scan: { tone: Tone; text: string };
  statusCounts: { status: string; count: number }[];     // shadow ledger by status, biggest first
  openPositions: { mode: string; symbol: string; qty: string; entry: string; stop: string }[];
  planned: { symbol: string; rank: string; signalDay: string; stopPct: string }[];
  numbers: { label: string; value: string }[];           // net / gross mean, median, win rate, stopped, gap-throughs (only when something closed)
}

const isNum = (v: unknown): v is number => typeof v === "number" && isFinite(v);
const pct = (v: unknown, digits = 2): string => (isNum(v) ? `${v > 0 ? "+" : ""}${v.toFixed(digits)}%` : "-");
const plain = (v: unknown, digits = 2): string => (isNum(v) ? v.toFixed(digits) : "-");

/** Rupees, no decimals, "-" when missing. */
export function inr(v: unknown): string {
  return isNum(v) ? `₹${Math.round(v).toLocaleString("en-IN")}` : "-";
}

export function verdictView(code: unknown, reasons: unknown): { code: string; tone: Tone; text: string } {
  const c = typeof code === "string" && code ? code : "UNKNOWN";
  const why = Array.isArray(reasons) ? reasons.filter((r): r is string => typeof r === "string" && !!r).join("; ") : "";
  if (c === "USEFUL") return { code: c, tone: "good", text: "USEFUL: the 90% interval of the net mean per trade stays above zero. Check it is not from one or two days before trusting it." };
  if (c === "NO_EDGE") return { code: c, tone: "bad", text: `NO_EDGE: ${why || "the net mean per trade is not clearly above zero after costs."}` };
  if (c === "NOT_ENOUGH_DATA") return { code: c, tone: "warn", text: `NOT_ENOUGH_DATA (the normal answer for weeks): ${why || `needs ${SWING_MIN_TRADES} closed trades on ${SWING_MIN_DAYS} entry days`}` };
  return { code: c, tone: "warn", text: "No verdict available." };
}

function scanLine(doc: SwingStatusDoc): { tone: Tone; text: string } {
  const s = doc.scan;
  const list = doc.scan_universe ? `, stock list: ${doc.scan_universe}` : "";
  const uni = `${isNum(doc.universe_size) ? doc.universe_size : "?"} stocks (${doc.universe_source || "unknown source"})`;
  if (!s || !s.day) return { tone: "warn", text: `No evening scan stored yet. It runs after the scan time on a trading day. Universe: ${uni}.` };
  const text = `Newest scan for the ${s.day} session${s.summary ? `: ${s.summary}` : ""}${list}. Universe: ${uni}.`;
  return { tone: s.ok === false ? "bad" : undefined, text };
}

export function swingView(doc: SwingStatusDoc | null | undefined): SwingView {
  const empty: SwingView = {
    kind: "unavailable", headline: "Swing lane data is not available.",
    verdict: { code: "UNKNOWN", tone: "warn", text: "No verdict available." },
    progress: { done: 0, needed: SWING_MIN_TRADES, days: 0, daysNeeded: SWING_MIN_DAYS, pct: 0 },
    problems: [], pauses: [], scan: { tone: "warn", text: "-" }, statusCounts: [], openPositions: [], planned: [], numbers: [],
  };
  if (!doc || typeof doc !== "object") return empty;
  if (doc.error) return { ...empty, headline: `Swing lane status failed: ${String(doc.error).slice(0, 160)}` };

  const sh = doc.shadow && typeof doc.shadow === "object" ? doc.shadow : null;
  const done = isNum(sh?.n) ? sh!.n : 0;
  const days = isNum(sh?.days) ? sh!.days : 0;
  const p = { done, needed: SWING_MIN_TRADES, days, daysNeeded: SWING_MIN_DAYS, pct: Math.max(0, Math.min(100, Math.round((done / SWING_MIN_TRADES) * 100))) };
  const verdict = sh && !sh.error ? verdictView(sh.verdict, sh.reasons) : verdictView(undefined, undefined);

  const mode = String(doc.mode || "").toLowerCase();
  const kind: SwingKind = mode === "off" ? "off" : mode === "live" ? "live" : "shadow";
  const headline = kind === "off"
    ? "Off. SWING_MODE is off: no scan runs and nothing is recorded. Switch it to shadow on the Switches page to start recording."
    : kind === "live"
      ? `LIVE. Real CNC swing orders are allowed in DEMO${doc.live_real_enabled ? " and REAL" : " (REAL stays blocked until SWING_LIVE_REAL_ENABLED is true)"}, at most ${isNum(doc.max_positions) ? doc.max_positions : "?"} positions of ${inr(doc.max_trade_value)} each, held up to ${isNum(doc.max_hold_days) ? doc.max_hold_days : "?"} sessions.`
      : "Shadow. The lane records the picks it WOULD have taken. No order is placed.";

  const pauses = Object.entries(doc.pause || {}).filter(([, v]) => v && v.paused).map(([m, v]) => ({ mode: m, why: String(v.why || "paused") }));
  const by = sh?.by_status && typeof sh.by_status === "object" ? sh.by_status : {};
  const statusCounts = Object.entries(by).filter(([, n]) => isNum(n) && n > 0).map(([status, count]) => ({ status, count: count as number }))
    .sort((a, b) => b.count - a.count || (a.status < b.status ? -1 : 1));
  const openPositions = (Array.isArray(doc.open_positions) ? doc.open_positions : []).map((x) => ({
    mode: String(x.mode), symbol: String(x.symbol), qty: isNum(x.qty) ? String(x.qty) : "-", entry: plain(x.entry), stop: plain(x.stop),
  }));
  const planned = (Array.isArray(doc.planned) ? doc.planned : []).map((r) => ({
    symbol: String(r.symbol), rank: isNum(r.rank) ? String(r.rank) : "-", signalDay: r.signal_day || "-", stopPct: isNum(r.stop_pct) ? `${r.stop_pct.toFixed(1)}%` : "-",
  }));
  const numbers: { label: string; value: string }[] = [];
  if (sh && !sh.error && done > 0) {
    numbers.push({ label: "net mean per trade", value: pct(sh.net?.mean) }, { label: "net median", value: pct(sh.net?.median) },
      { label: "gross mean", value: pct(sh.gross?.mean) }, { label: "win rate", value: isNum(sh.net?.win_pct) ? `${sh.net.win_pct}%` : "-" },
      { label: "stopped out", value: isNum(sh.stopped_pct) ? `${sh.stopped_pct}%` : "-" }, { label: "gapped through the stop", value: String(isNum(sh.gap_through) ? sh.gap_through : 0) });
    const ci = sh.net?.ci90;
    numbers.push({ label: "90% interval (net mean)", value: Array.isArray(ci) && ci.length === 2 ? `${pct(ci[0])} to ${pct(ci[1])}` : "needs more entry days" });
  }
  return {
    kind, headline, verdict, progress: p, problems: Array.isArray(doc.wiring?.problems) ? doc.wiring.problems.filter((s) => typeof s === "string") : [],
    pauses, scan: scanLine(doc), statusCounts, openPositions, planned, numbers,
  };
}

/** Tone of a ledger row's net result. */
export function netTone(v: number | null | undefined): Tone {
  return isNum(v) ? (v > 0 ? "good" : v < 0 ? "bad" : undefined) : undefined;
}

export interface SwingLedgerLine { id: number; symbol: string; status: string; signalDay: string; entryDay: string; exitDay: string; why: string; net: string; netTone: Tone }

/** Newest-first ledger rows as table lines: what happened to each pick (exit reason, or the reason it was skipped), and the net result after costs. */
export function ledgerLines(rows: SwingLedgerRow[] | null | undefined): SwingLedgerLine[] {
  return (Array.isArray(rows) ? rows : []).map((r) => ({
    id: r.id, symbol: r.symbol, status: r.status, signalDay: r.signal_day || "-", entryDay: r.entry_day || "-", exitDay: r.exit_day || "-",
    why: r.skip_reason || r.exit_reason || "", net: pct(r.net_pct), netTone: netTone(r.net_pct),
  }));
}
