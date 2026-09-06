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
  /** Optional: when the batch button kicks off a background "repair everything"
   * job rather than one capped call, pass these to render a live progress bar
   * and a Stop button underneath it. Omit entirely for callers that still use
   * a single capped repair call (premarket/IPO tabs) — nothing changes for them. */
  repairAllActive?: boolean;
  repairAllProgress?: { processed: number; total: number } | null;
  repairAllMessage?: string | null;
  onStopRepairAll?: () => void | Promise<void>;
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
  repairAllActive, repairAllProgress, repairAllMessage, onStopRepairAll,
}: Props) {
  return (
    <div className="mt-2 mb-6 border border-slate bg-ink/40 rounded-2xl p-4 sm:p-5">
      <div className="flex flex-wrap justify-between items-center gap-3 mb-4">
        <div>
          <h3 className="font-display tabular-nums text-sm font-bold text-paper">{title}</h3>
          {subtitle && (
            <p className="font-display tabular-nums text-[10px] text-mist/50 mt-0.5">
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
          className="font-display tabular-nums text-xs px-3 py-1.5 bg-graphite text-mist rounded-xl border border-slate hover:bg-slate/40 transition disabled:opacity-50"
        >
          {healthLoading ? "Auditing…" : "🔄 Refresh Audit"}
        </button>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="p-3 rounded-xl bg-graphite/80 border border-slate/60">
          <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider">Health score</div>
          <div
            className={`font-display tabular-nums text-xl font-bold mt-1 ${
              (healthData?.health_score ?? 0) >= 90
                ? "text-signal-buy"
                : (healthData?.health_score ?? 0) >= 70
                  ? "text-signal-hold"
                  : "text-signal-sell"
            }`}
          >
            {healthData?.health_score ?? "—"}%
          </div>
        </div>
        <div className="p-3 rounded-xl bg-graphite/80 border border-slate/60">
          <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider">Total tracked</div>
          <div className="font-display tabular-nums text-xl font-bold text-paper mt-1">
            {healthData?.total_tracked ?? "—"}
          </div>
        </div>
        <div className="p-3 rounded-xl bg-graphite/80 border border-slate/60">
          <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider">Fully populated</div>
          <div className="font-display tabular-nums text-xl font-bold text-signal-buy mt-1">
            {healthData?.fully_populated ?? "—"}
          </div>
        </div>
        <div className="p-3 rounded-xl bg-graphite/80 border border-slate/60">
          <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider">Missing data</div>
          <div className="font-display tabular-nums text-xl font-bold text-signal-sell mt-1">
            {healthData?.missing_data ?? "—"}
          </div>
        </div>
      </div>
      {healthData?.message && (
        <p className="font-display tabular-nums text-[10px] text-mist/50 mt-3">{healthData.message}</p>
      )}

      {/* Empty-state: no scan has run yet today — explain rather than show 0/0/0% */}
      {!healthLoading && (healthData?.total_tracked ?? 0) === 0 && (
        <div className="mt-4 rounded-xl border border-slate/40 bg-graphite/40 px-4 py-5 text-center space-y-1">
          <p className="font-display tabular-nums text-xs text-paper/80">
            No scan results yet — run <strong className="text-paper">Search Hot Picks Stocks</strong> to populate this feed.
          </p>
          <p className="font-display tabular-nums text-[10px] text-mist/50">
            Optionally click <strong className="text-signal-prepare">☀ Premarket</strong> first to warm up prices (closes market: yesterday&apos;s bhavcopy · live market: real-time LTP).
          </p>
          <p className="font-display tabular-nums text-[10px] text-mist/40">
            The repair buttons below activate automatically once the first scan writes rows.
          </p>
        </div>
      )}

      <div className="flex flex-wrap gap-2 mt-4">
        <button
          type="button"
          onClick={() => void onRepairBatch()}
          disabled={
            batchRepairBusy ||
            healthLoading ||
            // Enable whenever there is anything repairable. The backend reports
            // price-only gaps in `missing_data` but the full repairable set
            // (price + decision + score) lives in `incomplete_stocks`, which is
            // exactly what the table below renders. Gating on `missing_data`
            // left this button greyed out whenever only decision/score were
            // missing — the common case — so repair "did nothing".
            ((healthData?.incomplete_stocks?.length ?? healthData?.missing_data ?? 0) === 0)
          }
          className="font-display tabular-nums text-xs px-3 py-1.5 rounded-xl bg-signal-sell/20 text-white border border-signal-sell/40 hover:bg-signal-sell/35 transition disabled:opacity-50"
        >
          {batchRepairBusy ? "Repairing…" : repairBatchLabel}
        </button>
      </div>

      {repairAllActive && (
        <div className="mt-3 space-y-1.5">
          <div className="h-1.5 w-full rounded-full bg-graphite/80 overflow-hidden">
            <div
              className="h-full bg-signal-buy transition-all duration-500"
              style={{
                width:
                  repairAllProgress?.total
                    ? `${Math.max(4, Math.min(100, Math.round((repairAllProgress.processed / repairAllProgress.total) * 100)))}%`
                    : "30%",
              }}
            />
          </div>
          <div className="flex items-center justify-between gap-2">
            <p className="font-display tabular-nums text-[10px] text-mist/60">
              {repairAllMessage || "Repairing…"}
            </p>
            {onStopRepairAll && (
              <button
                type="button"
                onClick={() => void onStopRepairAll()}
                className="font-display tabular-nums text-[10px] px-2 py-1 rounded-lg bg-graphite text-mist border border-slate hover:bg-slate/40 transition shrink-0"
              >
                ⏹ Stop
              </button>
            )}
          </div>
        </div>
      )}

      {(healthData?.incomplete_stocks?.length ?? 0) > 0 && (
        <div className="mt-4 overflow-x-auto rounded-xl border border-slate/60">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="text-mist/50 border-b border-slate/60 font-display tabular-nums text-[10px] uppercase tracking-wider">
                <th className="py-2 px-3">Symbol</th>
                <th className="py-2 px-3">Missing</th>
                <th className="py-2 px-3 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {(healthData?.incomplete_stocks || []).map((stock) => (
                <tr key={stock.symbol} className="border-b border-slate/40 hover:bg-ink/40">
                  <td className="py-2 px-3 font-display tabular-nums font-semibold text-paper">{stock.symbol}</td>
                  <td className="py-2 px-3">
                    {(stock.missing_fields || ["price"]).map((m) => (
                      <span
                        key={m}
                        className="inline-block bg-signal-sell/40 text-signal-sell px-1.5 py-0.5 rounded mr-1 text-[10px] uppercase"
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
                      className="font-display tabular-nums text-[11px] px-2 py-1 bg-graphite text-mist rounded border border-slate hover:bg-slate/40 disabled:opacity-50"
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
