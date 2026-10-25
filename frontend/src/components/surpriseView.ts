// E152: summary tiles for the Surprise Momentum tab (pure, no React: tested by plain node in surpriseView.test.mjs).
// Values come from the rows and scan meta the tab already holds. A score or RVOL of exactly 0 stays 0; only non-numbers mean "missing".
export type SurpriseKpi = { label: string; value: string; sub?: string; tone?: "up" | "down" | "warn" | "info" | "flat" };
type Row = { symbol?: string; score?: number | null; rvol?: number | null; tier?: string | null };
type Meta = { static_loaded?: number | null; quotes_ok?: number | null; universe_scanned?: number | null; elapsed_sec?: number | null };

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const show = (v: unknown): string => { const n = num(v); return n == null ? "—" : String(n); };

export function surpriseKpis(rows: Row[] | null | undefined, meta: Meta | null | undefined): SurpriseKpi[] {
  const list = Array.isArray(rows) ? rows : [];
  if (!list.length && !meta) return [];
  let top: { s: number; sym: string } | null = null;
  let hot: { r: number; sym: string } | null = null;
  let breakout = 0, building = 0;
  for (const r of list) {
    const s = num(r.score);
    if (s != null && (top == null || s > top.s)) top = { s, sym: r.symbol || "" };
    const v = num(r.rvol);
    if (v != null && (hot == null || v > hot.r)) hot = { r: v, sym: r.symbol || "" };
    if (r.tier === "breakout") breakout++; else if (r.tier === "building") building++;
  }
  const q = num(meta?.quotes_ok), u = num(meta?.universe_scanned);
  const noQuotes = u != null && u > 0 && q === 0;
  return [
    { label: "Hits", value: String(list.length), sub: `breakout ${breakout} · building ${building}`, tone: "info" },
    { label: "Top score", value: top ? String(Math.round(top.s)) : "—", sub: top ? top.sym : "no scored hit yet", tone: "flat" },
    { label: "Top RVOL", value: hot ? `${hot.r.toFixed(1)}x` : "—", sub: hot ? hot.sym : "no volume reading yet", tone: "flat" },
    { label: "Scan", value: u != null ? `${show(q)}/${u}` : "—", sub: noQuotes ? "No live quotes - prices unreliable" : `quotes ok / symbols scanned · baselines ${show(meta?.static_loaded)}`, tone: noQuotes ? "warn" : "flat" },
  ];
}

// E161: one table row as display text (pure). A missing or non-numeric value is a dash, never "NaN", "₹NaN" or a silent 0
// (the old cells ran Number(x).toFixed on everything). A real 0 stays 0. Direction and state are spelled in words / glyphs, never colour only.
export type SurpriseRowView = {
  score: { text: string; word: string; tone: "buy" | "acc" | "warn" | "sell" } | null;
  tier: { label: string; glyph: string; tone: "buy" | "warn" };
  change: { text: string; dir: "up" | "down" | "flat" | null };
  rvol: string; slope: string | null; target: string; stop: string;
};
const fx = (v: unknown, d: number): string => { const n = num(v); return n == null ? "—" : n.toFixed(d); };
const inr = (v: unknown): string => { const n = num(v); return n == null ? "—" : `₹${n.toFixed(2)}`; };
export function surpriseRowView(s: { score?: unknown; tier?: unknown; change_pct?: unknown; rvol?: unknown; rvol_slope?: unknown; target_1?: unknown; trailing_stop?: unknown }): SurpriseRowView {
  const sc = num(s.score);
  const r = sc == null ? null : Math.max(0, Math.min(100, Math.round(sc)));
  const score = r == null ? null : { text: `${r}/100`, word: r >= 70 ? "Strong" : r >= 55 ? "Good" : r >= 40 ? "Mixed" : "Weak", tone: (r >= 70 ? "buy" : r >= 55 ? "acc" : r >= 40 ? "warn" : "sell") as "buy" | "acc" | "warn" | "sell" };
  const building = s.tier === "building";
  const c = num(s.change_pct);
  const change = c == null ? { text: "—", dir: null } : { text: `${c > 0 ? "+" : ""}${c.toFixed(2)}%`, dir: (c > 0 ? "up" : c < 0 ? "down" : "flat") as "up" | "down" | "flat" };
  const sl = num(s.rvol_slope);
  return {
    score,
    tier: building ? { label: "Building", glyph: "◆", tone: "warn" } : { label: "Breakout", glyph: "▲", tone: "buy" },
    change,
    rvol: num(s.rvol) == null ? "—" : `${fx(s.rvol, 2)}x`,
    slope: sl != null && sl > 0 ? `↑${sl.toFixed(2)}` : null,
    target: inr(s.target_1), stop: inr(s.trailing_stop),
  };
}
