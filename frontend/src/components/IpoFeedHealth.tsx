// frontend/src/components/IpoFeedHealth.tsx
//
// IPO Tracker's OWN feed-health panel — reads ipo_static_feed via
// GET /ipo/audit. This replaces the previous (incorrect) use of
// <DataHealthAudit /> inside IpoTracker.tsx, which called the SHARED
// /api/feed/audit-missing endpoint — that audits the general ~300-symbol
// stock scan universe (stockky_kv), which is why the IPO Tracker tab was
// showing unrelated stock symbols (AMBER, APOLLOHOSP, 3MINDIA, ...) instead
// of IPO rows.
//
// Auto-Repair here (POST /ipo/repair-batch) re-runs analyze_ipo() only for
// the specific symbols missing a field — a bounded, targeted batch, same
// shape as Hot Picks'/Surprise's repair-batch — NOT the "re-scan the whole
// universe" full scan this panel used to trigger instead (kept below as a
// fallback "Full Re-scan" action for when you actually want that).
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

// Same small spinner pattern used elsewhere in the app (ScanPanel,
// DecisionCard, BuySniperModal, StockChart, and now IpoTracker.tsx).
function BusySpinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block w-3 h-3 rounded-full border-2 border-t-transparent animate-spin ${className}`}
    />
  );
}

interface MissingIpo {
  symbol: string;
  company_name?: string;
  stage?: string;
  missing_fields: string[];
  updated_at?: string;
}

interface IpoAuditStats {
  ok: boolean;
  total_tracked?: number;
  fully_scored?: number;
  missing_count?: number;
  missing_ipos?: MissingIpo[];
  no_data_yet_count?: number;
  no_data_yet_ipos?: MissingIpo[];
  health_score?: number;
  message?: string;
  error?: string;
}

export default function IpoFeedHealth({ onRepairComplete }: { onRepairComplete?: () => void } = {}) {
  const [stats, setStats] = useState<IpoAuditStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [repairBusy, setRepairBusy] = useState(false);
  const [repairMsg, setRepairMsg] = useState<string | null>(null);
  const [rescanBusy, setRescanBusy] = useState(false);
  const [rescanMsg, setRescanMsg] = useState<string | null>(null);

  const fetchAudit = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.ipoAudit();
      setStats(data);
    } catch (e: any) {
      setError(e?.message || "IPO audit failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchAudit();
  }, [fetchAudit]);

  // Targeted repair — bounded to the symbols actually missing a field
  // (up to 20 per click), not a blanket re-scan of everything tracked.
  const autoRepairAll = useCallback(async () => {
    setRepairBusy(true);
    setRepairMsg(null);
    setError(null);
    try {
      // Use the full missing_count so one click repairs everything, not just 20.
      const limit = Math.max(20, stats?.missing_count ?? 20);
      const res = await api.ipoRepairBatch(limit);
      if (res.status === "completed") {
        setRepairMsg(res.message || (res.repaired?.length > 0
          ? `Repaired ${res.repaired.length} symbol(s): ${(res.repaired || []).join(", ")}`
          : "Nothing needed repair."));
        // Notify parent (IpoTracker) to reload its card list so repaired
        // scores appear immediately without a manual page refresh.
        onRepairComplete?.();
      } else {
        setRepairMsg(res.message || res.error || "Repair did not complete.");
      }
      await fetchAudit();
    } catch (e: any) {
      setRepairMsg(null);
      setError(e?.message || "Failed to run Auto-Repair");
    } finally {
      setRepairBusy(false);
    }
  }, [fetchAudit, onRepairComplete, stats?.missing_count]);

  // Kept as a secondary action for when a targeted repair isn't enough
  // (e.g. NSE/ipoalerts discovery itself needs refreshing, not just
  // scoring for symbols already known).
  const forceRescan = useCallback(async () => {
    setRescanBusy(true);
    setRescanMsg(null);
    try {
      const res = await api.forceIpoScan();
      setRescanMsg(
        res?.already_running
          ? "A scan is already running — check Scan IPOs above for progress."
          : res?.message || "Full re-scan started — missing fields will fill in as it completes."
      );
    } catch (e: any) {
      setRescanMsg(null);
      setError(e?.message || "Failed to start re-scan");
    } finally {
      setRescanBusy(false);
    }
  }, []);

  const total = stats?.total_tracked ?? 0;
  const scored = stats?.fully_scored ?? 0;
  const missing = stats?.missing_count ?? 0;
  const health = stats?.health_score ?? 0;
  const noDataYet = stats?.no_data_yet_count ?? 0;

  return (
    <div className="rounded-xl border border-white/10 bg-black/30 p-4 mt-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <p className="text-sm font-semibold text-white/90">🗄️ IPO Database Feed Health</p>
          <p className="text-xs text-white/50">
            Tracks ipo_static_feed only — the IPO Tracker's own table, separate from the stock scan universe.
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {missing > 0 && (
            <button
              className="text-xs px-3 py-1.5 rounded border border-signal-buy/40 bg-signal-buy/20 text-white hover:bg-signal-buy/35 disabled:opacity-50"
              onClick={() => void autoRepairAll()}
              disabled={repairBusy}
              title="Re-runs the analysis only for symbols currently missing a field (bounded batch), not a full re-scan"
            >
              {repairBusy ? (
                <span className="inline-flex items-center gap-1.5">
                  <BusySpinner className="border-white" /> Repairing…
                </span>
              ) : (
                `🛠 Auto-Repair All${missing > 0 ? ` (${missing})` : ""}`
              )}
            </button>
          )}
          {/* Bug fix (30-Aug session): this used to be gated on `missing > 0`
              only. When total_tracked is ALSO 0 (nothing has ever been
              scanned/persisted yet — e.g. first run, or the DB write path
              silently failed), missing_count is 0 too (0 missing out of 0
              rows), so this button — the one action that actually gets the
              table populated — was invisible in exactly the situation the
              user needed it most. Now it also shows whenever total === 0. */}
          {(missing > 0 || total === 0) && (
            <button
              className="text-xs px-3 py-1.5 rounded border border-signal-sell/40 bg-signal-sell/20 text-white hover:bg-signal-sell/35 disabled:opacity-50"
              onClick={() => void forceRescan()}
              disabled={rescanBusy}
              title={
                total === 0
                  ? "No IPOs tracked yet — run a full scan from upstream to populate the database"
                  : "Full universe re-scan from upstream — use if Auto-Repair isn't enough (e.g. discovery itself is stale)"
              }
            >
              {rescanBusy ? (
                <span className="inline-flex items-center gap-1.5">
                  <BusySpinner className="border-white" /> Starting…
                </span>
              ) : total === 0 ? (
                "⚡ Run Initial Scan"
              ) : (
                "⚡ Full Re-scan"
              )}
            </button>
          )}
          <button
            className="text-xs px-3 py-1.5 rounded border border-white/15 bg-white/5 hover:bg-white/10"
            onClick={() => void fetchAudit()}
            disabled={loading}
          >
            {loading ? (
              <span className="inline-flex items-center gap-1.5">
                <BusySpinner className="border-white/60" /> Auditing…
              </span>
            ) : (
              "🔄 Refresh Audit"
            )}
          </button>
        </div>
      </div>

      {error && <p className="text-xs text-signal-sell mt-2">{error}</p>}
      {/* Bug fix: /ipo/audit's `message`/`error` fields (e.g. "No IPO
          database configured — scan results are cache-only.") were parsed
          into IpoAuditStats but never rendered — the panel just showed
          confusing 0/0/0/0% with no explanation of *why*. This is very
          likely what a misconfigured/undeployed DB looks like in
          production, indistinguishable in the UI from "scan ran but wrote
          zero rows" until now. */}
      {!error && stats && (stats.message || stats.error) && (
        <p className="text-xs text-signal-hold mt-2">⚠️ {stats.message || stats.error}</p>
      )}
      {repairMsg && <p className="text-xs text-signal-buy mt-2">{repairMsg}</p>}
      {rescanMsg && <p className="text-xs text-signal-buy mt-2">{rescanMsg}</p>}



      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 mt-3">
        <div className="rounded border border-white/10 p-3">
          <p className="text-[10px] uppercase tracking-wide text-white/40">Health Score</p>
          <p className="text-lg font-semibold">{health}%</p>
        </div>
        <div className="rounded border border-white/10 p-3">
          <p className="text-[10px] uppercase tracking-wide text-white/40">Total Tracked</p>
          <p className="text-lg font-semibold">{total}</p>
        </div>
        <div className="rounded border border-white/10 p-3">
          <p className="text-[10px] uppercase tracking-wide text-white/40">Fully Scored</p>
          <p className="text-lg font-semibold">{scored}</p>
        </div>
        <div className="rounded border border-white/10 p-3">
          <p className="text-[10px] uppercase tracking-wide text-white/40">Missing Data</p>
          <p className="text-lg font-semibold">{missing}</p>
        </div>
        {/* no_data_yet: shown only when non-zero — it's a distinct bucket
            (waiting on Yahoo), not a broken/missing row, so it shouldn't
            inflate the "Missing Data" number or the repair queue. */}
        {noDataYet > 0 && (
          <div className="rounded border border-signal-hold/30 bg-signal-hold/5 p-3">
            <p className="text-[10px] uppercase tracking-wide text-signal-hold/70">Awaiting Yahoo</p>
            <p className="text-lg font-semibold text-signal-hold">{noDataYet}</p>
          </div>
        )}
      </div>

      {total === 0 ? (
        <p className="text-xs text-white/50 mt-3">
          No IPO rows tracked yet — run "Scan IPOs" above first, then Refresh Audit.
        </p>
      ) : missing > 0 ? (
        <div className="mt-3 max-h-64 overflow-y-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-white/40">
                <th className="py-1 pr-2">Symbol</th>
                <th className="py-1 pr-2">Stage</th>
                <th className="py-1">Missing</th>
              </tr>
            </thead>
            <tbody>
              {(stats?.missing_ipos || []).map((row) => (
                <tr key={row.symbol} className="border-t border-white/5">
                  <td className="py-1 pr-2 font-medium">{row.symbol}</td>
                  <td className="py-1 pr-2 text-white/60">
                    {row.stage === "no_data_yet" ? "⏳ Awaiting Yahoo"
                      : row.stage === "error" ? "⚠ Error"
                      : row.stage || "—"}
                  </td>
                  <td className="py-1">
                    {row.missing_fields.map((f) => (
                      <span
                        key={f}
                        className="inline-block mr-1 mb-1 px-1.5 py-0.5 rounded bg-signal-sell/15 text-signal-sell border border-signal-sell/30 text-[10px]"
                      >
                        {f}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-[11px] text-white/40 mt-2">
            Auto-Repair re-scores just these symbols; Full Re-scan re-discovers the whole universe from upstream.
          </p>
        </div>
      ) : (
        <p className="text-xs text-signal-buy mt-3">All tracked IPOs are fully scored ✓</p>
      )}

      {/* Waiting-on-Yahoo section — separate from the "broken" missing table.
          These rows aren't fixable today; they need Yahoo to crawl the ticker.
          Show them collapsed so the user knows they exist but isn't alarmed. */}
      {noDataYet > 0 && (stats?.no_data_yet_ipos || []).length > 0 && (
        <details className="mt-3">
          <summary className="text-[11px] text-signal-hold/80 cursor-pointer select-none">
            ⏳ {noDataYet} symbol(s) waiting on Yahoo price data — click to expand
          </summary>
          <div className="mt-2 max-h-40 overflow-y-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-white/40">
                  <th className="py-1 pr-2">Symbol</th>
                  <th className="py-1">Company</th>
                </tr>
              </thead>
              <tbody>
                {(stats?.no_data_yet_ipos || []).map((row) => (
                  <tr key={row.symbol} className="border-t border-white/5">
                    <td className="py-1 pr-2 font-medium text-signal-hold">{row.symbol}</td>
                    <td className="py-1 text-white/50 text-[11px]">{row.company_name || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="text-[11px] text-white/40 mt-2">
              Yahoo Finance hasn't indexed these tickers yet — common for new NSE SME listings.
              They'll resolve automatically once Yahoo's crawl picks them up (usually 1–5 days).
              Auto-Repair skips them so the repair count stays honest.
            </p>
          </div>
        </details>
      )}
    </div>
  );
}
