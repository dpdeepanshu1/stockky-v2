// frontend/src/components/IpoFeedHealth.tsx
//
// IPO Tracker's OWN feed-health panel — reads ipo_static_feed via
// GET /surprise/ipo/audit. This replaces the previous (incorrect) use of
// <DataHealthAudit /> inside IpoTracker.tsx, which called the SHARED
// /api/feed/audit-missing endpoint — that audits the general ~300-symbol
// stock scan universe (stockky_kv), which is why the IPO Tracker tab was
// showing unrelated stock symbols (AMBER, APOLLOHOSP, 3MINDIA, ...) instead
// of IPO rows. There is no "Repair" action here on purpose: IPO rows are
// populated by the Scan IPOs pipeline (ipo_scanner.analyze_ipo), not by the
// per-symbol technical/fundamental repair pipeline the stock feed uses — if
// an IPO row is missing fields, the fix is re-running Scan, not Repair.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

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
  health_score?: number;
  message?: string;
  error?: string;
}

export default function IpoFeedHealth() {
  const [stats, setStats] = useState<IpoAuditStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
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

  // There's no per-symbol IPO repair pipeline (unlike Hot Picks/Surprise's
  // price-only repair) — missing IPO fields only ever fill in via a real
  // re-scan (ipo_scanner.analyze_ipo). This button makes that one click
  // from the health panel itself instead of just telling you to go run
  // "Scan IPOs" above.
  const forceRescan = useCallback(async () => {
    setRescanBusy(true);
    setRescanMsg(null);
    try {
      const res = await api.forceIpoScan();
      setRescanMsg(
        res?.already_running
          ? "A scan is already running — check Scan IPOs above for progress."
          : res?.message || "Re-scan started — missing fields will fill in as it completes."
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

  return (
    <div className="rounded-lg border border-white/10 bg-black/30 p-4 mt-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <p className="text-sm font-semibold text-white/90">🗄️ IPO Database Feed Health</p>
          <p className="text-xs text-white/50">
            Tracks ipo_static_feed only — the IPO Tracker's own table, separate from the stock scan universe.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {missing > 0 && (
            <button
              className="text-xs px-3 py-1.5 rounded border border-rose-500/40 bg-rose-600/20 text-rose-200 hover:bg-rose-600/35 disabled:opacity-50"
              onClick={() => void forceRescan()}
              disabled={rescanBusy}
              title="Missing IPO fields only fill in via a real re-scan, not a price-only repair"
            >
              {rescanBusy ? "Starting…" : "⚡ Re-scan Missing"}
            </button>
          )}
          <button
            className="text-xs px-3 py-1.5 rounded border border-white/15 bg-white/5 hover:bg-white/10"
            onClick={() => void fetchAudit()}
            disabled={loading}
          >
            {loading ? "Auditing…" : "🔄 Refresh Audit"}
          </button>
        </div>
      </div>

      {error && <p className="text-xs text-rose-400 mt-2">{error}</p>}
      {rescanMsg && <p className="text-xs text-emerald-400 mt-2">{rescanMsg}</p>}


      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-3">
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
                  <td className="py-1 pr-2 text-white/60">{row.stage || "—"}</td>
                  <td className="py-1">
                    {row.missing_fields.map((f) => (
                      <span
                        key={f}
                        className="inline-block mr-1 mb-1 px-1.5 py-0.5 rounded bg-rose-500/15 text-rose-300 border border-rose-500/30 text-[10px]"
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
            Missing fields fill in via "Scan IPOs" (re-run the scan), not the stock Repair pipeline.
          </p>
        </div>
      ) : (
        <p className="text-xs text-emerald-400 mt-3">All tracked IPOs are fully scored ✓</p>
      )}
    </div>
  );
}
