import { useEffect, useState } from "react";

const STAGES = [
  { label: "Market data", detail: "Fetching live price & history", icon: "📈" },
  { label: "Technical analysis", detail: "RSI, MACD, EMA, ADX…", icon: "📊" },
  { label: "Fundamental analysis", detail: "P/E, P/B, growth, valuation…", icon: "🏛" },
  { label: "News intelligence", detail: "Sentiment scoring", icon: "📰" },
  { label: "Market sentiment", detail: "Bullish/Bearish/Neutral scoring", icon: "🧭" },
  { label: "Event tracker", detail: "Earnings, dividends, splits, insider trades…", icon: "📅" },
  { label: "AI prediction", detail: "XGBoost model inference", icon: "🤖" },
  { label: "Decision synthesis", detail: "Combining all signals", icon: "⚡" },
];

interface PipelineProps {
  running: boolean;
  /** When true, renders a static dashboard overview (all stages visible, idle state). */
  dashboard?: boolean;
}

export default function Pipeline({ running, dashboard = false }: PipelineProps) {
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

  // ── Dashboard mode: static overview panel ──────────────────────────────
  if (dashboard && !running) {
    return (
      <div className="rounded-2xl border border-slate/40 bg-graphite/60 p-4">
        <h3 className="font-display tabular-nums text-[11px] text-paper mb-3 flex items-center gap-2">
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-slate-400" />
          Scoring Pipeline
          <span className="text-mist/40 font-normal">— idle</span>
        </h3>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {STAGES.map((stage) => (
            <div
              key={stage.label}
              className="flex items-center gap-2 rounded-xl border border-slate/30 bg-ink/30 px-3 py-2"
            >
              <span className="text-[13px] opacity-60">{stage.icon}</span>
              <div>
                <div className="font-display tabular-nums text-[10px] text-mist/50 leading-tight">
                  {stage.label}
                </div>
                <div className="font-display tabular-nums text-[9px] text-mist/30 leading-tight mt-0.5 hidden sm:block">
                  {stage.detail}
                </div>
              </div>
            </div>
          ))}
        </div>
        <p className="font-display tabular-nums text-[9px] text-mist/30 mt-3">
          Click <strong className="text-mist/50">Search Hot Picks Stocks</strong> to run the full pipeline across the scan universe.
        </p>
      </div>
    );
  }

  // ── Active scan mode (existing vertical list) ──────────────────────────
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
                {stage.icon} {stage.label}
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
