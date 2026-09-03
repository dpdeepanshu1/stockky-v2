// frontend/src/components/StockChart.tsx
//
// Price chart with 1D/5D/1M/1Y/5Y period tabs, backed by
// /api/stock/history/{symbol} (training-service, wraps yfinance).
// Uses recharts — already a dependency (confirmed in package-lock.json),
// not a new one introduced here.
//
// 2026-09-03 Groww-style UI upgrade:
//  - Big price header that updates live as you drag/scrub the chart
//    (Groww's signature chart interaction), reverting to the latest price
//    on release.
//  - Pill-style period selector (rounded-full segmented control) instead
//    of small mono buttons.
//  - Colors now read from the active theme's CSS variables instead of
//    hardcoded dark-terminal hex — this was a real bug: the chart's grid/
//    axis/tooltip colors were fixed dark values that didn't adapt when the
//    light theme shipped, so this fixes an actual regression, not just a
//    style choice.

import { useEffect, useState, useCallback, useMemo } from "react";
import {
  AreaChart, Area, ResponsiveContainer, XAxis, YAxis, Tooltip, CartesianGrid,
  ReferenceDot,
} from "recharts";
import { api, StockHistory } from "../api";

interface Props {
  symbol: string;
  compact?: boolean; // smaller inline version, e.g. for a trade card
}

const PERIODS: { key: "1d" | "5d" | "1mo" | "1y" | "5y"; label: string }[] = [
  { key: "1d", label: "1D" },
  { key: "5d", label: "5D" },
  { key: "1mo", label: "1M" },
  { key: "1y", label: "1Y" },
  { key: "5y", label: "5Y" },
];

/** Reads the current theme's CSS custom properties once per render — cheap,
 *  not a hot path, and keeps the chart correct across the dark/light toggle
 *  without needing a context provider. */
function useThemeChartColors() {
  const isLight = typeof document !== "undefined" && document.documentElement.classList.contains("light");
  return useMemo(() => {
    const styles = getComputedStyle(document.documentElement);
    const v = (name: string, fallback: string) => styles.getPropertyValue(name).trim() || fallback;
    return {
      border: v("--border", "#2a2f3a"),
      muted: v("--muted", "#6b7280"),
      panel: v("--panel", "#14161c"),
      fg: v("--fg", "#e8edf2"),
      buy: v("--buy", "#0ecb81"),
      sell: v("--sell", "#f6465d"),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLight]);
}

export default function StockChart({ symbol, compact = false }: Props) {
  const [period, setPeriod] = useState<typeof PERIODS[number]["key"]>("1mo");
  const [data, setData] = useState<StockHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scrub, setScrub] = useState<{ label: string; close: number } | null>(null);

  const colors = useThemeChartColors();

  const fetchHistory = useCallback(async () => {
    setLoading(true);
    setError(null);
    setScrub(null);
    try {
      const result = await api.getStockHistory(symbol, period);
      setData(result);
    } catch (err) {
      console.error(err);
      setError("Chart unavailable — market data busy or cold start. Tap a period tab or retry.");
    } finally {
      setLoading(false);
    }
  }, [symbol, period]);

  useEffect(() => {
    fetchHistory();
  }, [fetchHistory]);

  const chartData = (data?.points ?? []).map((p) => ({
    date: p.date,
    close: p.close,
    label:
      period === "1d" || period === "5d"
        ? new Date(p.date).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })
        : new Date(p.date).toLocaleDateString("en-IN", { day: "2-digit", month: "short" }),
  }));

  const latestPoint = chartData[chartData.length - 1];
  const firstPoint = chartData[0];

  // While scrubbing, the header shows the touched point's price and its
  // change vs. the period's opening price — this is what makes the drag
  // interaction feel like Groww's rather than a static tooltip.
  const displayClose = scrub ? scrub.close : latestPoint?.close;
  const basePrice = firstPoint?.close ?? displayClose;
  const displayChangePct =
    scrub && basePrice
      ? ((scrub.close - basePrice) / basePrice) * 100
      : data?.change_pct ?? 0;
  const isUp = (displayChangePct ?? 0) >= 0;
  const lineColor = isUp ? colors.buy : colors.sell;

  const handleMove = (state: any) => {
    if (state?.activePayload?.length) {
      const point = state.activePayload[0].payload;
      setScrub({ label: point.label, close: point.close });
    }
  };
  const handleLeave = () => setScrub(null);

  return (
    <div className={compact ? "" : "rounded-2xl border border-slate bg-graphite p-5 shadow-panel"}>
      <div className="flex items-center justify-between mb-1">
        {!compact && (
          <span className="font-display text-sm sm:text-base font-bold text-paper tracking-wide">
            {symbol}
          </span>
        )}
        {/* Groww-style pill segmented control */}
        <div className="flex gap-0.5 bg-ink border border-slate rounded-full p-0.5 ml-auto">
          {PERIODS.map((p) => (
            <button
              key={p.key}
              onClick={() => setPeriod(p.key)}
              className={`px-2.5 py-1 text-[11px] font-semibold rounded-full transition-colors ${
                period === p.key
                  ? "bg-signal-buy text-white"
                  : "text-mist hover:text-paper"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {/* Big price header — updates live while scrubbing, Groww's signature interaction */}
      {!compact && data && (
        <div className="flex items-baseline gap-2 mb-3">
          <span className="font-display text-2xl sm:text-3xl font-extrabold text-paper tabular-nums">
            ₹{displayClose != null ? displayClose.toLocaleString("en-IN", { maximumFractionDigits: 2 }) : "—"}
          </span>
          <span className={`text-sm font-semibold tabular-nums ${isUp ? "text-signal-buy" : "text-signal-sell"}`}>
            {isUp ? "▲" : "▼"} {Math.abs(displayChangePct ?? 0).toFixed(2)}%
          </span>
          {scrub && <span className="text-[11px] text-mist ml-1">at {scrub.label}</span>}
        </div>
      )}

      {loading ? (
        <div className={`flex items-center justify-center ${compact ? "h-20" : "h-52"}`}>
          <span
            className="inline-block w-4 h-4 rounded-full border-2 border-current border-t-transparent animate-spin"
            style={{ color: colors.muted }}
          />
        </div>
      ) : error || chartData.length === 0 ? (
        <div
          className={`flex flex-col items-center justify-center gap-2 ${compact ? "h-20" : "h-52"} text-xs`}
          style={{ color: colors.muted }}
        >
          <span>{error || "No chart data"}</span>
          <button type="button" className="btn-terminal text-[10px]" onClick={() => fetchHistory()}>
            Retry chart
          </button>
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={compact ? 80 : 220}>
          <AreaChart
            data={chartData}
            margin={{ top: 4, right: 4, left: 0, bottom: 0 }}
            onMouseMove={handleMove}
            onMouseLeave={handleLeave}
            onTouchStart={handleMove}
            onTouchMove={handleMove}
            onTouchEnd={handleLeave}
          >
            <defs>
              <linearGradient id={`grad-${symbol}-${period}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={lineColor} stopOpacity={0.35} />
                <stop offset="100%" stopColor={lineColor} stopOpacity={0} />
              </linearGradient>
            </defs>
            {!compact && <CartesianGrid strokeDasharray="3 3" stroke={colors.border} vertical={false} />}
            {!compact && (
              <XAxis
                dataKey="label"
                tick={{ fill: colors.muted, fontSize: 10 }}
                axisLine={{ stroke: colors.border }}
                tickLine={false}
                minTickGap={40}
              />
            )}
            {!compact && (
              <YAxis
                domain={["auto", "auto"]}
                tick={{ fill: colors.muted, fontSize: 10 }}
                axisLine={false}
                tickLine={false}
                width={50}
                tickFormatter={(v) => `₹${v}`}
              />
            )}
            {!compact && (
              <Tooltip
                cursor={{ stroke: colors.muted, strokeDasharray: "3 3", strokeWidth: 1 }}
                contentStyle={{
                  background: colors.panel,
                  border: `1px solid ${colors.border}`,
                  borderRadius: 12,
                  fontSize: 11,
                  color: colors.fg,
                }}
                labelStyle={{ color: colors.muted }}
                formatter={(value: number) => [`₹${value}`, "Close"]}
              />
            )}
            <Area
              type="monotone"
              dataKey="close"
              stroke={lineColor}
              strokeWidth={2}
              fill={`url(#grad-${symbol}-${period})`}
              isAnimationActive={true}
              animationDuration={500}
              activeDot={{ r: 5, fill: lineColor, stroke: colors.panel, strokeWidth: 2 }}
            />
            {scrub && (
              <ReferenceDot
                x={scrub.label}
                y={scrub.close}
                r={5}
                fill={lineColor}
                stroke={colors.panel}
                strokeWidth={2}
                isFront
              />
            )}
          </AreaChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
