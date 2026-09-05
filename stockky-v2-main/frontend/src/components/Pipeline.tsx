import { useEffect, useState } from "react";

const STAGES = [
  { label: "Market data", detail: "Fetching live price & history" },
  { label: "Technical analysis", detail: "RSI, MACD, EMA, ADX…" },
  { label: "Fundamental analysis", detail: "P/E, P/B, growth, valuation…" },
  { label: "News intelligence", detail: "Sentiment scoring" },
  // ── NEW: Market Sentiment stage ──
  { label: "Market sentiment", detail: "Bullish/Bearish/Neutral scoring" },
  { label: "Event tracker", detail: "Earnings, dividends, splits, insider trades…" },
  { label: "AI prediction", detail: "XGBoost model inference" },
  { label: "Decision synthesis", detail: "Combining all signals" },
];

export default function Pipeline({ running }: { running: boolean }) {
  const [activeIndex, setActiveIndex] = useState(-1);

  useEffect(() => {
    if (!running) { setActiveIndex(-1); return; }
    setActiveIndex(0);
    const interval = setInterval(() => {
      setActiveIndex((i) => {
        if (i >= STAGES.length - 1) { clearInterval(interval); return i; }
        return i + 1;
      });
    }, 400);
    return () => clearInterval(interval);
  }, [running]);

  return (
    <div className="flex flex-col gap-0">
      {STAGES.map((stage, i) => {
        const state = !running
          ? "idle"
          : i < activeIndex
          ? "done"
          : i === activeIndex
          ? "running"
          : "waiting";
        return (
          <div key={stage.label} className="flex items-start gap-3 py-2">
            <div className="flex flex-col items-center pt-0.5">
              <span
                className={
                  "h-2 w-2 rounded-full transition-all duration-300 " +
                  (state === "done"
                    ? "bg-signal-buy"
                    : state === "running"
                    ? "bg-signal-prepare animate-pulse scale-125"
                    : "bg-slate")
                }
              />
              {i < STAGES.length - 1 && (
                <span
                  className={
                    "w-px h-6 mt-1 transition-colors duration-500 " +
                    (state === "done" ? "bg-signal-buy/40" : "bg-slate/40")
                  }
                />
              )}
            </div>
            <div>
              <div
                className={
                  "font-mono text-xs tracking-wide transition-colors duration-300 " +
                  (state === "done"
                    ? "text-mist"
                    : state === "running"
                    ? "text-paper"
                    : "text-mist/30")
                }
              >
                {stage.label}
              </div>
              {state === "running" && (
                <div className="font-mono text-[10px] text-mist/50 mt-0.5">
                  {stage.detail}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}