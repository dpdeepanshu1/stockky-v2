// frontend/src/shortShadowView.ts  (short selling step 6)
// Pure helpers for the "Short selling (shadow)" card on the Position Stocks Board tab (no imports, no network). Shape of GET /short/shadow on
// position-stocks-service (orders/short_entry.summary). NO order exists behind any of this: every row is a short Stockky WOULD have taken.
// Covered by shortShadow.test.mjs.

export type ShortStatus = "OPEN" | "TARGET" | "STOP" | "EOD" | "EXPIRED";
export interface ShortShadowRow {
  id: number; day: string; symbol: string; window_source: string | null; pct_change: number | null;
  entry_price: number | null; stop_price: number | null; target_price: number | null; quantity: number | null;
  status: ShortStatus | string; exit_price: number | null; gross_pnl: number | null; net_pnl: number | null; net_pnl_pct: number | null;
  low_water: number | null; high_water: number | null; board_conviction: number | null; market_state: string | null; created_at: string | null;
}
export interface ShortShadowDoc {
  mode: string; total: number; open: number; closed: number; target: number; stop: number; eod: number; expired: number;
  net_pnl: number; gross_pnl: number; win_rate_pct: number | null; days_with_results: number;
  s2_progress: { would_be_shorts: number; needed: number; days: number; weeks_needed: number };
  gate_counters: { cycles?: number; scanned?: number; recorded?: number; refused?: number; by_code?: Record<string, number> };
  recent: ShortShadowRow[];
  sell_probe?: SellProbeDoc;
}

/** E108 (T6): the last Dhan SELL probe, as position-stocks sends it under GET /short/shadow. */
export interface SellProbeDoc { ran: boolean; verdict: string; line: string; at?: string; symbol?: string; passed: boolean; rules?: string[]; required_for_live: boolean }

/** One plain line for the Short selling panel: what the probe says and what it means for going live. */
export function sellProbeView(p: SellProbeDoc | null | undefined): { tone: "good" | "bad" | "warn"; text: string } {
  if (!p) return { tone: "warn", text: "Dhan SELL probe (T6): status not available." };
  if (!p.ran) return { tone: "warn", text: p.required_for_live ? "Dhan SELL probe (T6): not run yet. A live short is refused until it passes (job: Dhan SELL probe)." : "Dhan SELL probe (T6): not run yet (not required: SHORT_LIVE_REQUIRES_PROBE is off)." };
  if (p.passed) return { tone: "good", text: `${p.line}${p.symbol ? ` (${p.symbol})` : ""}. A live short is allowed by this check.` };
  return { tone: "bad", text: `${p.line}. ${p.required_for_live ? "A live short is refused until the probe passes." : ""}`.trim() };
}

/** The plan's S2 gate: at least 100 would-be shorts over about 4 weeks (20 trading days), positive after charges. */
export const S2_DAYS_NEEDED = 20;

export type ShortKind = "unavailable" | "off" | "waiting" | "active";
export interface ShortShadowView {
  kind: ShortKind;
  headline: string;
  progress: { done: number; needed: number; days: number; daysNeeded: number; pct: number };
  s2Ready: boolean;               // the three numbers pass; a person still has to check the result is not from one or two days
  s2Missing: string[];            // plain reasons the gate is not passed yet
  refusals: { code: string; count: number }[];   // gate refusal counters since the service started, biggest first (at most 6)
  rows: ShortShadowRow[];         // newest first, as the service sends them
}

const num = (v: unknown): number | null => (typeof v === "number" && isFinite(v) ? v : null);

export function shortShadowView(doc: ShortShadowDoc | null | undefined): ShortShadowView {
  const base: ShortShadowView = {
    kind: "unavailable", headline: "Short shadow data is not available.",
    progress: { done: 0, needed: 100, days: 0, daysNeeded: S2_DAYS_NEEDED, pct: 0 }, s2Ready: false, s2Missing: [], refusals: [], rows: [],
  };
  if (!doc) return base;
  const needed = num(doc.s2_progress?.needed) ?? 100;
  const done = num(doc.s2_progress?.would_be_shorts) ?? num(doc.closed) ?? 0;
  const days = num(doc.s2_progress?.days) ?? num(doc.days_with_results) ?? 0;
  const net = num(doc.net_pnl) ?? 0;
  const pct = needed > 0 ? Math.max(0, Math.min(100, Math.round((done / needed) * 100))) : 0;
  const missing: string[] = [];
  if (done < needed) missing.push(`${needed - done} more would-be shorts (${done} of ${needed})`);
  if (days < S2_DAYS_NEEDED) missing.push(`${S2_DAYS_NEEDED - days} more trading days with a result (${days} of ${S2_DAYS_NEEDED})`);
  if (done > 0 && !(net > 0)) missing.push("a positive result after charges (it is " + (net < 0 ? "negative" : "zero") + ")");
  const by = doc.gate_counters?.by_code || {};
  const refusals = Object.entries(by).filter(([, n]) => typeof n === "number" && n > 0).map(([code, count]) => ({ code, count: count as number }))
    .sort((a, b) => b.count - a.count || (a.code < b.code ? -1 : 1)).slice(0, 6);
  const rows = Array.isArray(doc.recent) ? doc.recent : [];
  const common = { progress: { done, needed, days, daysNeeded: S2_DAYS_NEEDED, pct }, s2Ready: done >= needed && days >= S2_DAYS_NEEDED && net > 0, s2Missing: missing, refusals, rows };
  if (doc.mode !== "shadow" && doc.mode !== "live" && doc.total === 0) {
    return { ...base, ...common, kind: "off", headline: "Off. SHORT_MODE is off, so no short is considered. Switch it to shadow on the Switches page to start recording." };
  }
  if (doc.total === 0 && doc.mode === "live") {
    return { ...base, ...common, kind: "waiting", headline: "LIVE. Real short orders are allowed, at most SHORT_LIVE_QTY_CAP shares each. No short has passed every gate yet; live shorts show under Positions, not here." };
  }
  if (doc.total === 0) {
    return { ...base, ...common, kind: "waiting", headline: "Shadow is on. No short has passed every gate yet. That is normal on most days: the gates are strict and all fail closed." };
  }
  const stopText = doc.mode === "shadow" ? "" : doc.mode === "live" ? " (SHORT_MODE is live: these are older shadow rows; live shorts show under Positions)" : " (SHORT_MODE is off now: only leftover rows are shown)";
  return { ...base, ...common, kind: "active", headline: `${doc.total} would-be short${doc.total === 1 ? "" : "s"} recorded, ${doc.closed} closed, ${doc.open} open${stopText}.` };
}

/** Tone of a row's status for the card: TARGET good, STOP bad, others neutral. */
export function statusTone(s: string | null | undefined): "good" | "bad" | "warn" | undefined {
  return s === "TARGET" ? "good" : s === "STOP" ? "bad" : s === "OPEN" || s === "EOD" ? "warn" : undefined;
}

/** Rupees with a sign and no decimals, "-" when missing. */
export function rupees(v: number | null | undefined): string {
  if (typeof v !== "number" || !isFinite(v)) return "-";
  const r = Math.round(v);
  return `${r > 0 ? "+" : r < 0 ? "-" : ""}₹${Math.abs(r).toLocaleString("en-IN")}`;
}

/** Short exposure line of the Market Weather card: what the profile does to SHORTS in a state (the short dial, position-stocks only). */
export function shortExposureLine(state: string | null | undefined, dials: Record<string, number | string | null> | undefined): string {
  if (!state) return "";
  const v = dials ? dials["MARKET_STATE_SHORT_MULT"] : undefined;
  if (v == null) return "Shorts: left at the saved short size (position-stocks only; longs are not touched).";
  const n = Number(v);
  if (!isFinite(n)) return "";
  if (n <= 0) return "Shorts: OFF today (position-stocks only; longs are not touched).";
  return `Shorts: ${Math.round(n * 100)}% of the saved short size (position-stocks only; longs are not touched).`;
}


/** The "Short selling" switches of position-stocks for the calibration page: every flag in the catalog group "Short selling", SHORT_MODE first. */
export interface ShortSwitch { key: string; title: string; value: string; recommended: string | null; pending: boolean }
export function shortSwitchRows(flags: { key: string; title: string; group: string; effective: string | null; running: string | null; recommended: string | null; pending_restart?: boolean }[] | null | undefined): ShortSwitch[] {
  const rows = (flags || []).filter((f) => f && f.group === "Short selling")
    .map((f) => ({ key: f.key, title: f.title, value: String(f.effective ?? f.running ?? "-"), recommended: f.recommended ?? null, pending: !!f.pending_restart }));
  return rows.sort((a, b) => (a.key === "SHORT_MODE" ? -1 : b.key === "SHORT_MODE" ? 1 : a.key.localeCompare(b.key)));
}
/** One-click off is offered only while SHORT_MODE is not already off. */
export const canSwitchShortOff = (rows: ShortSwitch[]): boolean => rows.some((r) => r.key === "SHORT_MODE" && r.value !== "off");
