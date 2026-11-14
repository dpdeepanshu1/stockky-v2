// E539 (redesign round 6): pure helpers for the F&O option-chain table. No imports, no network. The table draws these rows; nothing here places an order.
export interface ChainSideIn { oi?: number | null; volume?: number | null; iv?: number | null; delta?: number | null; ltp?: number | null }
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
