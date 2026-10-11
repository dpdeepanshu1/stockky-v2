// group324: small pure helpers of the Market Board (kept out of the .tsx files so node can test them).
import type { BoardPayload } from "./boardTypes";

export type StripKind = "live" | "stale" | "last" | "closed";

/** Pill state + text from the payload. Order matters: a logged last-session board wins over "closed", a stale feed only counts while open. */
export function stripState(data: Pick<BoardPayload, "stale" | "stale_day" | "market_open" | "feed">): { label: string; kind: StripKind } {
  const f = data.feed;
  const feedStale = data.market_open && ((f.tick_age_s ?? 0) > 30 || !f.connected);
  if (data.stale) return { label: `LAST SESSION ${data.stale_day ?? ""}`.trim(), kind: "last" };
  if (!data.market_open) return { label: "MARKET CLOSED", kind: "closed" };
  if (feedStale) return { label: "FEED STALE", kind: "stale" };
  return { label: "LIVE", kind: "live" };
}

/** 0..1 heat of a tile from its percentile (higher percentile = stronger). Missing / non-finite = neutral 0.5. */
export function heat(percentile: number | null | undefined): number {
  const p = typeof percentile === "number" && Number.isFinite(percentile) ? percentile : 50;
  return Math.min(1, Math.max(0, p / 100));
}

/** Filled dots (0..5) for a 0-100 conviction value. */
export function convictionDots(c: number | null | undefined): number {
  if (typeof c !== "number" || !Number.isFinite(c)) return 0;
  return Math.min(5, Math.max(0, Math.round(c / 20)));
}

/** Epoch seconds or milliseconds -> "HH:MM" in IST (the exchange clock), or "" when not a time. */
export function istClock(t: number): string {
  if (!Number.isFinite(t) || t <= 0) return "";
  const ms = t > 1e11 ? t : t * 1000;
  const d = new Date(ms + 19800000);                     // UTC+5:30
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}

/** Composite intraday path of one sector: the mean of its rows' sparklines, each rebased to % move from its own first point and
 *  resampled to `points` samples. Rows without a usable spark are skipped; returns [] when fewer than 2 samples are possible. Pure. */
export function sectorSpark(rows: { sector: string; spark?: number[] | null }[], sector: string | null | undefined, points = 24): number[] {
  if (!sector || points < 2) return [];
  const paths: number[][] = [];
  for (const r of rows) {
    const s = r.sector === sector && Array.isArray(r.spark) ? r.spark.filter(v => typeof v === "number" && Number.isFinite(v)) : [];
    if (s.length < 2 || !(s[0] !== 0)) continue;
    const base = s[0];
    paths.push(Array.from({ length: points }, (_, i) => {
      const f = (i / (points - 1)) * (s.length - 1), k = Math.floor(f), a = s[k], b = s[Math.min(s.length - 1, k + 1)];
      return ((a + (b - a) * (f - k)) / base - 1) * 100;
    }));
  }
  if (!paths.length) return [];
  return Array.from({ length: points }, (_, i) => paths.reduce((acc, p) => acc + p[i], 0) / paths.length);
}

/** Next value of a count-up tween: eases `from` toward `to` over `dur` ms (t = elapsed ms), integer output. */
export function tweenInt(from: number, to: number, t: number, dur = 500): number {
  if (!(dur > 0) || t >= dur) return Math.round(to);
  const u = Math.max(0, t / dur), e = 1 - Math.pow(1 - u, 3);
  return Math.round(from + (to - from) * e);
}
