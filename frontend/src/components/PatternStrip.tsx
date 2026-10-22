// E26: always-visible "Market mood and patterns" strip at the top of the stock page (DecisionCard). The chips used to be reachable only after
// switching the chart to Candles. Daily timeframe, shares the per-symbol cache with StockCandleBlock (one request). Context only: it never feeds a
// decision, and it shows nothing at all while loading or when the engine has no data (no empty box, no error noise on the decision page).
import { useEffect, useState } from "react";
import { type ChartCtx } from "../chartContext";
import { loadCtx } from "./StockCandleBlock";
import PatternBadges from "./PatternBadges";

export default function PatternStrip({ symbol }: { symbol: string }) {
  const [ctx, setCtx] = useState<ChartCtx | null>(null);
  useEffect(() => {
    let live = true;
    setCtx(null);
    loadCtx(symbol, "1d").then((c) => { if (live) setCtx(c); });
    return () => { live = false; };
  }, [symbol]);
  if (!ctx || !ctx.ok || !ctx.badges.length) return null;
  return (
    <div className="rounded-2xl border border-slate/40 bg-graphite/30 p-3 space-y-1.5" aria-label="Market mood and patterns">
      <div className="font-display text-[10px] text-paper font-semibold">Market mood &amp; patterns <span className="text-mist font-normal">· daily chart, context only</span></div>
      <PatternBadges badges={ctx.badges} tf="1d" />
    </div>
  );
}
