// group338: pure helpers behind the "Top picks" cards (best 3 to 5). Both services (position-stocks GET /top-picks, real-trade GET /top-picks/{mode})
// return the same card shape; this turns a response into display-ready values. No React, no I/O, never throws: a missing / odd field becomes
// null and the card shows "-". Display only: it places no order.
export interface PickCard {
  pick: number; symbol: string; name: string; sector: string | null; side: "bull" | "bear" | null;
  source: "engine" | "board"; status: string | null; outcome: string | null; score: number | null;
  conviction: number | null; rank: number | null; ltp: number | null; changePct: number | null; zoneScore: number | null;
  entry: number | null; stop: number | null; target: number | null; rr: number | null;
  extended: boolean; inPosition: boolean; spark: number[]; reasons: string[];
}
export interface PicksView {
  picks: PickCard[]; note: string | null; asOf: number | null; marketOpen: boolean; engineFresh: boolean;
  enginePicks: number; previewPicks: number;
}

const fin = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const numOrNull = (v: unknown): number | null => (fin(v) ? v : null);

export function normalizePicks(body: any): PicksView {
  const raw: any[] = Array.isArray(body?.picks) ? body.picks : [];
  const picks: PickCard[] = raw.filter(p => p && typeof p.symbol === "string" && p.symbol).map((p, i) => ({
    pick: fin(p.pick) ? p.pick : i + 1, symbol: p.symbol, name: typeof p.name === "string" && p.name ? p.name : p.symbol,
    sector: typeof p.sector === "string" ? p.sector : null, side: p.side === "bull" || p.side === "bear" ? p.side : null,
    source: p.source === "engine" ? "engine" : "board", status: typeof p.status === "string" ? p.status : null,
    outcome: typeof p.outcome === "string" ? p.outcome : null, score: numOrNull(p.score), conviction: numOrNull(p.conviction),
    rank: numOrNull(p.rank), ltp: numOrNull(p.ltp), changePct: numOrNull(p.change_pct), zoneScore: numOrNull(p.zone_score),
    entry: numOrNull(p.entry), stop: numOrNull(p.stop), target: numOrNull(p.target), rr: numOrNull(p.rr),
    extended: !!p.extended, inPosition: !!p.in_position,
    spark: Array.isArray(p.spark) ? p.spark.filter(fin) : [], reasons: Array.isArray(p.reasons) ? p.reasons.filter((r: unknown) => typeof r === "string") : [],
  }));
  return {
    picks, note: typeof body?.note === "string" ? body.note : null, asOf: fin(body?.as_of) ? body.as_of * 1000 : null,
    marketOpen: !!body?.market_open, engineFresh: !!body?.engine_fresh,
    enginePicks: fin(body?.engine_picks) ? body.engine_picks : 0, previewPicks: fin(body?.preview_picks) ? body.preview_picks : 0,
  };
}

/** The badge a card shows: what the engine did with the pick, or that it is only a board preview. */
export function pickBadge(p: Pick<PickCard, "source" | "status" | "outcome">): { label: string; tone: "good" | "wait" | "bad" | "preview" } {
  const o = p.outcome || "";
  if (o === "ENTERED") return { label: "Entered", tone: "good" };
  if (o.startsWith("SKIPPED:")) return { label: `Skipped · ${o.slice(8).toLowerCase().replace(/_/g, " ")}`, tone: "bad" };
  if (o.startsWith("PLACEMENT_FAILED")) return { label: "Order failed", tone: "bad" };
  if (p.source === "board") return { label: "Board preview", tone: "preview" };
  if (o === "NOT_NEEDED") return { label: "Not needed", tone: "wait" };
  if (o === "NOT_PLACED") return { label: "Not placed", tone: "wait" };
  if (p.status === "SELECTED") return { label: "First in line", tone: "wait" };
  if (p.status === "RESERVE") return { label: "Reserve", tone: "wait" };
  return { label: "Shortlisted", tone: "wait" };
}

/** Points of a sparkline in a w x h box (min..max scaled, last point right). Empty for fewer than 2 values. */
export function sparkPath(vals: number[], w: number, h: number): string {
  const v = vals.filter(fin);
  if (v.length < 2) return "";
  const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
  return v.map((x, i) => `${(i / (v.length - 1) * w).toFixed(1)},${(h - ((x - lo) / span) * (h - 2) - 1).toFixed(1)}`).join(" ");
}

/** Where price sits between stop (0) and target (1), clamped; null when any of the three is missing or the range is not stop < target. */
export function levelPosition(stop: number | null, price: number | null, target: number | null): number | null {
  if (!fin(stop) || !fin(price) || !fin(target) || target <= stop) return null;
  return Math.max(0, Math.min(1, (price - stop) / (target - stop)));
}
