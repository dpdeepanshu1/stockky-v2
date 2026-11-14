// E540 (redesign round 7): Watchlist order helpers (pure, no React, no import: tested by plain node in markets.test.mjs).
// "mine" is the saved order. "best" / "worst" only change what is SHOWN (by % since added); they never rewrite the saved list.
// A symbol with no price yet (null / NaN) always goes last, in its saved order, so a missing figure is never ranked as 0.
export type WatchSort = "mine" | "best" | "worst";
export const WATCH_SORTS: { id: WatchSort; label: string }[] = [
  { id: "mine", label: "My order" },
  { id: "best", label: "Gainers first" },
  { id: "worst", label: "Losers first" },
];

const fin = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export function sortWatchlist(symbols: string[], pct: Record<string, number | null | undefined>, mode: WatchSort): string[] {
  if (mode === "mine") return symbols.slice();
  const known = symbols.filter((s) => fin(pct[s]));
  const unknown = symbols.filter((s) => !fin(pct[s]));
  // stable: equal figures keep their saved order (Array.prototype.sort is stable in every supported browser)
  known.sort((a, b) => (mode === "best" ? (pct[b] as number) - (pct[a] as number) : (pct[a] as number) - (pct[b] as number)));
  return known.concat(unknown);
}

/** Move the item at `from` so it ends up at index `to` (both clamped). Returns a new array; the same array contents when nothing moves. */
export function moveSymbol(symbols: string[], from: number, to: number): string[] {
  const n = symbols.length;
  if (!Number.isInteger(from) || !Number.isInteger(to) || from < 0 || from >= n) return symbols.slice();
  const t = Math.max(0, Math.min(n - 1, to));
  if (t === from) return symbols.slice();
  const out = symbols.slice();
  const [it] = out.splice(from, 1);
  out.splice(t, 0, it);
  return out;
}

/** Drag-and-drop by symbol name (the list can change while dragging, an index could be stale). Unknown names change nothing. */
export function dropSymbol(symbols: string[], dragged: string, target: string): string[] {
  const from = symbols.indexOf(dragged), to = symbols.indexOf(target);
  if (from < 0 || to < 0 || from === to) return symbols.slice();
  return moveSymbol(symbols, from, to);
}

export function sameOrder(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((s, i) => s === b[i]);
}
