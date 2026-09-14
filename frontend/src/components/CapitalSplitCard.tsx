// frontend/src/components/CapitalSplitCard.tsx
//
// Shared "Capital Split" block — shown on BOTH Real Automatic Trade's
// Live Dhan tab (below its actual-balance BalanceAllocation card) and
// Position Stocks' Overview tab (below its Dhan Account card).
//
// WHY THIS EXISTS (user request): both dashboards already show the raw,
// actual Dhan account balance (the whole pot), but neither ever showed
// what each SERVICE's own share of that pot is. position-stocks-service
// has always tracked its own 50% pool explicitly (capital/ledger.py,
// config.SCALP_POOL_CAPITAL_SHARE_PCT) — real-trade-service has not.
// This card surfaces both sides in one place, on both dashboards, so an
// operator can see the whole picture without switching tabs/services.
//
// IMPORTANT AUDIT NOTE (surfaced in the card itself, not hidden in a
// comment): position-stocks-service's "Allocated"/"Available" figures
// below are its REAL, DB-tracked capital ledger (capital/ledger.py) —
// entries/exits actually move this number. real-trade-service has NO
// equivalent ledger today (see execution/equity_sync.py — REAL mode's
// cash_available/current_equity are synced from the FULL live Dhan
// balance, not a reserved half). So "Real Trade Service" below is
// necessarily a COMPUTED reference figure (total balance minus whatever
// Position Stocks has allocated), not something real-trade-service
// itself enforces yet. Making that asymmetry visible on the card is the
// whole point — silently presenting a computed number as if it were a
// real, enforced pool would be misleading.
function fmtInr(n: number | null | undefined, decimals = 0): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1_00_00_000) return `${sign}₹${(abs / 1_00_00_000).toFixed(1)}Cr`;
  if (abs >= 1_00_000) return `${sign}₹${(abs / 1_00_000).toFixed(1)}L`;
  return `${sign}₹${abs.toLocaleString("en-IN", { maximumFractionDigits: decimals })}`;
}

export interface CapitalSplitCardProps {
  // Raw, live, whole-account balance — same figure each dashboard's own
  // actual-balance block already shows (Dhan's availabelBalance/etc).
  totalBalance: number | null;
  // config.SCALP_POOL_CAPITAL_SHARE_PCT from position-stocks-service's
  // GET /status (e.g. 50). Null if position-stocks-service isn't reachable.
  splitPct: number | null;

  // position-stocks-service's own tracked capital ledger (GET /ledger) —
  // real, DB-backed figures, not computed here.
  positionStocksAllocated: number | null;
  positionStocksAvailable: number | null;
  positionStocksError?: string | null;

  // real-trade-service's own account bookkeeping (GET /status/REAL's
  // `account` block) — its cash_available/current_equity reflect the
  // FULL account balance today (see note above), shown alongside the
  // computed reference share for transparency.
  realTradeCashAvailable: number | null;
  realTradeError?: string | null;

  // Whichever dashboard is rendering this card — only changes which side
  // gets a "(this service)" badge.
  highlight: "position_stocks" | "real_trade";
}

export default function CapitalSplitCard({
  totalBalance,
  splitPct,
  positionStocksAllocated,
  positionStocksAvailable,
  positionStocksError,
  realTradeCashAvailable,
  realTradeError,
  highlight,
}: CapitalSplitCardProps) {
  const pct = splitPct ?? 50;
  // Prefer the real, tracked allocation when we have it; fall back to a
  // flat split of the live total when position-stocks-service isn't
  // reachable from this dashboard (e.g. its URL isn't configured here).
  const psAllocated = positionStocksAllocated ?? (totalBalance != null ? totalBalance * (pct / 100) : null);
  const rtAllocatedComputed =
    totalBalance != null && psAllocated != null
      ? Math.max(0, totalBalance - psAllocated)
      : totalBalance != null
      ? totalBalance * ((100 - pct) / 100)
      : null;

  return (
    <div className="bg-graphite border border-slate rounded-2xl p-4">
      <div className="flex items-center justify-between mb-3">
        <p className="dash-section-title">Capital split — shared Dhan account</p>
        <span className="font-display tabular-nums text-[10px] px-2 py-0.5 rounded-full border bg-signal-prepare/10 border-signal-prepare/30 text-signal-prepare">
          {pct}% / {100 - pct}%
        </span>
      </div>

      <p className="font-display tabular-nums text-[10px] text-mist mb-3">
        Total live balance (Dhan): <span className="text-paper font-bold">{fmtInr(totalBalance)}</span>
      </p>

      <div className="grid grid-cols-2 gap-2">
        {/* Position Stocks pool */}
        <div className={`rounded-xl p-3 border ${highlight === "position_stocks" ? "bg-signal-buy/5 border-signal-buy/30" : "bg-ink border-slate"}`}>
          <p className="font-display tabular-nums text-[9px] uppercase tracking-widest text-mist">
            Position Stocks ({pct}%){highlight === "position_stocks" ? " · this service" : ""}
          </p>
          {positionStocksError ? (
            <p className="font-display tabular-nums text-[10px] text-signal-sell mt-1">{positionStocksError}</p>
          ) : (
            <>
              <p className="font-display tabular-nums text-sm font-bold text-paper mt-1">
                Allocated {fmtInr(psAllocated)}
              </p>
              <p className="font-display tabular-nums text-[11px] text-mist">
                Available <span className="text-signal-buy font-bold">{fmtInr(positionStocksAvailable)}</span>
              </p>
            </>
          )}
        </div>

        {/* Real Trade Service pool */}
        <div className={`rounded-xl p-3 border ${highlight === "real_trade" ? "bg-signal-buy/5 border-signal-buy/30" : "bg-ink border-slate"}`}>
          <p className="font-display tabular-nums text-[9px] uppercase tracking-widest text-mist">
            Real Trade Service ({100 - pct}%){highlight === "real_trade" ? " · this service" : ""}
          </p>
          <p className="font-display tabular-nums text-sm font-bold text-paper mt-1">
            Allocated (computed) {fmtInr(rtAllocatedComputed)}
          </p>
          {realTradeError ? (
            <p className="font-display tabular-nums text-[10px] text-signal-sell mt-1">{realTradeError}</p>
          ) : (
            <p className="font-display tabular-nums text-[11px] text-mist">
              Available (reported) <span className="text-signal-buy font-bold">{fmtInr(realTradeCashAvailable)}</span>
            </p>
          )}
        </div>
      </div>

      <p className="font-display tabular-nums text-[9px] text-mist mt-3 leading-relaxed">
        ⚠ Position Stocks' figures come from its own capital ledger (real, DB-tracked —
        entries/exits move it). Real Trade Service has no equivalent reserved pool today: its
        "Available" is the full account balance it currently sizes trades off of, not just this
        computed {100 - pct}% share — the "Allocated (computed)" figure above is a reference
        split only, not something Real Trade Service enforces internally yet.
      </p>
    </div>
  );
}
