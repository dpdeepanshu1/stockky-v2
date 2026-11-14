import { useCallback, useEffect, useState } from "react";
import { setVisibleInterval } from "../visibleInterval";
import { api } from "../api";
import { Button, DataTable, Empty, ErrorState, StatusPill, type PillState } from "./kit";   // E545

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

// E545: a level is a status pill (words + its own dot shape), not only a coloured box
function levelState(level: string): PillState {
  if (level === "critical") return "offline";
  if (level === "warn" || level === "degraded") return "paused";
  if (level === "watch") return "info";
  return "live";
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
    return setVisibleInterval(load, 30000);        // group 184: paused while the browser tab is hidden
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
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </Button>
      </div>

      {error && <ErrorState title={error} onRetry={load} />}

      {data && (
        <>
          <div className="k-card quiet">
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <span className="mono text-sm font-medium">
                Overall: <StatusPill state={levelState(data.overall)}>{data.overall}</StatusPill>
              </span>
              <span className="mono text-[10px] opacity-80">
                {data.events_1h} events · 429={data.by_status_1h?.["429"] || 0} · 503={data.by_status_1h?.["503"] || 0}
              </span>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {data.upstreams?.map((u) => (
              <div key={u.id} className="k-card quiet">
                <div className="flex justify-between items-baseline gap-2">
                  <span className="mono text-xs font-medium">{u.label}</span>
                  <StatusPill state={levelState(u.level)}>{u.level}</StatusPill>
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
            <div className="k-card quiet">
              <h4 className="mono text-[10px] text-mist uppercase tracking-widest mb-2">Circuits</h4>
              <div className="space-y-1">
                {data.circuits.map((c, i) => (
                  <div key={c.name || i} className="flex justify-between mono text-[11px] gap-2">
                    <span>{c.name || "—"}</span>
                    <StatusPill state={c.state === "open" ? "offline" : c.state === "half_open" ? "paused" : "live"}>
                      {c.state || "?"}{typeof c.failures === "number" ? ` (${c.failures} fails)` : ""}
                    </StatusPill>
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

          <div className="k-card quiet">
            <h4 className="mono text-[10px] text-mist uppercase tracking-widest mb-2">Recent events</h4>
            <DataTable
              dense
              rows={data.recent_events || []}
              rowKey={(e) => `${e.ts}-${e.source}-${e.status}-${e.symbol || ""}-${e.path || ""}`}
              empty={<Empty title="No rate-limit events recorded yet." />}
              columns={[
                { key: "ts", header: "IST", render: (e) => fmtTime(e.ts) },
                { key: "src", header: "Src", render: (e) => e.source },
                { key: "code", header: "Code", render: (e) => (e.status === 429 || e.status === 503 ? <b>{e.status}</b> : e.status) },
                { key: "sym", header: "Symbol", render: (e) => e.symbol || "—" },
                { key: "path", header: "Path / detail", render: (e) => <span title={e.detail || e.path}>{e.path || e.detail || "—"}</span> },
              ]}
            />
          </div>
        </>
      )}
    </div>
  );
}
