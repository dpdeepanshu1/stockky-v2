// E557 (round 539, B3): the row sparklines on Hot, Surprise and IPO (pure, no React: tested by plain node in rowSpark.test.mjs).
// One batched read per set of rows (GET /market/sparklines), kept for 30 minutes in this module; a name with no usable history gets NO line, never an invented one.
export const SPARK_DAYS = 30;
export const SPARK_TTL_MS = 30 * 60 * 1000;
export const SPARK_RETRY_MS = 2 * 60 * 1000;     // after a failed read, ask again only after this long
export const SPARK_BATCH = 40;                   // the gateway's per-call cap
export const SPARK_MAX_WANTED = 120;             // never ask for more than this many names for one page

export type SparkReply = { series?: Record<string, number[]> | null };
export type SparkFetcher = (symbols: string[]) => Promise<SparkReply>;
type Entry = { until: number; closes: number[] | null };

export const sparkKey = (s: unknown): string => String(s ?? "").trim().toUpperCase().replace(/\.NS$/, "");

const store = new Map<string, Entry>();
const inflight = new Set<string>();

/** Distinct, cleaned names, order kept. */
export function wantedSymbols(symbols: Iterable<unknown>): string[] {
  const out: string[] = [];
  for (const s of symbols) { const k = sparkKey(s); if (k && !out.includes(k)) out.push(k); if (out.length >= SPARK_MAX_WANTED) break; }
  return out;
}

export function sparkFor(symbol: string, nowMs: number = Date.now()): number[] | null {
  const e = store.get(sparkKey(symbol));
  return e && e.until > nowMs ? e.closes : null;
}

/** Fetch what is not fresh and not already in flight, in batches of SPARK_BATCH. Resolves true when the store changed. Never throws. */
export async function loadSparklines(symbols: Iterable<unknown>, fetcher: SparkFetcher, nowMs: () => number = Date.now): Promise<boolean> {
  const need = wantedSymbols(symbols).filter((s) => { const e = store.get(s); return !(e && e.until > nowMs()) && !inflight.has(s); });
  let changed = false;
  for (let i = 0; i < need.length; i += SPARK_BATCH) {
    const part = need.slice(i, i + SPARK_BATCH);
    part.forEach((s) => inflight.add(s));
    try {
      const r = await fetcher(part);
      const series = (r && r.series) || {};
      for (const s of part) {
        const v = series[s];
        const ok = Array.isArray(v) && v.length >= 2 && v.every((x) => Number.isFinite(x) && x > 0);
        store.set(s, { until: nowMs() + SPARK_TTL_MS, closes: ok ? v : null });
      }
      changed = true;
    } catch {
      for (const s of part) store.set(s, { until: nowMs() + SPARK_RETRY_MS, closes: null });
      changed = true;
    } finally {
      part.forEach((s) => inflight.delete(s));
    }
  }
  return changed;
}

/** Test helper: forget everything. */
export function resetSparklines(): void { store.clear(); inflight.clear(); }

/** Percent change of each close against the first one (so the drawing starts on its zero line and its colour is the whole-period direction). */
export function sparkPercents(closes: number[] | null | undefined): number[] | null {
  if (!closes || closes.length < 2) return null;
  const first = closes[0];
  if (!Number.isFinite(first) || first <= 0) return null;
  const out = closes.map((c) => Math.round(((c / first) - 1) * 10000) / 100);
  return out.every(Number.isFinite) ? out : null;
}

/** The text alternative: where the line starts and ends. */
export function sparkSummary(symbol: string, closes: number[] | null | undefined): string {
  const p = sparkPercents(closes);
  if (!p || !closes) return `${symbol}: no recent price line`;
  const last = p[p.length - 1];
  return `${symbol}: ${last > 0 ? "+" : ""}${last.toFixed(1)}% over the last ${closes.length} sessions, from ${closes[0].toFixed(2)} to ${closes[closes.length - 1].toFixed(2)}`;
}
