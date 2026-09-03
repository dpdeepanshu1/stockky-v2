import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";

interface IncompleteStock {
  symbol: string;
  current_price: number;
  missing_fields: string[];
  updated_at?: string | null;
}

interface AuditStats {
  total_universe: number;
  fully_populated: number;
  incomplete_count: number;
  health_score: number;
  incomplete_stocks: IncompleteStock[];
  required_fields?: string[];
}

interface RefillAllJob {
  status?: "idle" | "running" | "done" | "stopped" | "error";
  total?: number;
  processed?: number;
  ok_count?: number;
  message?: string;
  last_symbol?: string;
}

export default function DataHealthAudit() {
  const [stats, setStats] = useState<AuditStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [repairingSym, setRepairingSym] = useState<string | null>(null);
  const [batchBusy, setBatchBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [refillJob, setRefillJob] = useState<RefillAllJob | null>(null);
  const refillPollRef = useRef<number | null>(null);

  const fetchAudit = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.auditMissingFeed();
      setStats(data);
    } catch (e: any) {
      setError(e?.message || "Audit failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchAudit();
  }, [fetchAudit]);

  const stopRefillPoll = useCallback(() => {
    if (refillPollRef.current != null) {
      window.clearInterval(refillPollRef.current);
      refillPollRef.current = null;
    }
  }, []);

  const pollRefillStatus = useCallback(async () => {
    try {
      const st = await api.repairFeedAllStatus();
      setRefillJob(st);
      if (st?.status !== "running") {
        stopRefillPoll();
        await fetchAudit();
      }
    } catch (e: any) {
      console.warn("refill-all status", e);
    }
  }, [fetchAudit, stopRefillPoll]);

  useEffect(() => () => stopRefillPoll(), [stopRefillPoll]);

  const handleRefillAll = async () => {
    setMessage(null);
    setError(null);
    try {
      await api.repairFeedAll();
      stopRefillPoll();
      refillPollRef.current = window.setInterval(() => void pollRefillStatus(), 2000);
      await pollRefillStatus();
    } catch (e: any) {
      setError(e?.message || "Refill All failed to start");
    }
  };

  const handleRefillAllStop = async () => {
    try {
      await api.repairFeedAllStop();
    } catch (e: any) {
      setError(e?.message || "Failed to stop Refill All");
    }
  };

  const handleRepairSingle = async (symbol: string) => {
    setRepairingSym(symbol);
    setMessage(null);
    try {
      const res = await api.repairFeedSingle(symbol);
      const patched = (res?.patched_fields || []).join(", ") || "none";
      setMessage(`${symbol}: patched [${patched}]${res?.complete ? " · complete" : ""}`);
      await fetchAudit();
    } catch (e: any) {
      setError(e?.message || `Repair failed for ${symbol}`);
    } finally {
      setRepairingSym(null);
    }
  };

  const handleRepairBatch = async () => {
    setBatchBusy(true);
    setMessage(null);
    setError(null);
    try {
      const res = await api.repairFeedBatch(15);
      setMessage(
        `Batch done: ${res?.repaired_count ?? 0} touched · ${res?.successish_count ?? 0} improved`
      );
      await fetchAudit();
    } catch (e: any) {
      setError(e?.message || "Batch repair failed");
    } finally {
      setBatchBusy(false);
    }
  };

  const rows = (stats?.incomplete_stocks || []).filter((r) => {
    if (!filter.trim()) return true;
    const q = filter.trim().toUpperCase();
    return (
      r.symbol.toUpperCase().includes(q) ||
      r.missing_fields.some((f) => f.toUpperCase().includes(q))
    );
  });

  const health = stats?.health_score ?? 0;
  const healthColor =
    health >= 90 ? "text-signal-buy" : health >= 70 ? "text-signal-hold" : "text-signal-sell";

  const refillRunning = refillJob?.status === "running";
  const refillPct =
    refillJob && refillJob.total ? Math.round(((refillJob.processed || 0) / refillJob.total) * 100) : 0;

  return (
    <div className="scan-bento-card space-y-5 mt-6">
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-slate/40 pb-4">
        <div>
          <h2 className="font-display tabular-nums text-sm text-paper flex items-center gap-2 mb-1">
            🩺 Database Feed Health
          </h2>
          <p className="text-xs text-mist/60 mb-0">
            Audit Neon feed records and surgically patch missing fields only — rate-safe batches.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void fetchAudit()}
            disabled={loading || batchBusy}
            className="scan-action-btn"
          >
            {loading ? "Auditing…" : "🔄 Refresh Audit"}
          </button>
          <button
            type="button"
            onClick={() => void handleRepairBatch()}
            disabled={loading || batchBusy || refillRunning || (stats?.incomplete_count ?? 0) === 0}
            className="scan-action-btn scan-action-trade"
          >
            {batchBusy ? "Repairing…" : "⚡ Auto-Repair Next 15"}
          </button>
          {refillRunning ? (
            <button
              type="button"
              onClick={() => void handleRefillAllStop()}
              className="scan-action-btn scan-action-danger"
            >
              ⏹ Stop Refill
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void handleRefillAll()}
              disabled={loading || batchBusy || (stats?.incomplete_count ?? 0) === 0}
              className="scan-action-btn scan-action-trade"
              title="Repairs every incomplete record in the background, in rate-safe batches — no need to click Repair repeatedly."
            >
              🚀 Refill All ({stats?.incomplete_count ?? 0})
            </button>
          )}
        </div>
      </div>

      {refillJob && (refillRunning || refillJob.status === "done" || refillJob.status === "stopped" || refillJob.status === "error") && (
        <div className="rounded-xl border border-slate/50 bg-graphite/40 p-3 space-y-1.5">
          <div className="flex items-center justify-between text-[11px] font-display tabular-nums text-mist/70">
            <span>Refill All · {refillJob.status}</span>
            <span>
              {refillJob.processed ?? 0}/{refillJob.total ?? 0} · {refillJob.ok_count ?? 0} improved
            </span>
          </div>
          <div className="h-1.5 rounded-full bg-slate/40 overflow-hidden">
            <div
              className="h-full bg-signal-buy transition-all"
              style={{ width: `${refillRunning ? refillPct : 100}%` }}
            />
          </div>
          {refillJob.message && (
            <p className="text-[10px] text-mist/50 font-display tabular-nums">{refillJob.message}</p>
          )}
        </div>
      )}

      {(message || error) && (
        <div className={`mono text-xs ${error ? "text-signal-sell" : "text-mist/80"}`}>
          {error || message}
        </div>
      )}

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="scan-stat">
          <span className="scan-stat-label">Health score</span>
          <span className={`scan-stat-value ${healthColor}`}>{health}%</span>
        </div>
        <div className="scan-stat">
          <span className="scan-stat-label">Total tracked</span>
          <span className="scan-stat-value">{stats?.total_universe ?? "—"}</span>
        </div>
        <div className="scan-stat">
          <span className="scan-stat-label">Fully populated</span>
          <span className="scan-stat-value text-signal-buy">{stats?.fully_populated ?? "—"}</span>
        </div>
        <div className="scan-stat">
          <span className="scan-stat-label">Missing data</span>
          <span className="scan-stat-value text-signal-sell">{stats?.incomplete_count ?? "—"}</span>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter symbol or field (RSI, PE…)"
          className="font-display tabular-nums text-xs bg-graphite/60 border border-slate/50 rounded-xl px-3 py-1.5 text-paper min-w-[12rem] flex-1 max-w-md"
        />
        <span className="mono text-[10px] text-mist/50">
          Showing {rows.length}
          {stats?.required_fields ? ` · need: ${stats.required_fields.join(", ")}` : ""}
        </span>
      </div>

      <div className="rounded-2xl border border-slate overflow-hidden scan-table-wrap">
        <table className="w-full text-sm font-display tabular-nums">
          <thead>
            <tr className="border-b border-slate bg-graphite">
              <th className="text-left px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Symbol</th>
              <th className="text-left px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Price</th>
              <th className="text-left px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Missing</th>
              <th className="text-right px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Action</th>
            </tr>
          </thead>
          <tbody>
            {rows.length > 0 ? (
              rows.map((item) => (
                <tr key={item.symbol} className="border-b border-slate/40 hover:bg-graphite transition">
                  <td className="px-4 py-2.5 text-paper font-semibold">{item.symbol}</td>
                  <td className="px-4 py-2.5">
                    {item.current_price > 0 ? (
                      <span className="text-paper">₹{Number(item.current_price).toLocaleString("en-IN")}</span>
                    ) : (
                      <span className="text-signal-sell">0 (missing)</span>
                    )}
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="flex flex-wrap gap-1.5">
                      {item.missing_fields.map((field) => (
                        <span
                          key={field}
                          className="px-2 py-0.5 rounded text-[10px] font-semibold uppercase bg-signal-sell/60 text-signal-sell border border-signal-sell/40"
                        >
                          {field}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      type="button"
                      onClick={() => void handleRepairSingle(item.symbol)}
                      disabled={repairingSym === item.symbol || batchBusy}
                      className="scan-action-btn"
                    >
                      {repairingSym === item.symbol ? "Patching…" : "Repair"}
                    </button>
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={4} className="text-center py-10 text-signal-buy font-medium">
                  {loading
                    ? "Loading audit…"
                    : (stats?.total_universe ?? 0) === 0
                      ? "No feed records yet — run Data Feed first, then Refresh Audit."
                      : "🎉 All tracked feed records look complete for required fields."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
