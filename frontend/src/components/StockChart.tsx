// frontend/src/components/StockChart.tsx
//
// Price chart with 1D/5D/1M/1Y/5Y period tabs, backed by
// /api/stock/history/{symbol} (training-service, wraps yfinance).
// Uses recharts — already a dependency (confirmed in package-lock.json),
// not a new one introduced here.

import { useEffect, useState, useCallback } from "react";
import { AreaChart, Area, ResponsiveContainer, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
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

export default function StockChart({ symbol, compact = false }: Props) {
  const [period, setPeriod] = useState<typeof PERIODS[number]["key"]>("1mo");
  const [data, setData] = useState<StockHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchHistory = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getStockHistory(symbol, period);
      setData(result);
    } catch (err) {
      console.error(err);
      setError("Chart data unavailable right now.");
    } finally {
      setLoading(false);
    }
  }, [symbol, period]);

  useEffect(() => {
    fetchHistory();
  }, [fetchHistory]);

  const isUp = (data?.change_pct ?? 0) >= 0;
  const lineColor = isUp ? "#34d399" : "#f87171"; // signal-buy / signal-sell tones
  const chartData = (data?.points ?? []).map((p) => ({
    date: p.date,
    close: p.close,
    label:
      period === "1d" || period === "5d"
        ? new Date(p.date).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })
        : new Date(p.date).toLocaleDateString("en-IN", { day: "2-digit", month: "short" }),
  }));

  return (
    <div className={compact ? "" : "rounded-xl border border-slate bg-graphite p-5"}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-baseline gap-2">
          {!compact && <span className="font-mono text-[10px] text-mist uppercase tracking-widest">{symbol}</span>}
          {data && (
            <span className={`font-mono text-xs ${isUp ? "text-signal-buy" : "text-signal-sell"}`}>
              {isUp ? "▲" : "▼"} {data.change_pct != null ? `${Math.abs(data.change_pct)}%` : "—"}
            </span>
          )}
        </div>
        <div className="flex gap-0.5 bg-ink/40 border border-slate/40 rounded-lg p-0.5">
          {PERIODS.map((p) => (
            <button
              key={p.key}
              onClick={() => setPeriod(p.key)}
              className={`px-2 py-0.5 text-[10px] font-mono uppercase rounded transition-colors ${
                period === p.key ? "bg-slate/60 text-paper" : "text-mist/50 hover:text-mist"
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className={`flex items-center justify-center ${compact ? "h-20" : "h-52"}`}>
          <span className="inline-block w-4 h-4 rounded-full border-2 border-current border-t-transparent text-mist/50 animate-spin" />
        </div>
      ) : error || chartData.length === 0 ? (
        <div className={`flex items-center justify-center ${compact ? "h-20" : "h-52"} text-mist/40 text-xs font-mono`}>
          {error || "No chart data"}
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={compact ? 80 : 220}>
          <AreaChart data={chartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id={`grad-${symbol}-${period}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={lineColor} stopOpacity={0.35} />
                <stop offset="100%" stopColor={lineColor} stopOpacity={0} />
              </linearGradient>
            </defs>
            {!compact && <CartesianGrid strokeDasharray="3 3" stroke="#2a2f3a" vertical={false} />}
            {!compact && (
              <XAxis
                dataKey="label"
                tick={{ fill: "#6b7280", fontSize: 10, fontFamily: "monospace" }}
                axisLine={{ stroke: "#2a2f3a" }}
                tickLine={false}
                minTickGap={40}
              />
            )}
            {!compact && (
              <YAxis
                domain={["auto", "auto"]}
                tick={{ fill: "#6b7280", fontSize: 10, fontFamily: "monospace" }}
                axisLine={false}
                tickLine={false}
                width={50}
                tickFormatter={(v) => `₹${v}`}
              />
            )}
            {!compact && (
              <Tooltip
                contentStyle={{
                  background: "#14161c", border: "1px solid #2a2f3a", borderRadius: 8,
                  fontFamily: "monospace", fontSize: 11,
                }}
                labelStyle={{ color: "#9ca3af" }}
                formatter={(value: number) => [`₹${value}`, "Close"]}
              />
            )}
            <Area
              type="monotone"
              dataKey="close"
              stroke={lineColor}
              strokeWidth={1.5}
              fill={`url(#grad-${symbol}-${period})`}
              isAnimationActive={true}
              animationDuration={600}
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
