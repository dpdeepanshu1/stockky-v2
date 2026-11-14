import { Button, DataTable, Stat, Stats } from "./kit";   // E545

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
  /** group302: set when the last audit failed or timed out; shows the message and turns the button into Retry */
  auditError?: string | null;
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
  title, subtitle, healthData, healthLoading, auditError,
  onRefreshAudit, onRepairBatch, onRepairSingle,
  batchRepairBusy, patchingSymbol, repairBatchLabel = "⚡ Auto-Repair Missing (15)",
}: Props) {
  return (
    <div className="k-card mt-2 mb-6">
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
        <Button variant="ghost" size="sm" onClick={() => void onRefreshAudit()} disabled={healthLoading}>
          {healthLoading ? "Auditing…" : auditError ? "🔄 Retry Audit" : "🔄 Refresh Audit"}
        </Button>
      </div>
      {auditError && !healthLoading && (
        <p className="font-display tabular-nums text-[11px] text-signal-sell mb-3" role="alert">
          {auditError}
        </p>
      )}

      <Stats>
        {typeof healthData?.health_score === "number" && Number.isFinite(healthData.health_score)
          ? (
            <Stat
              label="Health score" size="sm" value={`${healthData.health_score}%`}
              tone={healthData.health_score >= 90 ? "up" : healthData.health_score >= 70 ? "warn" : "down"}
              delta={healthData.health_score >= 90 ? "Healthy" : healthData.health_score >= 70 ? "Needs attention" : "Poor"}
            />
          )
          : <Stat label="Health score" size="sm" value="—" sub="not reported" />}
        <Stat label="Total tracked" size="sm" value={healthData?.total_tracked ?? "—"} />
        <Stat label="Fully populated" size="sm" value={healthData?.fully_populated ?? "—"} />
        <Stat label="Missing data" size="sm" value={healthData?.missing_data ?? "—"} />
      </Stats>
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
        <Button
          variant="soft"
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
        >
          {batchRepairBusy ? "Repairing…" : repairBatchLabel}
        </Button>
      </div>

      {(healthData?.incomplete_stocks?.length ?? 0) > 0 && (
        <div className="mt-4">
          <DataTable
            dense
            rows={healthData?.incomplete_stocks || []}
            rowKey={(stock) => stock.symbol}
            columns={[
              { key: "sym", header: "Symbol", render: (stock) => <b>{stock.symbol}</b> },
              { key: "missing", header: "Missing", render: (stock) => (
                <>
                  {(stock.missing_fields || ["price"]).map((m) => (
                    <span key={m} className="inline-block bg-signal-sell/40 text-signal-sell px-1.5 py-0.5 rounded mr-1 text-[10px] uppercase">{m}</span>
                  ))}
                </>
              ) },
              { key: "act", header: "Action", align: "right", render: (stock) => (
                <Button variant="ghost" size="sm" onClick={() => void onRepairSingle(stock.symbol)} disabled={patchingSymbol === stock.symbol || batchRepairBusy}>
                  {patchingSymbol === stock.symbol ? "Patching…" : "Repair"}
                </Button>
              ) },
            ]}
          />
        </div>
      )}
    </div>
  );
}
