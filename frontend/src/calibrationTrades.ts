// group 442: pure helpers of the Real Money Calibration page (no network, no imports) so node can test them: calibration.test.mjs

export interface TradeRow {
  service: "real-trade" | "position-stocks"; id: string; symbol: string; entry: Date; exit: Date; day: string; heldMin: number; pnl: number; retPct: number | null; reason: string; qty: number | null;
  // E213: what a loss analysis needs and the first 10 CSV columns never carried. Every field is null when the service did not record it.
  side: "LONG" | "SHORT";
  entryPrice: number | null; exitPrice: number | null;
  stopPct: number | null;          // stop distance the trade was OPENED with, % of entry (never the trailed stop)
  targetPct: number | null;        // target distance it was opened with (position-stocks only)
  maxGainPct: number | null;       // best move in the trade's own direction since entry, % (>= 0)
  maxDrawdownPct: number | null;   // worst move against the trade since entry, % (<= 0)
  entrySlipPct: number | null;     // + = filled WORSE than the signal price
  exitSlipPct: number | null;      // fill vs the exit trigger / stop price: + = better, - = worse
  charges: number | null; stopBasis: string | null;
}
type Extra = Partial<Pick<TradeRow, "side" | "entryPrice" | "exitPrice" | "stopPct" | "targetPct" | "maxGainPct" | "maxDrawdownPct" | "entrySlipPct" | "exitSlipPct" | "charges" | "stopBasis">>;

/** % move from `from` to `to` in the trade's own direction (a short gains when price falls). null when `from` is unusable. */
export function directionalPct(side: "LONG" | "SHORT", from: number | null, to: number | null): number | null {
  if (from == null || to == null || !(from > 0)) return null;
  const m = ((to - from) / from) * 100;
  return side === "SHORT" ? -m : m;
}
/** Loss (or gain) in units of the stop the trade was opened with: -1 = lost exactly one stop. null without a stop or a return. */
export function rMultiple(r: Pick<TradeRow, "retPct" | "stopPct">): number | null {
  return r.retPct != null && r.stopPct != null && r.stopPct > 0 ? r.retPct / r.stopPct : null;
}
/** real-trade ghost record: P&L exactly 0 and no return % (a booked-nowhere position, e.g. HDFCLIFE 2026-10-20). A real 0.00 carries a return %. */
export function isUnbooked(r: any): boolean {
  const p = r?.net_pnl != null ? r.net_pnl : r?.gross_pnl;
  return num(p) === 0 && num(r?.return_pct) == null;
}

const IST_MS = 330 * 60 * 1000;
export function istDay(d: Date): string { return new Date(d.getTime() + IST_MS).toISOString().slice(0, 10); }
export function istHm(d: Date): string { return new Date(d.getTime() + IST_MS).toISOString().slice(11, 16); }

function parseTs(v: any): Date | null {
  if (v == null || v === "") return null;
  const s = String(v);
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s.replace(" ", "T") + "Z");   // a naive timestamp is UTC, as in the backends
  return Number.isNaN(d.getTime()) ? null : d;
}
function num(v: any): number | null {
  const n = typeof v === "number" ? v : parseFloat(v);
  return Number.isFinite(n) ? n : null;
}

function mkRow(service: TradeRow["service"], id: any, symbol: any, opened: any, closed: any, pnl: any, ret: any, reason: any, qty: any, extra: Extra = {}): TradeRow | null {
  const o = parseTs(opened), c = parseTs(closed), p = num(pnl);
  if (!o || !c || p == null) return null;
  return { service, id: String(id ?? ""), symbol: String(symbol || "?"), entry: o, exit: c, day: istDay(c), heldMin: Math.max(0, (c.getTime() - o.getTime()) / 60000), pnl: p, retPct: num(ret), reason: String(reason || "-"), qty: num(qty),
    side: extra.side ?? "LONG", entryPrice: extra.entryPrice ?? null, exitPrice: extra.exitPrice ?? null, stopPct: extra.stopPct ?? null, targetPct: extra.targetPct ?? null,
    maxGainPct: extra.maxGainPct ?? null, maxDrawdownPct: extra.maxDrawdownPct ?? null, entrySlipPct: extra.entrySlipPct ?? null, exitSlipPct: extra.exitSlipPct ?? null,
    charges: extra.charges ?? null, stopBasis: extra.stopBasis ?? null };
}

export function tradesFromRealTrade(doc: any): TradeRow[] {
  const recs: any[] = Array.isArray(doc) ? doc : doc?.records || [];
  const out: TradeRow[] = [];
  for (const r of recs) {
    if (!r || typeof r !== "object") continue;
    const pnl = r.net_pnl != null ? r.net_pnl : r.gross_pnl;
    if (isUnbooked(r)) continue;   // E213
    const side = String(r.side || "").toUpperCase() === "SHORT" ? "SHORT" : "LONG";
    const x = mkRow("real-trade", r.position_id, r.symbol, r.opened_at, r.closed_at, pnl, r.return_pct, r.exit_reason, r.quantity, {
      side, entryPrice: num(r.entry_price), exitPrice: num(r.exit_price), stopPct: num(r.stop_pct), maxGainPct: num(r.max_gain_pct), maxDrawdownPct: num(r.max_drawdown_pct),
      entrySlipPct: num(r.entry_slippage_pct), exitSlipPct: num(r.exit_slippage_pct), charges: num(r.charges), stopBasis: typeof r.stop_basis === "string" ? r.stop_basis : null });
    if (x) out.push(x);
  }
  return out;
}

const PS_SKIP = ["ERROR", "REJECTED", "PENDING_RECONCILE", "OPEN", "PENDING"];
export function tradesFromPositionStocks(doc: any): TradeRow[] {
  const recs: any[] = Array.isArray(doc) ? doc : doc?.trades || [];
  const out: TradeRow[] = [];
  for (const r of recs) {
    if (!r || typeof r !== "object") continue;
    const st = String(r.status || "").toUpperCase();
    if (PS_SKIP.includes(st) || r.exit_price == null || r.realized_pnl == null) continue;
    const ep = num(r.entry_price), xp = num(r.exit_price);
    const side = String(r.side || "").toUpperCase() === "SHORT" ? "SHORT" : "LONG";
    const ret = directionalPct(side, ep, xp);   // E213: was (exit-entry)/entry for every row, so a short's return had the wrong sign
    // the service reports excursions as price moves vs entry (high above, low below); for a short the best move is the low
    const hi = num(r.max_gain_pct), lo = num(r.max_drawdown_pct);
    const sig = num(r.signal_price), sp = num(r.stop_price);
    const stopExit = /stop/i.test(String(r.exit_reason || st));
    const x = mkRow("position-stocks", r.id, r.symbol, r.opened_at, r.closed_at, r.realized_pnl, ret, r.exit_reason || st.toLowerCase(), r.quantity, {
      side, entryPrice: ep, exitPrice: xp, stopPct: num(r.adaptive_stop_pct), targetPct: num(r.adaptive_target_pct),
      maxGainPct: side === "SHORT" ? (lo == null ? null : -lo) : hi, maxDrawdownPct: side === "SHORT" ? (hi == null ? null : -hi) : lo,
      entrySlipPct: directionalPct(side, sig, ep),   // + = filled worse than the signal (a long paying above it, a short selling below it)
      exitSlipPct: stopExit ? directionalPct(side, sp, xp) : null,   // vs the last stored stop (may have been trailed); + = better
      stopBasis: typeof r.stop_basis === "string" ? r.stop_basis : null });
    if (x) out.push(x);
  }
  return out;
}

export interface DayTotal { day: string; n: number; wins: number; losses: number; net: number; best: number; worst: number; firstEntry: string; lastExit: string }

export function dayTotals(rows: TradeRow[]): DayTotal[] {
  const by = new Map<string, TradeRow[]>();
  for (const r of rows) by.set(r.day, [...(by.get(r.day) || []), r]);
  const out: DayTotal[] = [];
  for (const [day, rs] of by) {
    const pn = rs.map((r) => r.pnl);
    out.push({
      day, n: rs.length, wins: pn.filter((x) => x > 0).length, losses: pn.filter((x) => x < 0).length, net: pn.reduce((a, b) => a + b, 0), best: Math.max(...pn), worst: Math.min(...pn),
      firstEntry: istHm(new Date(Math.min(...rs.map((r) => r.entry.getTime())))), lastExit: istHm(new Date(Math.max(...rs.map((r) => r.exit.getTime())))),
    });
  }
  return out.sort((a, b) => (a.day < b.day ? 1 : -1));
}

export interface Bucket { key: string; n: number; net: number; avg: number; winPct: number }
export function bucketBy(rows: TradeRow[], keyf: (r: TradeRow) => string): Bucket[] {
  const m = new Map<string, TradeRow[]>();
  for (const r of rows) { const k = keyf(r); m.set(k, [...(m.get(k) || []), r]); }
  return [...m.entries()].map(([key, rs]) => {
    const net = rs.reduce((a, r) => a + r.pnl, 0);
    return { key, n: rs.length, net, avg: net / rs.length, winPct: (100 * rs.filter((r) => r.pnl > 0).length) / rs.length };
  }).sort((a, b) => (a.key < b.key ? -1 : 1));
}

// E213: the first 10 columns are unchanged (scripts/daily_trade_report.py and service_onoff_advice.py read them); the rest are new.
export const CSV_HEAD = ["day_ist", "service", "symbol", "entry_ist", "exit_ist", "held_min", "pnl", "return_pct", "reason", "qty",
  "side", "entry_price", "exit_price", "stop_pct", "target_pct", "r_mult", "best_gain_pct", "worst_dd_pct", "entry_slip_pct", "exit_slip_pct", "charges", "stop_basis"];

export function tradesCsv(rows: TradeRow[]): string {
  const esc = (v: string) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  const f = (v: number | null, d: number) => (v == null ? "" : v.toFixed(d));
  const lines = rows.slice().sort((a, b) => a.entry.getTime() - b.entry.getTime()).map((r) => {
    const rm = rMultiple(r);
    return [r.day, r.service, r.symbol, `${istDay(r.entry)} ${istHm(r.entry)}`, `${istDay(r.exit)} ${istHm(r.exit)}`, r.heldMin.toFixed(0), r.pnl.toFixed(2), f(r.retPct, 3), r.reason, r.qty ?? "",
      r.side, f(r.entryPrice, 2), f(r.exitPrice, 2), f(r.stopPct, 3), f(r.targetPct, 3), f(rm, 2), f(r.maxGainPct, 2), f(r.maxDrawdownPct, 2), f(r.entrySlipPct, 3), f(r.exitSlipPct, 3), f(r.charges, 2), r.stopBasis ?? ""]
      .map((x) => esc(String(x))).join(",");
  });
  return [CSV_HEAD.join(","), ...lines].join("\n") + "\n";
}

export function relTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "-";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function fmtDur(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "-";
  if (sec < 90) return `${Math.round(sec)} s`;
  if (sec < 5400) return `${Math.round(sec / 60)} min`;
  return `${(sec / 3600).toFixed(1)} h`;
}

