// E153: summary tiles for the IPO Tracker tab (pure, no React: tested by plain node in ipoView.test.mjs).
// Values come from the rows the tab already holds. 0 stays 0 (a listing that closed flat is a reading, not "missing").
export type IpoKpi = { label: string; value: string; sub?: string; tone?: "up" | "down" | "warn" | "info" | "flat" };
type Row = { symbol?: string; stage?: string | null; decision?: string | null; listing_date?: string | null; listing_pop_pct?: number | null };

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const ISO = /^\d{4}-\d{2}-\d{2}/;

export function ipoKpis(rows: Row[] | null | undefined, totalScanned?: number | null): IpoKpi[] {
  const list = Array.isArray(rows) ? rows : [];
  if (!list.length) return [];
  const upcoming = list.filter((r) => r.stage === "upcoming" || r.stage === "pre_listing");
  const listed = list.filter((r) => r.stage === "listing_day" || r.stage === "listed");
  const awaiting = list.filter((r) => r.stage === "no_data_yet" || r.stage === "error");
  const buys = list.filter((r) => r.decision === "BUY NOW" || r.decision === "PREPARE TO BUY");
  const dates = upcoming.map((r) => r.listing_date || "").filter((d) => ISO.test(d)).sort();
  const pops = listed.map((r) => num(r.listing_pop_pct)).filter((v): v is number => v != null);
  const avg = pops.length ? pops.reduce((a, b) => a + b, 0) / pops.length : null;
  const scanned = num(totalScanned);
  return [
    { label: "Tracked", value: String(list.length), sub: scanned != null && scanned !== list.length ? `of ${scanned} scanned` : "in this window", tone: "info" },
    { label: "Upcoming", value: String(upcoming.length), sub: dates.length ? `next lists ${dates[0].slice(0, 10)}` : "no listing date yet", tone: "info" },
    { label: "Avg listing gain", value: avg == null ? "—" : `${avg > 0 ? "+" : ""}${avg.toFixed(1)}%`, sub: avg == null ? "no listed IPO with a gain figure" : `${pops.length} listed IPO${pops.length === 1 ? "" : "s"}`, tone: avg == null || avg === 0 ? "flat" : avg > 0 ? "up" : "down" },
    { label: "Buy signals", value: String(buys.length), sub: "BUY NOW or PREPARE TO BUY", tone: "flat" },
    { label: "Awaiting data", value: String(awaiting.length), sub: awaiting.length ? "no price data yet or a data error" : "every row has data", tone: awaiting.length ? "warn" : "flat" },
  ];
}

// E162: one IPO row face as display text (pure). Missing values are a dash or are left out, never "₹undefined", "Lists undefined",
// "Day undefined" or "₹0". A real 0 gain stays 0.0%. Direction and state are spelled in words / glyphs, never colour only.
export type IpoRowView = {
  stage: { text: string; tone: "up" | "warn" | "down" | "flat" };
  issue: string; cmp: string | null;
  gain: { text: string; dir: "up" | "down" | "flat" } | null;
  score: { text: string; word: string; tone: "buy" | "acc" | "warn" | "sell" } | null;
  gmp: { text: string; dir: "up" | "down" | "flat" } | null;
};
type IpoRow = { stage?: unknown; listing_date?: unknown; days_since_listing?: unknown; issue_price?: unknown; current_price?: unknown; current_vs_issue_pct?: unknown; ipo_score?: unknown; gmp?: unknown; gmp_pct_of_issue?: unknown };
const dirOf = (n: number): "up" | "down" | "flat" => (n > 0 ? "up" : n < 0 ? "down" : "flat");
const sgn = (n: number, d: number): string => `${n > 0 ? "+" : ""}${n.toFixed(d)}`;
export function ipoRowView(r: IpoRow): IpoRowView {
  const date = typeof r.listing_date === "string" && ISO.test(r.listing_date) ? r.listing_date.slice(0, 10) : null;
  const days = num(r.days_since_listing);
  let stage: IpoRowView["stage"];
  switch (r.stage) {
    case "upcoming": stage = { text: date ? `Lists ${date}` : "Listing date not set", tone: "flat" }; break;
    case "pre_listing": stage = { text: "Lists today · pre-open", tone: "warn" }; break;
    case "listing_day": stage = { text: "Listing day · live", tone: "up" }; break;
    case "listed": stage = { text: days != null ? `Day ${days}` : "Listed", tone: "flat" }; break;
    case "no_data_yet": stage = { text: "⏳ Awaiting Yahoo data", tone: "warn" }; break;
    case "error": stage = { text: "⚠ Data error", tone: "down" }; break;
    default: stage = { text: typeof r.stage === "string" && r.stage ? r.stage : "—", tone: "flat" };
  }
  const issue = num(r.issue_price), cmp = num(r.current_price), g = num(r.current_vs_issue_pct), sc = num(r.ipo_score);
  const s = sc == null ? null : Math.max(0, Math.min(100, Math.round(sc)));
  const gm = num(r.gmp), gp = num(r.gmp_pct_of_issue);
  const gmpStage = r.stage === "upcoming" || r.stage === "pre_listing" || r.stage === "listing_day" || r.stage === "no_data_yet";
  return {
    stage,
    issue: issue == null ? "—" : `₹${issue}`,
    cmp: cmp != null && cmp > 0 ? `₹${cmp}` : null,
    gain: g == null ? null : { text: `${sgn(g, 1)}%`, dir: dirOf(g) },
    score: s == null ? null : { text: `${s}/100`, word: s >= 70 ? "Strong" : s >= 55 ? "Good" : s >= 40 ? "Mixed" : "Weak", tone: s >= 70 ? "buy" : s >= 55 ? "acc" : s >= 40 ? "warn" : "sell" },
    gmp: gm == null || !gmpStage ? null : { text: `GMP ₹${gm}${gp != null ? ` (${sgn(gp, 0)}%)` : ""}`, dir: dirOf(gm) },
  };
}
