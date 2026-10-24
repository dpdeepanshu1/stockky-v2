// E25 / E26 / E27: pure helpers for the candlestick chart (1m, 5m, 15m, 30m, 1h, 1d) + pattern / regime badges (api-gateway GET /stock/{sym}/chart-context?tf=).
// No React, no I/O, never throws: every function takes unknown / partial input and returns a safe value. Node test: chartContext.test.mjs
// Context only: nothing here feeds a decision, gate or score. Wording follows the whyXShubh visual guide (plain English, no jargon).

export type Tone = "good" | "bad" | "warn" | "mute";
export type Bar = [string, number, number, number, number, number]; // label, open, high, low, close, volume. label = "YYYY-MM-DD" (1d) or "YYYY-MM-DD HH:MM" IST bar start (intraday)
export type Tf = "1m" | "5m" | "15m" | "30m" | "1h" | "1d";

export interface ChartLine { role: string; x1: string; y1: number; x2: string; y2: number; dashed?: boolean }
export interface ChartShape { name: string; bias?: string | null; status?: string | null; label: string; tip?: string; lines: ChartLine[]; target?: number | null }
export interface ChartMarker { date: string; name: string; label: string; bias?: string | null; volume_confirmed?: boolean; bars_ago?: number }
export interface ChartBadge { key: string; kind: "regime" | "chart" | "candle" | "signal" | string; label: string; tone: Tone | string; tip?: string; status?: string; bars_ago?: number }
export interface ChartRegime { trend: string | null; volatility: string | null; momentum: string | null; structure: string | null }
export interface ChartCtx {
  symbol?: string; schema?: number; ok: boolean; bars: Bar[]; last_bar?: string | null;
  tf?: Tf; unit?: "day" | "candle"; note?: string | null; live?: Bar | null; data_lag_s?: number | null; reason?: string;   // E27
  markers: ChartMarker[]; shapes: ChartShape[]; levels: { kind: string; price: number }[]; badges: ChartBadge[]; regime: ChartRegime;
  fib?: ChartFib | null;   // E25b
}
/** E25b: Fibonacci retracement of the latest confirmed daily leg (display only). kind: end = 0 % / 100 %, retrace, golden = 50 / 61.8 %, ext = 127.2 / 161.8 %. */
export interface FibLevel { ratio: number; label: string; price: number; kind: "end" | "retrace" | "golden" | "ext" }
export interface ChartFib {
  direction: "bullish" | "bearish"; swing_low: number; swing_high: number; levels: FibLevel[]; golden: { low: number; high: number };
  in_golden_zone: boolean; retracement: number | null; invalidated: boolean; source: string;
}
export interface PriceLevel { label: string; price: number; tone: Tone }

export const WINDOWS: { key: string; label: string; bars: number }[] = [
  { key: "1m", label: "1M", bars: 22 }, { key: "3m", label: "3M", bars: 63 }, { key: "6m", label: "6M", bars: 126 }, { key: "1y", label: "1Y", bars: 250 },
];

// ---- E27: timeframes. `tail` = bars the gateway is asked for; `refreshMs` = how often an open chart re-reads while the market is open (0 = never).
export interface TfInfo { key: Tf; label: string; word: string; minutes: number; intraday: boolean; tail: number; ttlMs: number; refreshMs: number }
export const TIMEFRAMES: TfInfo[] = [
  { key: "1m", label: "1m", word: "1-minute", minutes: 1, intraday: true, tail: 750, ttlMs: 20_000, refreshMs: 30_000 },
  { key: "5m", label: "5m", word: "5-minute", minutes: 5, intraday: true, tail: 600, ttlMs: 45_000, refreshMs: 60_000 },
  { key: "15m", label: "15m", word: "15-minute", minutes: 15, intraday: true, tail: 400, ttlMs: 90_000, refreshMs: 120_000 },
  { key: "30m", label: "30m", word: "30-minute", minutes: 30, intraday: true, tail: 260, ttlMs: 90_000, refreshMs: 120_000 },
  { key: "1h", label: "1h", word: "1-hour", minutes: 60, intraday: true, tail: 300, ttlMs: 150_000, refreshMs: 180_000 },
  { key: "1d", label: "1D", word: "Daily", minutes: 1440, intraday: false, tail: 300, ttlMs: 300_000, refreshMs: 0 },
];
export const isTf = (v: unknown): v is Tf => typeof v === "string" && TIMEFRAMES.some((t) => t.key === v);
export const tfInfo = (tf: unknown): TfInfo => TIMEFRAMES.find((t) => t.key === tf) ?? TIMEFRAMES[TIMEFRAMES.length - 1];

// How many bars each zoom button shows. One NSE session is 375 minutes (09:15-15:30): 1m = 375 bars, 5m = 75, 15m = 25, 30m = 13, 1h = 7.
const INTRADAY_WINDOWS: Record<string, { key: string; label: string; bars: number }[]> = {
  "1m": [{ key: "1h", label: "1H", bars: 60 }, { key: "2h", label: "2H", bars: 120 }, { key: "1d", label: "1D", bars: 375 }, { key: "2d", label: "2D", bars: 750 }],
  "5m": [{ key: "1d", label: "1D", bars: 75 }, { key: "2d", label: "2D", bars: 150 }, { key: "5d", label: "5D", bars: 375 }],
  "15m": [{ key: "2d", label: "2D", bars: 50 }, { key: "5d", label: "5D", bars: 125 }, { key: "10d", label: "10D", bars: 250 }],
  "30m": [{ key: "2d", label: "2D", bars: 26 }, { key: "5d", label: "5D", bars: 65 }, { key: "10d", label: "10D", bars: 130 }, { key: "20d", label: "20D", bars: 260 }],
  "1h": [{ key: "5d", label: "5D", bars: 35 }, { key: "10d", label: "10D", bars: 70 }, { key: "20d", label: "20D", bars: 140 }, { key: "all", label: "All", bars: 300 }],
};
const DEFAULT_WINDOW: Record<string, string> = { "1m": "2h", "5m": "2d", "15m": "5d", "30m": "5d", "1h": "10d", "1d": "3m" };
export const windowsFor = (tf: unknown): { key: string; label: string; bars: number }[] => INTRADAY_WINDOWS[String(tf)] ?? WINDOWS;
export const defaultWindow = (tf: unknown): string => DEFAULT_WINDOW[String(tf)] ?? "3m";
/** The window key to use for this timeframe: the asked-for one when it exists there, else the timeframe's default. */
export const windowKeyFor = (tf: unknown, key: string | undefined): string => (windowsFor(tf).some((w) => w.key === key) ? (key as string) : defaultWindow(tf));

const fin = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** Safe ChartFib from anything the gateway returned, or null when it cannot be drawn. Never throws. */
export function normalizeFib(raw: unknown): ChartFib | null {
  try {
    const r = raw as any;
    if (!r || typeof r !== "object" || (r.direction !== "bullish" && r.direction !== "bearish") || !Array.isArray(r.levels)) return null;
    const kinds = ["end", "retrace", "golden", "ext"];
    const levels = (r.levels as any[]).filter((l) => l && fin(l.price) && l.price > 0 && fin(l.ratio) && typeof l.label === "string" && kinds.includes(l.kind))
      .map((l) => ({ ratio: l.ratio, label: l.label, price: l.price, kind: l.kind } as FibLevel));
    const g = r.golden;
    if (levels.length < 3 || !g || !fin(g.low) || !fin(g.high) || !fin(r.swing_low) || !fin(r.swing_high)) return null;
    return { direction: r.direction, swing_low: r.swing_low, swing_high: r.swing_high, levels, golden: { low: g.low, high: g.high },
      in_golden_zone: !!r.in_golden_zone, retracement: fin(r.retracement) ? r.retracement : null, invalidated: !!r.invalidated, source: typeof r.source === "string" ? r.source : "daily" };
  } catch { return null; }
}

/** Safe ChartCtx from anything the gateway returned; ok=false (and empty arrays) when it is not usable. */
export function normalizeCtx(raw: unknown): ChartCtx {
  const empty: ChartCtx = { ok: false, bars: [], markers: [], shapes: [], levels: [], badges: [], regime: { trend: null, volatility: null, momentum: null, structure: null } };
  try {
    const r = raw as any;
    if (r && typeof r === "object" && !r.ok && typeof r.reason === "string") return { ...empty, tf: isTf(r.tf) ? r.tf : undefined, reason: r.reason.slice(0, 40) };
    if (!r || typeof r !== "object" || !r.ok || !Array.isArray(r.bars)) return empty;
    const bars = (r.bars as unknown[]).filter((b): b is Bar => Array.isArray(b) && typeof b[0] === "string" && [1, 2, 3, 4].every((i) => fin((b as any[])[i]))).map((b) => [b[0], b[1], b[2], b[3], b[4], fin(b[5]) ? b[5] : 0] as Bar);
    if (bars.length < 5) return empty;
    const arr = <T,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);
    const lv = r.live, live: Bar | null = Array.isArray(lv) && typeof lv[0] === "string" && [1, 2, 3, 4].every((i) => fin(lv[i])) ? [lv[0], lv[1], lv[2], lv[3], lv[4], fin(lv[5]) ? lv[5] : 0] : null;
    const tf: Tf = isTf(r.tf) ? r.tf : "1d";
    return {
      symbol: r.symbol, schema: r.schema, ok: true, bars, last_bar: r.last_bar ?? bars[bars.length - 1][0],
      tf, unit: tf === "1d" ? "day" : "candle", note: typeof r.note === "string" && r.note ? r.note : null, live: tf === "1d" ? null : live,
      data_lag_s: fin(r.data_lag_s) ? Math.max(0, Math.round(r.data_lag_s)) : null,
      markers: arr<ChartMarker>(r.markers), shapes: arr<ChartShape>(r.shapes).filter((s) => s && Array.isArray(s.lines)),
      levels: arr<{ kind: string; price: number }>(r.levels).filter((l) => l && fin(l.price)), badges: arr<ChartBadge>(r.badges),
      regime: { ...empty.regime, ...(r.regime && typeof r.regime === "object" ? r.regime : {}) }, fib: normalizeFib(r.fib),
    };
  } catch { return empty; }
}

export const toneOf = (t: unknown): Tone => (t === "good" || t === "bad" || t === "warn" ? t : "mute");
/** Tailwind text class per tone (the app's own tokens). */
export const toneText = (t: unknown): string => ({ good: "text-signal-buy", bad: "text-signal-sell", warn: "text-signal-hold", mute: "text-mist" }[toneOf(t)]);
/** Chip classes per tone. */
export const toneChip = (t: unknown): string => ({
  good: "text-signal-buy border-signal-buy/30 bg-signal-buy/10", bad: "text-signal-sell border-signal-sell/30 bg-signal-sell/10",
  warn: "text-signal-hold border-signal-hold/30 bg-signal-hold/10", mute: "text-mist border-slate bg-ink",
}[toneOf(t)]);
/** CSS colour for SVG (theme variables, so the chart follows the dark / light toggle). */
export const toneColor = (t: unknown): string => ({ good: "rgb(var(--buy-rgb))", bad: "rgb(var(--sell-rgb))", warn: "rgb(var(--warn-rgb))", mute: "rgb(var(--muted-rgb))" }[toneOf(t)]);

/** Last `n` bars (never fewer than 5 unless the series is shorter). Returns the start index into the full series. */
export function windowStart(total: number, n: number): number {
  if (!fin(total) || total <= 0) return 0;
  return Math.max(0, total - Math.max(5, Math.floor(n)));
}

/** A line given in dates, mapped to full-series indices and clipped to start at `lo`. null when it ends before the window or a date is unknown. */
export function clipLine(line: ChartLine, dates: string[], lo: number): { i1: number; y1: number; i2: number; y2: number } | null {
  const i2 = dates.indexOf(line?.x2);
  let i1 = dates.indexOf(line?.x1);
  if (i1 < 0 || i2 < 0 || !fin(line.y1) || !fin(line.y2) || i2 < lo) return null;
  let y1 = line.y1;
  if (i1 < lo) { y1 = i2 === i1 ? y1 : y1 + (line.y2 - y1) * (lo - i1) / (i2 - i1); i1 = lo; }
  return { i1, y1, i2, y2: line.y2 };
}

/** "Nice" axis ticks between min and max (about `count` of them). */
export function niceTicks(min: number, max: number, count = 4): number[] {
  if (!fin(min) || !fin(max) || max <= min || count < 2) return [];
  const raw = (max - min) / count, mag = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / mag;
  const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag;
  const out: number[] = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(Math.round(v / step) * step);
  return out;
}

/** Price scale for the visible bars + extra prices that are close enough to matter (others are drawn off-scale, i.e. skipped). */
export function priceRange(bars: Bar[], extras: number[]): { min: number; max: number } | null {
  if (!bars.length) return null;
  let lo = Infinity, hi = -Infinity;
  for (const b of bars) { lo = Math.min(lo, b[3]); hi = Math.max(hi, b[2]); }
  if (!fin(lo) || !fin(hi)) return null;
  const span = Math.max(hi - lo, Math.abs(hi) * 0.001, 1e-6);
  let a = lo, z = hi;
  for (const p of extras) if (fin(p) && p >= lo - 0.3 * span && p <= hi + 0.3 * span) { a = Math.min(a, p); z = Math.max(z, p); }
  const pad = (z - a) * 0.06;
  return { min: a - pad, max: z + pad };
}

export const BADGE_ORDER = ["regime", "chart", "candle", "signal"] as const;
export const BADGE_TITLES: Record<string, string> = { regime: "Market mood", chart: "Chart shapes", candle: "Candles", signal: "Signals" };

/** Badges grouped in display order; unknown kinds go last under "signal". */
export function groupBadges(badges: ChartBadge[] | null | undefined): { kind: string; title: string; items: ChartBadge[] }[] {
  const list = Array.isArray(badges) ? badges.filter((b) => b && typeof b.label === "string") : [];
  return BADGE_ORDER.map((k) => ({ kind: k, title: BADGE_TITLES[k], items: list.filter((b) => (BADGE_ORDER as readonly string[]).includes(b.kind) ? b.kind === k : k === "signal") }))
    .filter((g) => g.items.length > 0);
}

/** "today" / "yesterday" / "3d ago" on the daily chart; "latest candle" / "1 candle ago" / "3 candles ago" on an intraday one. */
export function agoText(ago: number | undefined, tf?: unknown): string {
  const n = typeof ago === "number" && Number.isFinite(ago) ? Math.max(0, Math.floor(ago)) : 0;
  if (tfInfo(tf).intraday) return n === 0 ? "latest candle" : n === 1 ? "1 candle ago" : `${n} candles ago`;
  return n === 0 ? "today" : n === 1 ? "yesterday" : `${n}d ago`;
}

/** One plain line for a collapsed card: "Uptrend · Bull flag (forming) · Hammer today". Parts with no data are dropped. */
export function summaryLine(ctx: ChartCtx | null | undefined): string {
  if (!ctx || !ctx.ok) return "";
  const parts: string[] = [];
  const trend = ctx.badges.find((b) => b.key === "regime_trend");
  if (trend) parts.push(trend.label);
  const shape = ctx.badges.find((b) => b.kind === "chart");
  if (shape) parts.push(`${shape.label}${shape.status ? ` (${shape.status})` : ""}`);
  const candle = ctx.badges.find((b) => b.kind === "candle");
  if (candle) parts.push(`${candle.label} ${agoText(candle.bars_ago, ctx.tf)}`);
  const sig = ctx.badges.find((b) => b.kind === "signal");
  if (sig && parts.length < 3) parts.push(sig.label);
  return parts.join(" · ");
}

/** Day change of a bar vs the previous close, in %, or null. */
export function barChangePct(bars: Bar[], i: number): number | null {
  const cur = bars[i], prev = bars[i - 1];
  if (!cur || !prev || !prev[4]) return null;
  return ((cur[4] - prev[4]) / prev[4]) * 100;
}

/** "22 Oct" from "2026-10-22"; the input back when it does not parse. */
export function shortDate(d: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d || "");
  if (!m) return d || "";
  return `${Number(m[3])} ${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][Number(m[2]) - 1] ?? ""}`;
}

// ---- E27: labels, session breaks, market hours
const INTRA = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/;
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const isIntradayLabel = (l: unknown): boolean => typeof l === "string" && INTRA.test(l);
export const dateOf = (l: string): string => (l || "").slice(0, 10);
export const timeOf = (l: string): string => { const m = INTRA.exec(l || ""); return m ? `${m[4]}:${m[5]}` : ""; };
const dm = (l: string): string => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(l || ""); return m ? `${Number(m[3])} ${MON[Number(m[2]) - 1] ?? ""}` : l || ""; };

/** Readout label: "22 Oct" (daily) or "22 Oct 14:35" (intraday). */
export function barLabel(l: string): string { return isIntradayLabel(l) ? `${dm(l)} ${timeOf(l)}` : shortDate(l); }

/** Axis tick label: intraday shows the clock time, and the date instead when the day changed since the previous tick (the first tick shows both). */
export function axisLabel(l: string, prev?: string): string {
  if (!isIntradayLabel(l)) return shortDate(l);
  if (!prev) return `${dm(l)} ${timeOf(l)}`;
  return dateOf(prev) !== dateOf(l) ? dm(l) : timeOf(l);
}

/** Series indices (within [from, to]) of the first bar of a new IST day: where a session separator is drawn. Daily labels never break. */
export function dayBreaks(labels: string[], from = 0, to = labels.length - 1): number[] {
  const out: number[] = [];
  for (let i = Math.max(1, from); i <= Math.min(to, labels.length - 1); i++) {
    if (isIntradayLabel(labels[i]) && isIntradayLabel(labels[i - 1]) && dateOf(labels[i]) !== dateOf(labels[i - 1])) out.push(i);
  }
  return out;
}

/** Close vs the previous session's last close, in %, for an intraday bar (null on the first session, or for daily labels). */
export function sessionChangePct(bars: Bar[], i: number): number | null {
  const cur = bars[i];
  if (!cur || !isIntradayLabel(cur[0])) return null;
  for (let j = i - 1; j >= 0; j--) {
    if (dateOf(bars[j][0]) !== dateOf(cur[0])) return bars[j][4] ? ((cur[4] - bars[j][4]) / bars[j][4]) * 100 : null;
  }
  return null;
}

/** NSE cash hours in IST (Mon-Fri 09:15-15:35, five minutes of grace for the last bar). Holidays are not known here: the server answers either way. */
export function marketHoursIST(nowMs: number): boolean {
  if (!fin(nowMs)) return false;
  const d = new Date(nowMs + 19_800_000);                  // shift to IST, then read it as UTC
  const dow = d.getUTCDay();
  if (dow === 0 || dow === 6) return false;
  const min = d.getUTCHours() * 60 + d.getUTCMinutes();
  return min >= 9 * 60 + 15 && min <= 15 * 60 + 35;
}

/** Plain-English reason an intraday chart is missing (the gateway passes the technical service's reason through). */
export function missingText(symbol: string, tf: unknown, reason?: string): string {
  const t = tfInfo(tf);
  if (!t.intraday) return `Candles are not available for ${symbol} right now (daily history too short or the data service is busy).`;
  const why = reason === "thin" ? "there are too few recent candles yet, so try a longer timeframe or come back after the open"
    : reason === "no_data" ? "the data service has no intraday candles for this symbol"
    : reason === "transient" || (reason ?? "").startsWith("http_") ? "the data service is busy or slow right now"
    : "the data service did not return recent candles";
  return `${t.word} candles are not available for ${symbol} right now: ${why}.`;
}

/** Remember the chosen timeframe across charts (localStorage when there is one; never throws). */
export function loadTfPref(fallback: Tf = "1d"): Tf {
  try { const v = typeof localStorage !== "undefined" ? localStorage.getItem("stockky.candle.tf") : null; return isTf(v) ? v : fallback; } catch { return fallback; }
}
export function saveTfPref(tf: Tf): void {
  try { if (typeof localStorage !== "undefined") localStorage.setItem("stockky.candle.tf", tf); } catch { /* private mode: the choice just is not remembered */ }
}
