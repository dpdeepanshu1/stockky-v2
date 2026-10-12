// group339: pure "current momentum" read for the stock popup. Input is what GET /board/{symbol} already returns (candles [t,o,h,l,c,v] at tf_min
// minutes, the live ltp, vwap), output is a few plain numbers plus one label. No React, no I/O, never throws: too little data gives
// label "Warming up" and null fields. Display only - it places no order; the entry engines have their own gates.
export interface Momentum {
  label: "Strong up" | "Up" | "Flat" | "Down" | "Strong down" | "Warming up";
  tone: "g" | "r" | "flat";
  score: number | null;            // -100 (strong down) .. +100 (strong up)
  shortPct: number | null;         // move over the last 3 bars, % (uses the live ltp as the newest price)
  longPct: number | null;          // move over the last 6 bars, %
  shortBars: number; longBars: number; tfMin: number;
  greenOf5: number | null;         // green bars among the last 5 (close >= open), null with < 5 bars
  vwapPct: number | null;          // live price vs VWAP, %
  dayPos: number | null;           // 0 = at the day low, 1 = at the day high
  volRatio: number | null;         // last bar's volume vs the average of the previous bars (null when volume is not available)
  bars: number;
}

const fin = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const pct = (a: number, b: number) => (b > 0 ? ((a - b) / b) * 100 : null);

export function computeMomentum(candles: number[][] | null | undefined, ltp: number | null | undefined, vwap: number | null | undefined, tfMin = 5): Momentum {
  const c = (Array.isArray(candles) ? candles : []).filter(k => Array.isArray(k) && k.length >= 5 && k.slice(1, 5).every(fin));
  const empty: Momentum = { label: "Warming up", tone: "flat", score: null, shortPct: null, longPct: null, shortBars: 3, longBars: 6, tfMin,
    greenOf5: null, vwapPct: null, dayPos: null, volRatio: null, bars: c.length };
  if (c.length < 3) return empty;
  const px = fin(ltp) && ltp > 0 ? ltp : c[c.length - 1][4];
  const closeAt = (back: number) => c[Math.max(0, c.length - 1 - back)][4];       // close `back` bars before the newest bar
  const shortPct = pct(px, closeAt(3)), longPct = c.length >= 7 ? pct(px, closeAt(6)) : pct(px, c[0][1]);
  const last5 = c.slice(-5);
  const greenOf5 = last5.length === 5 ? last5.filter(k => k[4] >= k[1]).length : null;
  const vwapPct = fin(vwap) && vwap > 0 ? pct(px, vwap) : null;
  const hi = Math.max(...c.map(k => k[2]), px), lo = Math.min(...c.map(k => k[3]), px);
  const dayPos = hi > lo ? (px - lo) / (hi - lo) : null;
  let volRatio: number | null = null;
  const vols = c.map(k => (fin(k[5]) ? k[5] : 0));
  if (c.length >= 4 && vols[vols.length - 1] > 0) {
    const prev = vols.slice(0, -1).filter(x => x > 0);
    if (prev.length) volRatio = vols[vols.length - 1] / (prev.reduce((a, b) => a + b, 0) / prev.length);
  }
  // blend: recent drift (clamped at +/-1.5%), the longer drift (+/-3%), bar colour, VWAP side, where price sits in the day range
  const cl = (x: number | null, m: number) => (x == null ? 0 : Math.max(-1, Math.min(1, x / m)));
  const parts = [
    [cl(shortPct, 1.5), 35], [cl(longPct, 3), 25],
    [greenOf5 == null ? 0 : (greenOf5 - 2.5) / 2.5, 15], [cl(vwapPct, 1.5), 15], [dayPos == null ? 0 : (dayPos - 0.5) * 2, 10],
  ] as [number, number][];
  const score = Math.round(parts.reduce((a, [v, w]) => a + v * w, 0));
  const label: Momentum["label"] = score >= 45 ? "Strong up" : score >= 15 ? "Up" : score <= -45 ? "Strong down" : score <= -15 ? "Down" : "Flat";
  return { label, tone: score >= 15 ? "g" : score <= -15 ? "r" : "flat", score, shortPct, longPct, shortBars: 3, longBars: c.length >= 7 ? 6 : c.length - 1,
    tfMin, greenOf5, vwapPct, dayPos, volRatio, bars: c.length };
}

/** Signed percent for display: "+0.42%", "-1.10%", "-" when not a number. */
export function fmtPct(v: number | null | undefined, d = 2): string {
  return fin(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(d)}%` : "-";
}
