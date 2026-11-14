// E538 (redesign round 5b): the "history" sub-tab of Position Stocks, moved out of PositionStocksTab.tsx (JSX unchanged).
// All state and handlers come from usePositionStocks() through `s`, so the ten sub-tabs still share one set of state.
import { entryReason } from "../../setupView";
import { isShort, sideBadge } from "../../positionSide";
import { type Window, fmtInr, fmtDateTimeIst, statusColor, PositionRow } from "./parts";
import type { PsState } from "./usePositionStocks";

export function PsHistoryTab({ s }: { s: PsState }) {
  const { tradeHistory, tradeHistoryRange, setTradeHistoryRange } = s;
  return (
      <>
      {/* ── Trade history ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="dash-section-title">Trade History — Buy vs Sell, Win Rate</p>
          {/* this session: Today/Last-3-days subtabs the user asked for.
              Backend also only retains 3 days (see config.
              TRADE_HISTORY_RETENTION_DAYS + orders/reconcile.py::
              run_retention_cleanup()), so "3d" is effectively "all". */}
          <div className="flex gap-1">
            {(["today", "3d"] as const).map(r => (
              <button
                key={r}
                onClick={() => setTradeHistoryRange(r)}
                className={`px-2.5 py-1 rounded-lg font-display tabular-nums text-[10px] uppercase border ${
                  tradeHistoryRange === r
                    ? "bg-signal-buy/20 border-signal-buy/40 text-signal-buy"
                    : "border-slate text-mist"
                }`}
              >
                {r === "today" ? "Today" : "Last 3 Days"}
              </button>
            ))}
          </div>
        </div>
        {tradeHistory ? (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
              <div>
                <p className="text-[9px] text-mist uppercase tracking-widest">Total Trades</p>
                <p className="font-display tabular-nums font-bold text-sm text-paper">{tradeHistory.summary.total_trades}</p>
              </div>
              <div>
                <p className="text-[9px] text-mist uppercase tracking-widest">Win Rate</p>
                <p className="font-display tabular-nums font-bold text-sm text-paper">
                  {tradeHistory.summary.win_rate_pct != null ? `${tradeHistory.summary.win_rate_pct}%` : "—"}
                  <span className="text-[10px] text-mist ml-1">({tradeHistory.summary.wins}W / {tradeHistory.summary.losses}L{(tradeHistory.summary.breakeven ?? 0) > 0 ? ` / ${tradeHistory.summary.breakeven}BE` : ""})</span>
                </p>
              </div>
              <div>
                <p className="text-[9px] text-mist uppercase tracking-widest">Total P&L</p>
                <p className={`font-display tabular-nums font-bold text-sm ${tradeHistory.summary.total_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                  {fmtInr(tradeHistory.summary.total_pnl)}
                </p>
              </div>
              <div>
                <p className="text-[9px] text-mist uppercase tracking-widest">Best / Worst</p>
                <p className="font-display tabular-nums text-[11px]">
                  {tradeHistory.summary.best_trade
                    ? <span className="text-signal-buy">{tradeHistory.summary.best_trade.symbol} {fmtInr(tradeHistory.summary.best_trade.pnl)}</span>
                    : <span className="text-mist">—</span>}
                  <span className="text-mist"> / </span>
                  {tradeHistory.summary.worst_trade
                    ? <span className="text-signal-sell">{tradeHistory.summary.worst_trade.symbol} {fmtInr(tradeHistory.summary.worst_trade.pnl)}</span>
                    : <span className="text-mist">—</span>}
                </p>
              </div>
            </div>

            {/* Win/loss breakdown bar */}
            {tradeHistory.summary.total_trades > 0 && (
              <div className="mb-3">
                <div className="h-1.5 rounded-full overflow-hidden flex">
                  <div className="bg-signal-buy h-full transition-all"
                    style={{ width: `${((tradeHistory.summary.wins / tradeHistory.summary.total_trades) * 100).toFixed(1)}%` }} />
                  <div className="bg-mist/40 h-full"
                    style={{ width: `${(((tradeHistory.summary.breakeven ?? 0) / tradeHistory.summary.total_trades) * 100).toFixed(1)}%` }} />
                  <div className="bg-signal-sell h-full flex-1" />
                </div>
                <div className="flex justify-between font-display tabular-nums text-[9px] text-mist mt-1">
                  <span>Wins: {tradeHistory.summary.wins}</span>
                  {(tradeHistory.summary.breakeven ?? 0) > 0 && <span>Breakeven: {tradeHistory.summary.breakeven}</span>}
                  <span>Losses: {tradeHistory.summary.losses}</span>
                </div>
              </div>
            )}
            {((tradeHistory.summary.pending_reconcile ?? 0) > 0 || (tradeHistory.summary.error_trades ?? 0) > 0) && (
              <p className="font-display tabular-nums text-[10px] text-mist mb-3">
                Not in the totals above:
                {(tradeHistory.summary.pending_reconcile ?? 0) > 0 && ` ${tradeHistory.summary.pending_reconcile} exit(s) waiting for the real fill price`}
                {(tradeHistory.summary.pending_reconcile ?? 0) > 0 && (tradeHistory.summary.error_trades ?? 0) > 0 && ","}
                {(tradeHistory.summary.error_trades ?? 0) > 0 && ` ${tradeHistory.summary.error_trades} ERROR row(s)`}
              </p>
            )}

            {tradeHistory.trades.length === 0 ? (
              <p className="font-display tabular-nums text-xs text-mist">No trades recorded yet.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-[11px] font-display tabular-nums">
                  <thead>
                    <tr className="text-mist text-left border-b border-slate">
                      <th className="py-1 pr-3">Symbol</th>
                      <th className="py-1 pr-3">Window</th>
                      <th className="py-1 pr-3">Entry reason</th>
                      <th className="py-1 pr-3">Qty</th>
                      <th className="py-1 pr-3">Buy ₹</th>
                      <th className="py-1 pr-3">Sell ₹</th>
                      <th className="py-1 pr-3">Buy Total</th>
                      <th className="py-1 pr-3">Sell Total</th>
                      <th className="py-1 pr-3">P&L</th>
                      <th className="py-1 pr-3">Status</th>
                      <th className="py-1 pr-3">Closed</th>
                      {/* AUDIT ADD (session22 cont'd): the table had no
                          order-ID column at all — dhan_super_order_id was
                          only ever shown in the Overview subtab's
                          PositionRow cards, not here, even though the
                          backend now returns entry and exit order ids too
                          (see main.py::trades_history). */}
                      <th className="py-1">Order IDs</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tradeHistory.trades.map(t => (
                      <tr key={t.id} className="border-b border-slate/50">
                        <td className="py-1 pr-3 text-paper font-bold">{t.symbol}{isShort(t) && <span className="ml-1 text-[9px] text-signal-sell" title="Short trade">{sideBadge(t)}</span>}</td>
                        <td className="py-1 pr-3 text-mist">{t.window_source}</td>
                        {/* E272: the rule that took the trade, from the setup stamp stored on the trade (hover for trigger, confirmations, penalties, size) */}
                        {(() => { const er = entryReason(t.setup); return (
                          <td className={`py-1 pr-3 ${er.known ? "text-paper" : "text-mist"}`} title={er.title} data-testid="entry-reason">
                            {er.label}{er.known && <span className="ml-1 text-[9px] text-mist">{er.slot}</span>}
                          </td>); })()}
                        <td className="py-1 pr-3 text-mist">{t.quantity}</td>
                        <td className="py-1 pr-3 text-paper">₹{t.entry_price.toFixed(2)}</td>
                        <td className="py-1 pr-3 text-paper">{t.exit_price != null ? `₹${t.exit_price.toFixed(2)}` : "—"}</td>
                        {/* AUDIT ADD (this session): buy/sell VALUE (price × qty), not
                            just per-share price — "total price" the user actually paid/
                            received on the leg, same distinction real-trade-service's
                            Charges tab makes between per-share price and order value. */}
                        <td className="py-1 pr-3 text-mist">{fmtInr(t.entry_price * t.quantity)}</td>
                        <td className="py-1 pr-3 text-mist">{t.exit_price != null ? fmtInr(t.exit_price * t.quantity) : "—"}</td>
                        <td className={`py-1 pr-3 ${t.realized_pnl != null && t.realized_pnl >= 0 ? "text-signal-buy" : t.realized_pnl != null ? "text-signal-sell" : "text-mist"}`}>
                          {t.realized_pnl != null ? `${fmtInr(t.realized_pnl)} (${(t.realized_pnl_pct ?? 0).toFixed(2)}%)` : "—"}
                        </td>
                        <td className={`py-1 pr-3 ${statusColor(t.status)}`}>{t.status}</td>
                        <td className="py-1 pr-3 text-mist">{fmtDateTimeIst(t.closed_at)}</td>
                        <td className="py-1 text-mist text-[9px] leading-tight">
                          {t.dhan_super_order_id ? <div>Entry {t.dhan_super_order_id}</div> : null}
                          {t.dhan_exit_order_id && t.dhan_exit_order_id !== t.dhan_super_order_id
                            ? <div>Exit {t.dhan_exit_order_id}</div>
                            : null}
                          {!t.dhan_super_order_id && !t.dhan_exit_order_id ? "—" : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        ) : (
          <p className="font-display tabular-nums text-xs text-mist">Loading trade history…</p>
        )}
      </div>

      </>
      );
}
