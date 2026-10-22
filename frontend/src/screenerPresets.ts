// E29 (2026-10-22): one-click fundamentals presets for the scanner ("profit up, debt low" etc).
// Pure helpers (no React, no network) so they can be tested with
//   node --experimental-strip-types screenerPresets.test.mjs   (Node >= 22.6)
// Units follow the fundamental service: growth and ROE are PERCENT (15 = 15%), debt_to_equity is a RATIO (0.5 = 0.5x),
// pe_ratio is a plain multiple. A row with a missing metric a preset needs does NOT match that preset (no guessing).
// The data is a single snapshot, so "debt low" is a level check, not a "debt falling over time" check.
// A preset only narrows the table view; it never changes scores, decisions or trades.

export interface FundamentalsLike {
  revenue_growth?: number | null;
  earnings_growth?: number | null;
  roe?: number | null;
  debt_to_equity?: number | null;
  pe_ratio?: number | null;
}

export interface Preset {
  id: string;
  label: string;
  hint: string;
  test: (m: FundamentalsLike) => boolean;
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export const PRESETS: Preset[] = [
  {
    id: "profit_up_debt_low",
    label: "Profit up, debt low",
    hint: "Earnings growth above 10%, revenue growth above 0%, debt/equity at most 1",
    test: (m) => {
      const e = num(m.earnings_growth), r = num(m.revenue_growth), d = num(m.debt_to_equity);
      return e !== null && r !== null && d !== null && e > 10 && r > 0 && d <= 1;
    },
  },
  {
    id: "quality",
    label: "Quality",
    hint: "ROE at least 15% and debt/equity at most 0.5",
    test: (m) => {
      const roe = num(m.roe), d = num(m.debt_to_equity);
      return roe !== null && d !== null && roe >= 15 && d <= 0.5;
    },
  },
  {
    id: "growth",
    label: "Fast growth",
    hint: "Revenue and earnings growth both at least 15%",
    test: (m) => {
      const e = num(m.earnings_growth), r = num(m.revenue_growth);
      return e !== null && r !== null && e >= 15 && r >= 15;
    },
  },
  {
    id: "value",
    label: "Cheap + healthy",
    hint: "P/E between 0 and 20 (exclusive of 0), ROE at least 12%",
    test: (m) => {
      const pe = num(m.pe_ratio), roe = num(m.roe);
      return pe !== null && roe !== null && pe > 0 && pe <= 20 && roe >= 12;
    },
  },
];

export function getPreset(id: string | null | undefined): Preset | null {
  return PRESETS.find((p) => p.id === id) ?? null;
}

/** Rows matching the preset; `id` null/unknown returns the rows untouched. */
export function applyPreset<T extends { fundamental_metrics?: FundamentalsLike | null }>(rows: T[], id: string | null | undefined): T[] {
  const p = getPreset(id);
  if (!p) return rows;
  return rows.filter((r) => (r.fundamental_metrics ? p.test(r.fundamental_metrics) : false));
}

/** How many rows lack fundamentals entirely (so the UI can say why a preset looks short). */
export function missingFundamentals<T extends { fundamental_metrics?: FundamentalsLike | null }>(rows: T[]): number {
  return rows.filter((r) => !r.fundamental_metrics).length;
}
