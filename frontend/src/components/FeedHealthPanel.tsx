import { useState } from "react";

export interface IncompleteStock {
  symbol: string;
  missing_fields?: string[];
}

export interface FeedHealthData {
  health_score?: number;
  total_tracked?: number;
  fully_populated?: number;
  missing_data?: number;
  incomplete_stocks?: IncompleteStock[];
  market_open?: boolean | null;
  source?: string;
  message?: string;
}

interface Props {
  title: string;
  subtitle?: string;
  healthData: FeedHealthData | null;
  healthLoading: boolean;
  onRefreshAudit: () => void | Promise<void>;
  onRepairBatch: () => void | Promise<void>;
  onRepairSingle: (symbol: string) => void | Promise<void>;
  batchRepairBusy: boolean;
  patchingSymbol: string | null;
  repairBatchLabel?: string;
}

/**
 * Extracted verbatim from SurpriseStocks.tsx's "Premarket Feed Health"
 * panel (same markup, same classes) so Hot Picks — and any future tab
 * that needs this — gets identical behavior instead of a hand-rebuilt
 * near-copy that inevitably drifts from the original over time. All
 * state (loading, busy flags, data) stays owned by the parent; this is
 * pure presentation + the two button callbacks.
 */
export default function FeedHealthPanel({
  title, subtitle, healthData, healthLoading,
  onRefreshAudit, onRepairBatch, onRepairSingle,
  batchRepairBusy, patchingSymbol, repairBatchLabel = "⚡ Auto-Repair Missing (15)",
}: Props) {
  return (
    <div className="mt-2 mb-6 border border-slate bg-ink/40 rounded-xl p-4 sm:p-5">
      <div className="flex flex-wrap justify-between items-center gap-3 mb-4">
        <div>
          <h3 className="font-mono text-sm font-bold text-paper">{title}</h3>
          {subtitle && (
            <p className="font-mono text-[10px] text-mist/50 mt-0.5">
              {subtitle}
              {healthData?.market_open != null
                ? healthData.market_open
                  ? " · Market OPEN"
                  : " · Market CLOSED"
                : ""}
              {healthData?.source ? ` · source: ${healthData.source}` : ""}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => void onRefreshAudit()}
          disabled={healthLoading}
          className="font-mono text-xs px-3 py-1.5 bg-graphite text-mist rounded-lg border border-slate hover:bg-slate/40 transition disabled:opacity-50"
        >
          {healthLoading ? "Auditing…" : "🔄 Refresh Audit"}
        </button>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="p-3 rounded-lg bg-graphite/80 border border-slate/60">
          <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">Health score</div>
          <div
            className={`font-mono text-xl font-bold mt-1 ${
              (healthData?.health_score ?? 0) >= 90
                ? "text-emerald-400"
                : (healthData?.health_score ?? 0) >= 70
                  ? "text-amber-300"
                  : "text-rose-400"
            }`}
          >
            {healthData?.health_score ?? "—"}%
          </div>
        </div>
        <div className="p-3 rounded-lg bg-graphite/80 border border-slate/60">
          <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">Total tracked</div>
          <div className="font-mono text-xl font-bold text-paper mt-1">
            {healthData?.total_tracked ?? "—"}
          </div>
        </div>
        <div className="p-3 rounded-lg bg-graphite/80 border border-slate/60">
          <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">Fully populated</div>
          <div className="font-mono text-xl font-bold text-emerald-400 mt-1">
            {healthData?.fully_populated ?? "—"}
          </div>
        </div>
        <div className="p-3 rounded-lg bg-graphite/80 border border-slate/60">
          <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">Missing data</div>
          <div className="font-mono text-xl font-bold text-rose-400 mt-1">
            {healthData?.missing_data ?? "—"}
          </div>
        </div>
      </div>
      {healthData?.message && (
        <p className="font-mono text-[10px] text-mist/50 mt-3">{healthData.message}</p>
      )}

      <div className="flex flex-wrap gap-2 mt-4">
        <button
          type="button"
          onClick={() => void onRepairBatch()}
          disabled={batchRepairBusy || healthLoading || (healthData?.missing_data ?? 0) === 0}
          className="font-mono text-xs px-3 py-1.5 rounded-lg bg-rose-600/20 text-rose-200 border border-rose-500/40 hover:bg-rose-600/35 transition disabled:opacity-50"
        >
          {batchRepairBusy ? "Repairing…" : repairBatchLabel}
        </button>
      </div>

      {(healthData?.incomplete_stocks?.length ?? 0) > 0 && (
        <div className="mt-4 overflow-x-auto rounded-lg border border-slate/60">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="text-mist/50 border-b border-slate/60 font-mono text-[10px] uppercase tracking-wider">
                <th className="py-2 px-3">Symbol</th>
                <th className="py-2 px-3">Missing</th>
                <th className="py-2 px-3 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {(healthData?.incomplete_stocks || []).map((stock) => (
                <tr key={stock.symbol} className="border-b border-slate/40 hover:bg-ink/40">
                  <td className="py-2 px-3 font-mono font-semibold text-paper">{stock.symbol}</td>
                  <td className="py-2 px-3">
                    {(stock.missing_fields || ["price"]).map((m) => (
                      <span
                        key={m}
                        className="inline-block bg-rose-900/40 text-rose-300 px-1.5 py-0.5 rounded mr-1 text-[10px] uppercase"
                      >
                        {m}
                      </span>
                    ))}
                  </td>
                  <td className="py-2 px-3 text-right">
                    <button
                      type="button"
                      onClick={() => void onRepairSingle(stock.symbol)}
                      disabled={patchingSymbol === stock.symbol || batchRepairBusy}
                      className="font-mono text-[11px] px-2 py-1 bg-graphite text-mist rounded border border-slate hover:bg-slate/40 disabled:opacity-50"
                    >
                      {patchingSymbol === stock.symbol ? "Patching…" : "Repair"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
