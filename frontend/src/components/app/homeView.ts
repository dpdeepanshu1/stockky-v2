// E536 (redesign round 4): pure helpers for the Home screen. No import, no call, no state. The market strip and the positions row only
// FORMAT numbers the app already fetched; every direction is spelled with a sign or an arrow as well as a colour (never colour-only).

const fin = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);

export type Tone = "up" | "down" | "flat";
export const toneOf = (n: number | null | undefined): Tone => (!fin(n) || n === 0 ? "flat" : n > 0 ? "up" : "down");

/** "+₹1,250", "-₹340", "₹0": the sign is in the text, Indian digit grouping, never a minus on a zero. */
export function signedRupees(n: number): string {
  const r = Math.round(n);
  const body = Math.abs(r).toLocaleString("en-IN");
  return r > 0 ? `+₹${body}` : r < 0 ? `-₹${body}` : "₹0";
}

/** "+12.30 (+0.45%)" for an index move. A missing number gives "—" (a half-loaded feed must not show 0.00 as if it were a flat day). */
export function indexDelta(change: number | null | undefined, pct: number | null | undefined): string {
  if (!fin(change) || !fin(pct)) return "—";
  const s = (n: number, d: number) => `${n > 0 ? "+" : ""}${n.toFixed(d)}`;
  return `${s(change, 2)} (${s(pct, 2)}%)`;
}

export function indexValue(price: number | null | undefined): string {
  return fin(price) ? price.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—";
}

export type MoodTone = "up" | "down" | "flat";
export function moodView(mood: string | null | undefined, score: number | null | undefined): { word: string; tone: MoodTone; scoreText: string } {
  const word = mood === "BULLISH" ? "Bullish" : mood === "BEARISH" ? "Bearish" : "Neutral";
  const tone: MoodTone = mood === "BULLISH" ? "up" : mood === "BEARISH" ? "down" : "flat";
  return { word, tone, scoreText: fin(score) ? `${Math.round(score)}/100` : "—" };
}

// ── "Your positions today" ──
export interface HomePosition { source: "Auto Trade" | "Position Stocks"; symbol: string; side: "LONG" | "SHORT"; qty: number; pnl: number | null; pnlPct: number | null }

interface RtLike { symbol: string; qty_open: number; unrealized_pnl: number | null; pnl_pct: number | null }
interface PsLike { symbol: string; status: string; quantity: number; side?: string | null; unrealized_pnl: number | null; unrealized_pnl_pct: number | null }

/** Real Trade rows: only those with shares still open. */
export function fromRealTrade(rows: RtLike[] | null | undefined): HomePosition[] {
  return (rows ?? [])
    .filter((p) => p && fin(p.qty_open) && p.qty_open > 0)
    .map((p) => ({ source: "Auto Trade" as const, symbol: p.symbol, side: "LONG" as const, qty: p.qty_open, pnl: fin(p.unrealized_pnl) ? p.unrealized_pnl : null, pnlPct: fin(p.pnl_pct) ? p.pnl_pct : null }));
}

/** Position Stocks rows: only status OPEN; a short is labelled SHORT. */
export function fromScalp(rows: PsLike[] | null | undefined): HomePosition[] {
  return (rows ?? [])
    .filter((r) => r && r.status === "OPEN")
    .map((r) => ({ source: "Position Stocks" as const, symbol: r.symbol, side: String(r.side ?? "LONG").toUpperCase() === "SHORT" ? ("SHORT" as const) : ("LONG" as const), qty: r.quantity, pnl: fin(r.unrealized_pnl) ? r.unrealized_pnl : null, pnlPct: fin(r.unrealized_pnl_pct) ? r.unrealized_pnl_pct : null }));
}

export interface PositionsSummary { count: number; pnl: number | null; pnlText: string; tone: Tone; missing: number }

/** Total open P&L over the rows that have a number; `missing` counts rows whose P&L is not known yet (the total says so instead of hiding it). */
export function summarizePositions(rows: Pick<HomePosition, "pnl">[]): PositionsSummary {
  const known = rows.filter((r) => r.pnl !== null);
  const pnl = known.length ? known.reduce((a, r) => a + (r.pnl as number), 0) : null;
  return { count: rows.length, pnl, pnlText: pnl === null ? "—" : `${pnl > 0 ? "▲ " : pnl < 0 ? "▼ " : ""}${signedRupees(pnl)}`, tone: toneOf(pnl), missing: rows.length - known.length };
}

/** Biggest absolute P&L first; rows without a P&L last; stable by symbol. */
export function sortPositions<T extends Pick<HomePosition, "pnl" | "symbol">>(rows: T[]): T[] {
  return [...rows].sort((a, b) => Math.abs(b.pnl ?? -1) - Math.abs(a.pnl ?? -1) || a.symbol.localeCompare(b.symbol));
}

export function pctText(n: number | null | undefined): string {
  if (!fin(n)) return "—";
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}
