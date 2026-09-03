import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

type Upstream = {
  id: string;
  label: string;
  events_1h: number;
  by_status: Record<string, number>;
  level: "ok" | "watch" | "warn" | "critical";
  codes?: number[];
};

type Circuit = {
  name?: string;
  state?: string;
  failures?: number;
  last_error?: string | null;
  recovery_timeout?: number;
};

type RateLimitSnapshot = {
  overall: string;
  window_sec: number;
  events_1h: number;
  by_status_1h: Record<string, number>;
  upstreams: Upstream[];
  circuits: Circuit[];
  recent_events: Array<{
    ts: number;
    source: string;
    status: number;
    path?: string;
    detail?: string;
    symbol?: string;
  }>;
  redis_backed: boolean;
  neon_backed?: boolean;
  advice: string[];
  generated_at: number;
};

function levelClass(level: string) {
  if (level === "critical") return "text-signal-sell border-signal-sell/40 bg-signal-sell/10";
  if (level === "warn" || level === "degraded") return "text-signal-hold border-signal-hold/40 bg-signal-hold/10";
  if (level === "watch") return "text-signal-prepare border-signal-prepare/30 bg-signal-prepare/5";
  return "text-signal-buy border-signal-buy/30 bg-signal-buy/5";
}

function fmtTime(ts: number) {
  try {
    return new Date(ts * 1000).toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZone: "Asia/Kolkata",
    });
  } catch {
    return "—";
  }
}

export default function RateLimitDashboard() {
  const [data, setData] = useState<RateLimitSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snap = await api.getRateLimits();
      setData(snap);
    } catch (e) {
      setError((e as Error).message || "Failed to load rate-limit dashboard");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div className="rate-limit-dash space-y-3">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div>
          <h3 className="mono text-xs text-mist uppercase tracking-widest mb-1">
            Rate limit monitor
          </h3>
          <p className="text-xs text-mist/60 mb-0">
            Yahoo / market-data 503 · Gemini 429 · circuit breakers · last {data?.window_sec ? Math.round(data.window_sec / 60) : 60}m
            {data?.neon_backed ? " · Neon-backed" : data?.redis_backed ? " · Redis-backed" : " · process memory"}
          </p>
        </div>
        <button type="button" className="btn-terminal text-xs" onClick={load} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {error && (
        <p className="mono text-xs text-signal-sell mb-0">{error}</p>
      )}

      {data && (
        <>
          <div className={`terminal-panel border rounded-xl px-3 py-2 ${levelClass(data.overall)}`}>
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <span className="mono text-sm font-medium uppercase tracking-wider">
                Overall: {data.overall}
              </span>
              <span className="mono text-[10px] opacity-80">
                {data.events_1h} events · 429={data.by_status_1h?.["429"] || 0} · 503={data.by_status_1h?.["503"] || 0}
              </span>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {data.upstreams?.map((u) => (
              <div
                key={u.id}
                className={`terminal-panel border rounded-xl px-3 py-2 ${levelClass(u.level)}`}
              >
                <div className="flex justify-between items-baseline gap-2">
                  <span className="mono text-xs font-medium">{u.label}</span>
                  <span className="mono text-[10px] uppercase">{u.level}</span>
                </div>
                <p className="mono text-[11px] opacity-80 mb-0 mt-1">
                  {u.events_1h} hits/1h
                  {Object.keys(u.by_status || {}).length > 0 && (
                    <> · {Object.entries(u.by_status).map(([c, n]) => `${c}×${n}`).join(" ")}</>
                  )}
                </p>
              </div>
            ))}
          </div>

          {data.circuits && data.circuits.length > 0 && (
            <div className="terminal-panel">
              <h4 className="mono text-[10px] text-mist uppercase tracking-widest mb-2">Circuits</h4>
              <div className="space-y-1">
                {data.circuits.map((c, i) => (
                  <div key={c.name || i} className="flex justify-between mono text-[11px] gap-2">
                    <span>{c.name || "—"}</span>
                    <span className={c.state === "open" ? "text-signal-sell" : c.state === "half_open" ? "text-signal-hold" : "text-signal-buy"}>
                      {c.state || "?"}{typeof c.failures === "number" ? ` (${c.failures} fails)` : ""}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {data.advice?.length > 0 && (
            <ul className="text-xs text-mist/80 space-y-1 pl-4 list-disc mono">
              {data.advice.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          )}

          <div className="terminal-panel overflow-x-auto">
            <h4 className="mono text-[10px] text-mist uppercase tracking-widest mb-2">Recent events</h4>
            {(!data.recent_events || data.recent_events.length === 0) && (
              <p className="mono text-xs text-mist/50 mb-0">No rate-limit events recorded yet.</p>
            )}
            <table className="w-full text-left mono text-[10px]">
              <thead>
                <tr className="text-mist/50 border-b border-slate/40">
                  <th className="py-1 pr-2 font-normal">IST</th>
                  <th className="py-1 pr-2 font-normal">Src</th>
                  <th className="py-1 pr-2 font-normal">Code</th>
                  <th className="py-1 pr-2 font-normal">Symbol</th>
                  <th className="py-1 font-normal">Path / detail</th>
                </tr>
              </thead>
              <tbody>
                {(data.recent_events || []).map((e, i) => (
                  <tr key={i} className="border-b border-slate/20 align-top">
                    <td className="py-1 pr-2 whitespace-nowrap">{fmtTime(e.ts)}</td>
                    <td className="py-1 pr-2">{e.source}</td>
                    <td className={`py-1 pr-2 ${e.status === 429 || e.status === 503 ? "text-signal-hold" : ""}`}>
                      {e.status}
                    </td>
                    <td className="py-1 pr-2">{e.symbol || "—"}</td>
                    <td className="py-1 text-mist/70 max-w-[200px] truncate" title={e.detail || e.path}>
                      {e.path || e.detail || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
