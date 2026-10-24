// E25 / E26 / E27: one self-contained "chart + info" block for a stock: candlesticks on 1m / 5m / 15m / 30m / 1h / daily, pattern lines, badges and
// (for a position) entry / stop / target. Used under every position in Real Trade and Position Stocks, and as the Candles view of the stock chart.
// Fetches lazily, shares one request per symbol + timeframe across blocks (cache per timeframe), re-reads intraday charts while the market is open
// and the tab is visible, and falls back to a one-line note when the engine is down. Context only: it never feeds a decision.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import {
  type ChartCtx, type PriceLevel, type Tf, TIMEFRAMES, loadTfPref, marketHoursIST, missingText, normalizeCtx, saveTfPref, summaryLine, tfInfo, windowKeyFor, windowsFor,
} from "../chartContext";
import { setVisibleInterval } from "../visibleInterval";
import CandleChart from "./CandleChart";
import PatternBadges from "./PatternBadges";

const cache = new Map<string, { at: number; ctx: ChartCtx }>();
const inflight = new Map<string, Promise<ChartCtx>>();

export function loadCtx(symbol: string, tf: Tf, force = false): Promise<ChartCtx> {
  const sym = symbol.toUpperCase().replace(/\.(NS|BO)$/i, "");
  const key = `${sym}|${tf}`;
  const info = tfInfo(tf);
  const hit = cache.get(key);
  if (!force && hit && Date.now() - hit.at < info.ttlMs) return Promise.resolve(hit.ctx);
  const pending = inflight.get(key);
  if (pending) return pending;
  const p = api.getChartContext(sym, info.tail, tf, force).then(normalizeCtx).catch(() => normalizeCtx(null)).then((ctx) => {
    if (ctx.ok) cache.set(key, { at: Date.now(), ctx });
    inflight.delete(key);
    return ctx;
  });
  inflight.set(key, p);
  return p;
}

const lagText = (s: number): string => (s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

interface Props {
  symbol: string;
  levels?: PriceLevel[];
  defaultOpen?: boolean;
  bare?: boolean;      // no card frame / collapse header (used inside StockChart's Candles view)
  height?: number;
  defaultTf?: Tf;      // first timeframe when the person has not picked one before (default: the remembered one, else daily)
}

export default function StockCandleBlock({ symbol, levels = [], defaultOpen = true, bare = false, height = 220, defaultTf }: Props) {
  const [open, setOpen] = useState(defaultOpen || bare);
  const [tf, setTf] = useState<Tf>(() => loadTfPref(defaultTf ?? "1d"));
  const [ctx, setCtx] = useState<ChartCtx | null>(null);
  const [loading, setLoading] = useState(false);
  const [showFib, setShowFib] = useState(true);
  const [win, setWin] = useState<string | undefined>(undefined);
  const info = tfInfo(tf);

  const pick = (t: Tf) => { if (t === tf) return; setCtx(null); setWin(undefined); setTf(t); saveTfPref(t); };

  // (re)load when opened or when the timeframe / symbol changes; a result for an older selection is dropped
  const run = useCallback((force = false, silent = false) => {
    if (!silent) setLoading(true);
    let live = true;
    loadCtx(symbol, tf, force).then((c) => { if (live) { setCtx((prev) => (!c.ok && silent && prev?.ok ? prev : c)); setLoading(false); } });
    return () => { live = false; };
  }, [symbol, tf]);

  useEffect(() => (open ? run() : undefined), [open, run]);

  // intraday charts keep up with the market: re-read on the timeframe's cadence, only while open, in market hours and with the tab visible
  useEffect(() => {
    if (!open || !info.refreshMs) return undefined;
    return setVisibleInterval(() => { if (marketHoursIST(Date.now())) run(false, true); }, info.refreshMs);
  }, [open, info.refreshMs, run]);

  const wKey = windowKeyFor(tf, win);
  const summary = summaryLine(ctx);
  const body = (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex gap-0.5 bg-ink border border-slate rounded-full p-0.5" role="group" aria-label="Candle timeframe">
          {TIMEFRAMES.map((t) => (
            <button key={t.key} type="button" onClick={() => pick(t.key)} aria-pressed={tf === t.key} title={`${t.word} candles`}
              className={`px-2 py-0.5 text-[10px] font-semibold rounded-full ${tf === t.key ? "bg-signal-prepare text-white" : "text-mist hover:text-paper"}`}>{t.label}</button>
          ))}
        </div>
        <div className="flex gap-0.5 bg-ink border border-slate rounded-full p-0.5" role="group" aria-label="Zoom">
          {windowsFor(tf).map((w) => (
            <button key={w.key} type="button" onClick={() => setWin(w.key)} aria-pressed={wKey === w.key}
              className={`px-2 py-0.5 text-[10px] font-semibold rounded-full ${wKey === w.key ? "bg-signal-buy text-white" : "text-mist hover:text-paper"}`}>{w.label}</button>
          ))}
        </div>
      </div>
      <div className="font-display text-[9px] text-mist">
        {info.word} candles{ctx?.last_bar ? ` · to ${ctx.last_bar}` : ""}{info.intraday ? " (IST)" : ""}
        {ctx?.ok && info.intraday && ctx.live ? " · latest candle still forming" : ""}
      </div>
      {loading && !ctx ? (
        <div className="flex items-center justify-center" style={{ height }}>
          <span className="inline-block w-4 h-4 rounded-full border-2 border-mist border-t-transparent animate-spin" />
        </div>
      ) : ctx && ctx.ok ? (
        <>
          {ctx.note && <p className="font-display text-[10px] leading-snug text-signal-hold bg-signal-hold/10 border border-signal-hold/30 rounded-lg px-2.5 py-1.5">{ctx.note}</p>}
          {info.intraday && ctx.data_lag_s != null && ctx.data_lag_s > 180 && (
            <p className="font-display text-[10px] text-signal-hold">Data is about {lagText(ctx.data_lag_s)} behind the market, so the newest candle may be missing.</p>
          )}
          <CandleChart ctx={ctx} windowKey={wKey} height={height} levels={levels} showFib={showFib} />
          <PatternBadges badges={ctx.badges} tf={tf} />
          {ctx.fib && (
            <div className="flex items-center gap-2 font-display text-[9px] text-mist">
              <button type="button" onClick={() => setShowFib((v) => !v)} aria-pressed={showFib}
                className={`px-2 py-0.5 text-[10px] font-semibold rounded-full border ${showFib ? "border-signal-hold/40 text-signal-hold bg-signal-hold/10" : "border-slate text-mist hover:text-paper"}`}
                title="Fibonacci retracement of the latest daily swing. Shaded band = the 50 % - 61.8 % golden zone.">Fib</button>
              {showFib && <span>{ctx.fib.direction === "bullish" ? "Up" : "Down"}-leg {ctx.fib.swing_low}–{ctx.fib.swing_high} (daily){ctx.fib.in_golden_zone ? " · price is in the golden zone" : ""}{ctx.fib.invalidated ? " · leg broken, treat as stale" : ""}</span>}
            </div>
          )}
          {ctx.shapes.length > 0 && (
            <p className="font-display text-[9px] text-mist">Lines on the chart: {ctx.shapes.map((s) => `${s.label} (${s.status ?? "forming"})`).join(", ")}. Dashed = target.</p>
          )}
        </>
      ) : (
        <div className="flex items-center justify-between gap-2 text-[10px] font-display text-mist" style={{ minHeight: 48 }}>
          <span>{missingText(symbol, tf, ctx?.reason)}</span>
          <button type="button" className="btn-terminal text-[10px]" onClick={() => run(true)}>Retry</button>
        </div>
      )}
    </div>
  );

  if (bare) return body;
  return (
    <div className="mt-2 pt-2 border-t border-slate/50">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
        className="w-full flex items-center justify-between gap-2 text-left font-display text-[10px] text-mist">
        <span className="text-paper font-semibold">Candles &amp; patterns</span>
        <span className="truncate flex-1 text-right">{open ? summary : "tap to open"}</span>
        <span aria-hidden>{open ? "▾" : "▸"}</span>
      </button>
      {open && <div className="mt-2">{body}</div>}
    </div>
  );
}
