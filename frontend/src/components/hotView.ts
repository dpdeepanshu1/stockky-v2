// E151: summary tiles for the Hot Picks tab (pure, no React: tested by plain node in hotView.test.mjs).
// Everything is read from the payload the tab already holds. A score of exactly 0 is shown as 0 (only null / non-numbers mean "missing").
export type HotKpi = { label: string; value: string; sub?: string; tone?: "up" | "down" | "warn" | "info" | "flat" };

type Item = { symbol?: string; score?: number | null; combined_score?: number | null };
type Payload = {
  news_driven?: Item[]; results_driven?: Item[]; bulk_insider_driven?: Item[];
  universe_size?: number; scan_seed_count?: number; source?: string; age_hours?: number | null; fresh?: boolean; hours?: number; count?: number;
  stopped_early?: boolean; processed_symbols?: number;
};

export function hotAge(hours?: number | null): string {
  if (hours == null || !Number.isFinite(hours)) return "age unknown";
  if (hours < 1) return `${Math.max(0, Math.round(hours * 60))}m ago`;
  if (hours < 24) return `${hours.toFixed(1)}h ago`;
  return `${(hours / 24).toFixed(1)}d ago`;
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export function hotKpis(p: Payload | null | undefined): HotKpi[] {
  if (!p) return [];
  const groups: [string, Item[]][] = [["bulk / insider", p.bulk_insider_driven || []], ["results", p.results_driven || []], ["news", p.news_driven || []]];
  const total = groups.reduce((n, [, g]) => n + g.length, 0);
  let top: { s: number; sym: string } | null = null;
  for (const [, g] of groups) for (const it of g) {
    const s = num(it.score) ?? num(it.combined_score);
    if (s != null && (top == null || s > top.s)) top = { s, sym: it.symbol || "" };
  }
  const stored = p.source === "hotpicks_static_feed";
  const stale = stored && p.fresh === false;
  const out: HotKpi[] = [
    { label: "Picks", value: String(total), sub: groups.map(([n, g]) => `${n} ${g.length}`).join(" · "), tone: "info" },
    { label: "Top score", value: top ? String(Math.round(top.s)) : "—", sub: top ? top.sym : "no scored pick yet", tone: "flat" },
    { label: "Source", value: stored ? "Stored feed" : "Live scan", sub: stale ? `Stale - ${hotAge(p.age_hours)} - run a fresh scan` : stored ? `last ${p.hours ?? 24}h - ${hotAge(p.age_hours)}` : "just scanned", tone: stale ? "warn" : "info" },
    { label: "Universe", value: num(p.universe_size) != null ? String(p.universe_size) : "—", sub: p.stopped_early ? `Partial - stopped after ${p.processed_symbols ?? 0} symbols` : `scan seeds ${num(p.scan_seed_count) ?? "—"}`, tone: p.stopped_early ? "warn" : "flat" },
  ];
  return out;
}
