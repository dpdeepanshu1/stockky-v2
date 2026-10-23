import { fmtInr, pickNum, pnlColor } from "./format";
import { SectionHdr } from "./parts";

// E61 - moved out of RealAutoTrade.tsx with the Live Dhan tab (code unchanged except `export` on BalanceAllocation).
// ── Balance allocation bar ───────────────────────────────────────────────────
// group208 (item 13): Dhan's positions payload has no LTP and keeps closed rows (netQty 0). The backend
// now sends `rows` (normalised, `is_open`, ltp/unrealized_pnl null when unknown). This keeps only OPEN
// rows, and falls back to the raw list for an older backend (no LTP, no P&L, never the day buy value).
export interface LivePositionRow {
  symbol: string; product: string; net_qty: number;
  avg_price: number | null; ltp: number | null; unrealized_pnl: number | null; cost_value: number | null;
}
export function openLivePositions(res: any): LivePositionRow[] {
  if (Array.isArray(res?.rows)) return res.rows.filter((r: any) => r.is_open);
  return (res?.positions || [])
    .filter((p: any) => Number(p.netQty ?? 0) !== 0)
    .map((p: any) => {
      const qty = Number(p.netQty);
      const avg = Number(p.buyAvg || p.costPrice || p.averageBuyPrice || 0) || null;
      return {
        symbol: p.tradingSymbol || p.symbol || "—", product: p.productType || p.positionType || "",
        net_qty: qty, avg_price: avg, ltp: null, unrealized_pnl: null,
        cost_value: avg ? Math.abs(qty) * avg : null,
      };
    });
}

export function BalanceAllocation({ funds, positions }: { funds: any; positions: LivePositionRow[] }) {
  const available = pickNum(funds, "availabelBalance", "availableBalance", "availableCash") ?? 0;
  const utilized = pickNum(funds, "utilizedAmount", "utilisedAmount") ?? 0;
  const collateral = pickNum(funds, "collateralAmount") ?? 0;
  const total = available + utilized + (collateral > 0 ? collateral : 0);
  const pct = (v: number) => total > 0 ? Math.round((v / total) * 100) : 0;

  // Per-position allocation from broker positions array
  const positionsValue = positions.reduce((sum, p) => sum + (p.cost_value ?? 0), 0);

  return (
    <div className="bg-graphite border border-slate rounded-2xl p-4 mb-4">
      <SectionHdr>Balance allocation</SectionHdr>
      <div className="space-y-3">
        {/* Bar */}
        <div className="h-3 rounded-full bg-ink flex overflow-hidden">
          <div className="bg-signal-buy/70 transition-all" style={{ width: `${pct(available)}%` }} />
          <div className="bg-signal-hold/70 transition-all" style={{ width: `${pct(utilized)}%` }} />
          {collateral > 0 && <div className="bg-signal-prepare/70 transition-all" style={{ width: `${pct(collateral)}%` }} />}
        </div>
        <div className="grid grid-cols-3 gap-2 text-[10px] font-display tabular-nums">
          <div><span className="inline-block w-2 h-2 rounded-sm bg-signal-buy/70 mr-1" />Available<br/><span className="text-paper font-bold">{fmtInr(available, 0)}</span></div>
          <div><span className="inline-block w-2 h-2 rounded-sm bg-signal-hold/70 mr-1" />Utilized<br/><span className="text-paper font-bold">{fmtInr(utilized, 0)}</span></div>
          {collateral > 0 && <div><span className="inline-block w-2 h-2 rounded-sm bg-signal-prepare/70 mr-1" />Collateral<br/><span className="text-paper font-bold">{fmtInr(collateral, 0)}</span></div>}
        </div>
        {positions.length > 0 && (
          <div className="border-t border-slate pt-2">
            <p className="text-[9px] font-display tabular-nums text-mist mb-1">OPEN BROKER POSITIONS</p>
            <div className="space-y-1">
              {positions.slice(0, 6).map((p, i) => {
                const sym = p.symbol || "—";
                const val = p.cost_value ?? 0;
                const pnl = p.unrealized_pnl;
                const valPct = total > 0 ? Math.round((val / total) * 100) : 0;
                return (
                  <div key={i} className="flex items-center gap-2">
                    <div className="w-24 font-display tabular-nums text-[10px] text-paper truncate">{sym}</div>
                    <div className="flex-1 h-1.5 bg-ink rounded-full overflow-hidden">
                      <div className="h-full bg-signal-hold/60 rounded-full" style={{ width: `${valPct}%` }} />
                    </div>
                    <div className="w-16 text-right font-display tabular-nums text-[10px] text-mist">{fmtInr(val, 0)}</div>
                    <div className={`w-14 text-right font-display tabular-nums text-[10px] ${pnl == null ? "text-mist" : pnlColor(pnl)}`}>{pnl == null ? "—" : `${pnl >= 0 ? "+" : ""}${fmtInr(pnl, 0)}`}</div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
