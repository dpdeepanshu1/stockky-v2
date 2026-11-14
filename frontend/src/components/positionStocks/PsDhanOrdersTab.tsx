// E538 (redesign round 5b): the "dhanorders" sub-tab of Position Stocks, moved out of PositionStocksTab.tsx (JSX unchanged).
// All state and handlers come from usePositionStocks() through `s`, so the ten sub-tabs still share one set of state.
import type { PsState } from "./usePositionStocks";

export function PsDhanOrdersTab({ s }: { s: PsState }) {
  const { positions, dhanLive, dhanLiveError, loadDhanLive } = s;
  return (
      <>
      {/* ── Live Dhan Orders ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="dash-section-title">Live Dhan Order Activity</p>
          <button onClick={() => void loadDhanLive()}
            className="px-3 py-1 rounded-lg bg-graphite border border-slate font-display tabular-nums text-[10px] text-mist">
            Refresh
          </button>
        </div>
        {dhanLiveError ? (
          <p className="font-display tabular-nums text-[11px] text-signal-sell">{dhanLiveError}</p>
        ) : !dhanLive ? (
          <p className="font-display tabular-nums text-xs text-mist">Loading…</p>
        ) : dhanLive.orders.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">No live scalp orders on Dhan right now.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-display tabular-nums">
              <thead>
                <tr className="text-mist text-left border-b border-slate">
                  <th className="py-1 pr-3">Symbol</th>
                  <th className="py-1 pr-3">Leg</th>
                  <th className="py-1 pr-3">Side</th>
                  <th className="py-1 pr-3">Qty</th>
                  <th className="py-1 pr-3">Price</th>
                  <th className="py-1 pr-3">Trigger</th>
                  <th className="py-1">Status</th>
                </tr>
              </thead>
              <tbody>
                {dhanLive.orders.map((o, i) => (
                  <tr key={`${o.orderId ?? i}-${o.legName ?? ""}`} className="border-b border-slate/50">
                    <td className="py-1 pr-3 text-paper font-bold">{String(o.tradingSymbol ?? "—")}</td>
                    <td className="py-1 pr-3 text-mist">{String(o.legName ?? "—")}</td>
                    <td className={`py-1 pr-3 ${o.transactionType === "BUY" ? "text-signal-buy" : o.transactionType === "SELL" ? "text-signal-sell" : "text-mist"}`}>
                      {String(o.transactionType ?? "—")}
                    </td>
                    <td className="py-1 pr-3 text-mist">{String(o.quantity ?? "—")}</td>
                    <td className="py-1 pr-3 text-paper">{o.price != null ? `₹${o.price}` : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{o.triggerPrice != null ? `₹${o.triggerPrice}` : "—"}</td>
                    <td className="py-1 text-paper">{String(o.orderStatus ?? "—")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="font-display tabular-nums text-[9px] text-mist mt-2">
          Pulled directly from Dhan's super-order book — broker-side truth, can lag the positions list above by up to one reconciliation cycle.
        </p>
      </div>

      </>
      );
}
