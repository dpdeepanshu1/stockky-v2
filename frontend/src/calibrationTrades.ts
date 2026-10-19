// group 442: pure helpers of the Real Money Calibration page (no network, no imports) so node can test them: calibration.test.mjs

export interface TradeRow {
  service: "real-trade" | "position-stocks"; id: string; symbol: string; entry: Date; exit: Date; day: string; heldMin: number; pnl: number; retPct: number | null; reason: string; qty: number | null;
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

function mkRow(service: TradeRow["service"], id: any, symbol: any, opened: any, closed: any, pnl: any, ret: any, reason: any, qty: any): TradeRow | null {
  const o = parseTs(opened), c = parseTs(closed), p = num(pnl);
  if (!o || !c || p == null) return null;
  return { service, id: String(id ?? ""), symbol: String(symbol || "?"), entry: o, exit: c, day: istDay(c), heldMin: Math.max(0, (c.getTime() - o.getTime()) / 60000), pnl: p, retPct: num(ret), reason: String(reason || "-"), qty: num(qty) };
}

export function tradesFromRealTrade(doc: any): TradeRow[] {
  const recs: any[] = Array.isArray(doc) ? doc : doc?.records || [];
  const out: TradeRow[] = [];
  for (const r of recs) {
    if (!r || typeof r !== "object") continue;
    const pnl = r.net_pnl != null ? r.net_pnl : r.gross_pnl;
    const x = mkRow("real-trade", r.position_id, r.symbol, r.opened_at, r.closed_at, pnl, r.return_pct, r.exit_reason, r.quantity);
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
    const ret = ep && xp != null ? ((xp - ep) / ep) * 100 : null;
    const x = mkRow("position-stocks", r.id, r.symbol, r.opened_at, r.closed_at, r.realized_pnl, ret, r.exit_reason || st.toLowerCase(), r.quantity);
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

export function tradesCsv(rows: TradeRow[]): string {
  const esc = (v: string) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  const head = ["day_ist", "service", "symbol", "entry_ist", "exit_ist", "held_min", "pnl", "return_pct", "reason", "qty"];
  const lines = rows.slice().sort((a, b) => a.entry.getTime() - b.entry.getTime()).map((r) =>
    [r.day, r.service, r.symbol, `${istDay(r.entry)} ${istHm(r.entry)}`, `${istDay(r.exit)} ${istHm(r.exit)}`, r.heldMin.toFixed(0), r.pnl.toFixed(2), r.retPct == null ? "" : r.retPct.toFixed(3), r.reason, r.qty ?? ""].map((x) => esc(String(x))).join(","));
  return [head.join(","), ...lines].join("\n") + "\n";
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

