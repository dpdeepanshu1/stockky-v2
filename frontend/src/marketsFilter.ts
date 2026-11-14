// E541 (redesign round 7b): the ONE filter + sort for the Markets pages (Hot Picks, Surprise Momentum, IPO Tracker). Pure, no React, no import:
// tested by plain node in markets.test.mjs. It only decides what is SHOWN; it never changes, saves or re-requests the page's own list.
// A row with no figure for the chosen sort (null / NaN / not a number) goes last in its original order, and is never ranked as 0.
export type MarketSort = "rank" | "score" | "change" | "name";
export const SORT_LABEL: Record<MarketSort, string> = { rank: "Default", score: "Score", change: "Change %", name: "A to Z" };

export interface MarketAccess<T> {
  symbol: (r: T) => unknown;
  name?: (r: T) => unknown;
  score?: (r: T) => unknown;
  change?: (r: T) => unknown;
}
export interface MarketFilter { query: string; sort: MarketSort }
export const NO_FILTER: MarketFilter = { query: "", sort: "rank" };

const fin = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const txt = (v: unknown): string => (typeof v === "string" ? v : "");

/** Which sorts a page can offer: the ones its rows have a figure accessor for. "Default" and "A to Z" are always possible. */
export function sortsFor<T>(a: MarketAccess<T>): MarketSort[] {
  return (["rank", ...(a.score ? ["score"] : []), ...(a.change ? ["change"] : []), "name"]) as MarketSort[];
}

export function matchesQuery<T>(row: T, query: string, a: MarketAccess<T>): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return txt(a.symbol(row)).toLowerCase().includes(q) || (a.name ? txt(a.name(row)).toLowerCase().includes(q) : false);
}

export function applyMarketFilter<T>(rows: T[] | null | undefined, a: MarketAccess<T>, f: MarketFilter): T[] {
  const list = Array.isArray(rows) ? rows : [];
  const kept = list.filter((r) => matchesQuery(r, f.query, a));
  if (f.sort === "rank" || (f.sort === "score" && !a.score) || (f.sort === "change" && !a.change)) return kept;
  if (f.sort === "name") return kept.slice().sort((x, y) => txt(a.symbol(x)).localeCompare(txt(a.symbol(y))));
  const get = f.sort === "score" ? a.score! : a.change!;
  const known = kept.filter((r) => fin(get(r)));
  const unknown = kept.filter((r) => !fin(get(r)));
  known.sort((x, y) => (get(y) as number) - (get(x) as number));   // stable: equal figures keep their incoming order
  return known.concat(unknown);
}

/** "12 of 30 shown" / "30 shown" - the count line under the bar. */
export function countLine(shown: number, total: number): string {
  return shown === total ? `${total} shown` : `${shown} of ${total} shown`;
}

/** True when the filter is hiding rows that exist (so the page says "no match", not "nothing found"). */
export function isFilteredEmpty(shown: number, total: number): boolean {
  return shown === 0 && total > 0;
}
