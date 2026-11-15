// E539 (redesign round 6): pure helpers for the F&O option-chain table. No imports, no network. The table draws these rows; nothing here places an order.
export interface ChainSideIn { oi?: number | null; prev_oi?: number | null; volume?: number | null; iv?: number | null; delta?: number | null; ltp?: number | null }
export interface ChainStrikeIn { strike: number; ce: ChainSideIn | null; pe: ChainSideIn | null }
export interface ChainRow {
  strike: number; atm: boolean;
  /** a call is in the money below the spot, a put above it; the table shades those cells and says so in the legend */
  ceItm: boolean; peItm: boolean;
  ce: ChainSideIn | null; pe: ChainSideIn | null;
  /** open interest as a whole percent of the largest OI in the rows shown (CE and PE share one scale, so the two sides compare) */
  ceOiPct: number; peOiPct: number;
}

/** how far from the ATM strike the table reaches (index points): Nifty 600, Sensex 2000 (the same window as before the redesign). */
export const chainWindow = (und: string): number => (und === "NIFTY" ? 600 : 2000);

export function atmStrike(strikes: ChainStrikeIn[] | null | undefined, spot: number): number | null {
  let best: number | null = null;
  for (const r of strikes || []) if (best == null || Math.abs(r.strike - spot) < Math.abs(best - spot)) best = r.strike;
  return best;
}

const pct = (v: number | null | undefined, max: number): number => (!max || v == null || !Number.isFinite(v) || v <= 0 ? 0 : Math.max(0, Math.min(100, Math.round((v / max) * 100))));

export function chainRows(strikes: ChainStrikeIn[] | null | undefined, spot: number, und: string): ChainRow[] {
  const atm = atmStrike(strikes, spot);
  if (atm == null) return [];
  const w = chainWindow(und);
  const kept = (strikes || []).filter((r) => Math.abs(r.strike - atm) <= w).slice().sort((a, b) => a.strike - b.strike);
  let max = 0;
  for (const r of kept) max = Math.max(max, r.ce?.oi ?? 0, r.pe?.oi ?? 0);
  return kept.map((r) => ({
    strike: r.strike, atm: r.strike === atm,
    ceItm: r.strike < spot && r.strike !== atm, peItm: r.strike > spot && r.strike !== atm,
    ce: r.ce, pe: r.pe, ceOiPct: pct(r.ce?.oi, max), peOiPct: pct(r.pe?.oi, max),
  }));
}

/** index of the ATM row inside the rows shown (-1 when there is none): the table scrolls it into view. */
export const atmIndex = (rows: ChainRow[]): number => rows.findIndex((r) => r.atm);

export const chainN0 = (v: number | null | undefined): string => (v == null || !Number.isFinite(v) ? "-" : Math.round(v).toLocaleString("en-IN"));
export const chainN2 = (v: number | null | undefined): string => (v == null || !Number.isFinite(v) ? "-" : v.toFixed(2));

// round 550 (P1): the "VIX rank" stat of the Chain tab: today's India VIX against its own recorded history (the F&O service's `vix_rank` key). Pure; a missing or half-ready reply reads as words, never as 0.
export interface VixRankIn { vix?: number | null; rank?: number | null; percentile?: number | null; days?: number | null; need?: number | null; pending?: number | null; ready?: boolean | null; note?: string | null }
export interface VixRankView { value: string; sub: string }
const fin = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export function vixRankView(v: VixRankIn | null | undefined): VixRankView {
  if (!v || typeof v !== "object") return { value: "-", sub: "not reported by this service" };
  const days = fin(v.days) ? v.days : 0, need = fin(v.need) ? v.need : 20;
  if (v.ready === true && fin(v.rank)) {
    const above = fin(v.percentile) ? `, above ${Math.round(v.percentile)}% of ${days} days` : `, ${days} days`;
    return { value: `${Math.round(v.rank)} / 100`, sub: `VIX ${fin(v.vix) ? v.vix.toFixed(2) : "-"}${above}` };
  }
  if (!fin(v.vix)) return { value: "-", sub: "India VIX not available" };
  if (fin(v.pending) && v.pending > 0) return { value: "-", sub: `reading history, ${v.pending} days left` };
  return { value: "-", sub: `${days} of ${need} days recorded` };
}

// round 552 (P3): numbers the F&O service already computes (summary.atm_iv, straddle, expected_move_pct) and the change in open interest (oi - prev_oi, both sent per strike), shown on the Chain tab.
// Pure. A missing number is a dash, never 0; a negative number uses the real minus sign so it cannot be mistaken for the dash.
const MINUS = "\u2212";
export const signedN0 = (v: number | null | undefined): string => (!fin(v) ? "-" : v > 0 ? `+${Math.round(v).toLocaleString("en-IN")}` : v < 0 ? `${MINUS}${Math.abs(Math.round(v)).toLocaleString("en-IN")}` : "0");

/** open interest now minus open interest at the previous close, for one side of one strike; None unless both are real numbers */
export const oiChange = (s: ChainSideIn | null | undefined): number | null => (s && fin(s.oi) && fin(s.prev_oi) ? s.oi - s.prev_oi : null);

export interface OiChangeTotals { ce: number | null; pe: number | null; strikes: number }
/** the change in call and put OI summed over the strikes shown (a strike without both numbers is left out; no strike at all = null, not 0) */
export function oiChangeTotals(rows: ChainRow[]): OiChangeTotals {
  let ce: number | null = null, pe: number | null = null, n = 0;
  for (const r of rows) {
    const c = oiChange(r.ce), p = oiChange(r.pe);
    if (c != null) { ce = (ce ?? 0) + c; n++; }
    if (p != null) pe = (pe ?? 0) + p;
  }
  return { ce, pe, strikes: n };
}

export interface ChainStat { value: string; sub: string }
/** ATM IV and the ATM straddle with the move and range it implies, from the service's `summary` */
export function expectedMoveView(sm: Record<string, any> | null | undefined, spot: number | null | undefined): { iv: ChainStat; straddle: ChainStat } {
  const s = sm || {};
  const iv: ChainStat = fin(s.atm_iv) && s.atm_iv > 0 ? { value: `${s.atm_iv.toFixed(2)}%`, sub: fin(s.atm_strike) ? `average of the ${s.atm_strike} call and put` : "at the money" } : { value: "-", sub: "not reported" };
  const st = fin(s.straddle) && s.straddle > 0 ? s.straddle : null;
  if (st == null) return { iv, straddle: { value: "-", sub: "not reported" } };
  const move = fin(s.expected_move_pct) && s.expected_move_pct > 0 ? `\u00b1${s.expected_move_pct.toFixed(2)}%` : null;
  const range = fin(spot) && spot > 0 ? `${Math.round(spot - st).toLocaleString("en-IN")} to ${Math.round(spot + st).toLocaleString("en-IN")}` : null;
  return { iv, straddle: { value: st.toFixed(2), sub: [move, range].filter(Boolean).join(", ") || "call + put at the money" } };
}

export interface ChartPoint { strike: number; atm: boolean; ce: number | null; pe: number | null }
const posIv = (v: number | null | undefined): number | null => (fin(v) && v > 0 ? v : null);       // an IV of 0 or less is "not computed", not a volatility
export const oiChangePoints = (rows: ChainRow[]): ChartPoint[] => rows.map((r) => ({ strike: r.strike, atm: r.atm, ce: oiChange(r.ce), pe: oiChange(r.pe) }));
export const ivPoints = (rows: ChainRow[]): ChartPoint[] => rows.map((r) => ({ strike: r.strike, atm: r.atm, ce: posIv(r.ce?.iv), pe: posIv(r.pe?.iv) }));

const xAt = (i: number, n: number, w: number, pad: number): number => (n <= 1 ? w / 2 : pad + ((w - 2 * pad) * i) / (n - 1));
export interface OiBar { x: number; y: number; w: number; h: number; side: "ce" | "pe"; strike: number; v: number }
/** diverging bars (up = OI added, down = OI removed), call and put side by side per strike, one shared scale; no data = no bars and max 0 */
export function oiBars(pts: ChartPoint[], w: number, h: number, pad = 8): { bars: OiBar[]; zeroY: number; max: number } {
  let max = 0;
  for (const p of pts) max = Math.max(max, fin(p.ce) ? Math.abs(p.ce) : 0, fin(p.pe) ? Math.abs(p.pe) : 0);
  const zeroY = h / 2, bars: OiBar[] = [];
  if (!(max > 0)) return { bars, zeroY, max: 0 };
  const slot = pts.length > 1 ? (w - 2 * pad) / (pts.length - 1) : w / 2, bw = Math.max(1, Math.min(14, slot * 0.4));
  pts.forEach((p, i) => {
    const cx = xAt(i, pts.length, w, pad);
    for (const side of ["ce", "pe"] as const) {
      const v = p[side];
      if (!fin(v) || v === 0) continue;
      const bh = (Math.abs(v) / max) * (h / 2 - 3);
      bars.push({ x: side === "ce" ? cx - bw - 0.5 : cx + 0.5, y: v > 0 ? zeroY - bh : zeroY, w: bw, h: bh, side, strike: p.strike, v });
    }
  });
  return { bars, zeroY, max };
}

/** one SVG path per side; a missing value breaks the line (no value is drawn as 0); also the low / high of the scale and the x of the ATM strike */
export function ivPaths(pts: ChartPoint[], w: number, h: number, pad = 8): { ce: string; pe: string; lo: number | null; hi: number | null; atmX: number | null } {
  const vals: number[] = [];
  for (const p of pts) { if (fin(p.ce)) vals.push(p.ce); if (fin(p.pe)) vals.push(p.pe); }
  if (!vals.length) return { ce: "", pe: "", lo: null, hi: null, atmX: null };
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const y = (v: number) => (hi === lo ? h / 2 : pad + (h - 2 * pad) * (1 - (v - lo) / (hi - lo)));
  const build = (key: "ce" | "pe"): string => {
    let d = "", open = false;
    pts.forEach((p, i) => {
      const v = p[key];
      if (!fin(v)) { open = false; return; }
      const x = xAt(i, pts.length, w, pad).toFixed(1), yy = y(v).toFixed(1);
      d += open ? ` L${x} ${yy}` : `${d ? " " : ""}M${x} ${yy} L${x} ${yy}`;
      open = true;
    });
    return d;
  };
  const ai = pts.findIndex((p) => p.atm);
  return { ce: build("ce"), pe: build("pe"), lo, hi, atmX: ai >= 0 ? xAt(ai, pts.length, w, pad) : null };
}
