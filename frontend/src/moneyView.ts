// Round 270: pure helpers for the Dhan money block shown on Real Trade, Position Stocks and F&O (no React, no fetch).
//  - boardView: the 3-way capital board (GET /capital/board on Real Trade / Position Stocks, GET /fno/capital on F&O) as rows a card can draw
//  - openCharges / afterCharges: what closing an OPEN stock position now would cost, so a page can show P&L before AND after charges
import { legCharges } from "./chargesRates.ts";

export const SERVICE_LABEL: Record<string, string> = { real_trade: "Real Trade", position_stocks: "Position Stocks", fno: "F&O Options" };
const ORDER = ["real_trade", "position_stocks", "fno"];

export interface BoardDoc {
  ts?: number; day?: string; mode?: string; active?: string[]; pcts?: Record<string, number>; amounts?: Record<string, number>;
  total_equity?: number; free_cash?: number; exposure?: Record<string, number>; scores?: Record<string, number | null>; trades?: Record<string, number>;
}
/** Shape of GET /capital/board (Real Trade, Position Stocks) and GET /fno/capital (F&O adds the real_sizing_* / demo_capital fields). */
export interface BoardStatus {
  mode?: string; this_service?: string; effective_share_pct?: number; why?: string; fixed_share_pct?: number; board?: BoardDoc | null;
  active_for_real?: boolean; real_sizing_capital?: number; real_sizing_source?: string; demo_capital?: number; switch?: string;
}
export interface BoardRow { key: string; label: string; pct: number | null; amount: number | null; exposure: number; room: number | null; active: boolean; trades: number; score: number | null; isThis: boolean }
export interface BoardView {
  /** live = this service really sizes from the board; shadow = shown only; off = switch off here; missing = no fresh board file */
  state: "live" | "shadow" | "off" | "missing";
  total: number | null; freeCash: number | null; rows: BoardRow[]; thisRow: BoardRow | null; ageMin: number | null; notes: string[];
  /** F&O only: the rupees F&O sizes a trade from right now (REAL = board amount, else the virtual DEMO capital) and which one it is */
  sizing: { amount: number; label: string } | null;
}

function num(v: unknown): number | null {
  const x = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : NaN;
  return Number.isFinite(x) ? x : null;
}

export function boardView(st: BoardStatus | null | undefined, nowSec: number = Date.now() / 1000): BoardView {
  const empty: BoardView = { state: "missing", total: null, freeCash: null, rows: [], thisRow: null, ageMin: null, notes: [], sizing: null };
  if (!st) return empty;
  let sizing: BoardView["sizing"] = null;
  if (st.demo_capital != null || st.real_sizing_capital != null) {
    sizing = st.active_for_real && (num(st.real_sizing_capital) ?? 0) > 0
      ? { amount: num(st.real_sizing_capital) as number, label: "REAL: from the capital board" }
      : { amount: num(st.demo_capital) ?? 0, label: "DEMO: virtual capital, no real money" };
  }
  const own = String(st.mode || "off").toLowerCase();
  const doc = st.board || null;
  if (own === "off") return { ...empty, sizing, state: "off", notes: ["CAPITAL_ALLOC_MODE is off on this service, so no capital board is read or written. Set it to shadow (show only) or live."] };
  if (!doc || !doc.pcts) return { ...empty, sizing, notes: ["No fresh capital board yet. Real Trade writes it once its Dhan balance sync runs (it needs CAPITAL_ALLOC_MODE shadow or live there)."] };
  const state: BoardView["state"] = own === "live" && String(doc.mode || "").toLowerCase() === "live" ? "live" : "shadow";
  const active = new Set(doc.active || []);
  const rows: BoardRow[] = ORDER.filter((k) => k in (doc.pcts || {}) || k in (doc.amounts || {})).map((k) => {
    const amount = num(doc.amounts?.[k]);
    const exposure = Math.max(0, num(doc.exposure?.[k]) ?? 0);
    return { key: k, label: SERVICE_LABEL[k] || k, pct: num(doc.pcts?.[k]), amount, exposure, room: amount == null ? null : Math.max(0, amount - exposure),
             active: active.has(k), trades: num(doc.trades?.[k]) ?? 0, score: num(doc.scores?.[k]), isThis: k === st.this_service };
  });
  const ts = num(doc.ts);
  const notes: string[] = [];
  if (state === "shadow") notes.push(`Shown only: sizing on this service still uses its fixed share${st.fixed_share_pct != null ? ` (${st.fixed_share_pct}%)` : ""}. ${st.why || ""}`.trim());
  return { state, total: num(doc.total_equity), freeCash: num(doc.free_cash), rows, thisRow: rows.find((r) => r.isThis) || null, ageMin: ts == null ? null : Math.max(0, Math.round((nowSec - ts) / 60)), notes, sizing };
}

export interface OpenLeg { side?: string | null; qty: number; entry: number; price: number | null | undefined; delivery?: boolean; entryAlreadyPaid?: boolean }
/** Estimated total charges (Rs) to open and close this stock position at `price` now. null when there is no live price. A short sells first, so its entry leg is the SELL.
 *  entryAlreadyPaid: a demat holding bought earlier, only the closing leg is still to pay. Estimate only: DP and contract-note rounding are not included. */
export function openCharges(l: OpenLeg): number | null {
  const q = Number(l.qty), e = Number(l.entry), p = Number(l.price);
  if (!(q > 0) || !(e > 0) || !(p > 0)) return null;
  const short = String(l.side || "LONG").toUpperCase() === "SHORT";
  const del = !!l.delivery;
  const entryLeg = l.entryAlreadyPaid ? 0 : legCharges(q * e, short ? "SELL" : "BUY", del).total;
  const exitLeg = legCharges(q * p, short ? "BUY" : "SELL", del).total;
  return Math.round((entryLeg + exitLeg) * 100) / 100;
}
export function afterCharges(pnl: number | null | undefined, charges: number | null | undefined): number | null {
  if (pnl == null || charges == null || !Number.isFinite(pnl) || !Number.isFinite(charges)) return null;
  return Math.round((pnl - charges) * 100) / 100;
}
