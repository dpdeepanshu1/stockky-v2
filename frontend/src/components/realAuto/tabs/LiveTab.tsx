import type { DhanStatus, GateStatus } from "../../../realTradeApi";
import CapitalSplitCard from "../../CapitalSplitCard";
import StockCandleBlock from "../../StockCandleBlock";
import { GlintBox, PriceRoll } from "../../ui/motion";
import { fmtInr, groupDhanOrdersBySymbol, pickNum, pnlColor, positionLevels } from "../format";
import { BalanceAllocation, type LivePositionRow } from "../liveParts";
import { SectionHdr } from "../parts";
import type { Mode } from "../types";

// E61 - E31 step 5: the "live" (Live Dhan) tab, moved out of RealAutoTrade.tsx (JSX unchanged).
export interface LiveTabProps {
  mode: Mode;
  loggedIn: boolean;
  status: GateStatus | null;
  dhanAccount: (DhanStatus & { funds: any; funds_error: string | null }) | null;
  livePositions: LivePositionRow[];
  liveHoldings: any[];
  liveDhanOrders: any[];
  liveLoading: boolean;
  liveError: string | null;
  loadLiveDhanData: () => Promise<void>;
  psSplitPct: number | null;
  psAllocated: number | null;
  psAvailable: number | null;
  psError: string | null;
  soldTodayBySymbol: Map<string, { qty: number; avgPrice: number }>;
}

export function LiveTab({ mode, loggedIn, status, dhanAccount, livePositions, liveHoldings, liveDhanOrders, liveLoading, liveError, loadLiveDhanData, psSplitPct, psAllocated, psAvailable, psError, soldTodayBySymbol }: LiveTabProps) {
  return (
            <div className="space-y-4">
              {mode !== "REAL" ? (
                <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                  <p className="font-display tabular-nums text-sm text-mist">Switch to REAL mode and log in to see live Dhan account data.</p>
                </div>
              ) : !loggedIn ? (
                <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                  <p className="font-display tabular-nums text-sm text-mist">Log in to view live Dhan data.</p>
                </div>
              ) : (
                <>
                  <div className="flex items-center justify-between">
                    <SectionHdr>Live data from Dhan broker</SectionHdr>
                    <button onClick={() => void loadLiveDhanData()} disabled={liveLoading}
                      className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist disabled:opacity-40">
                      {liveLoading ? "Loading…" : "↻ Refresh all"}
                    </button>
                  </div>

                  {liveError && (
                    <div className="px-3 py-2 rounded-xl bg-signal-sell/40 border border-signal-sell/30 font-display tabular-nums text-xs text-signal-sell">{liveError}</div>
                  )}

                  {/* Balance allocation */}
                  {dhanAccount?.funds && (
                    <BalanceAllocation funds={dhanAccount.funds} positions={livePositions} />
                  )}

                  {/* Capital split (this service + Position Stocks Service) */}
                  {dhanAccount?.funds && (
                    <CapitalSplitCard
                      totalBalance={pickNum(dhanAccount.funds, "availabelBalance", "availableBalance", "availableCash")}
                      splitPct={psSplitPct}
                      positionStocksAllocated={psAllocated}
                      positionStocksAvailable={psAvailable}
                      positionStocksError={psError}
                      realTradeCashAvailable={status?.account?.cash_available ?? null}
                      highlight="real_trade"
                    />
                  )}

                  {/* Live positions */}
                  <div className="bg-graphite border border-slate rounded-2xl p-4">
                    <SectionHdr>Open positions at Dhan ({livePositions.length})</SectionHdr>
                    {livePositions.length === 0 ? (
                      <p className="font-display tabular-nums text-[11px] text-mist">No open positions at broker.</p>
                    ) : (
                      <div className="space-y-2">
                        {livePositions.map((p, i) => {
                          const sym = p.symbol || "—";
                          const qty = p.net_qty;
                          const avg = p.avg_price;
                          const ltp = p.ltp;
                          const pnl = p.unrealized_pnl;   // null = unknown (never the day buy value)
                          const product = p.product;
                          return (
                            <GlintBox key={i} value={ltp} className="bg-ink rounded-xl px-3 py-2 border border-slate">
                              <div className="flex items-center justify-between">
                                <div>
                                  <span className="font-display tabular-nums text-sm font-bold text-paper">{sym}</span>
                                  <span className="font-display tabular-nums text-[10px] text-mist ml-2">{product}</span>
                                </div>
                                <span className={`font-display tabular-nums text-sm font-bold ${pnl == null ? "text-mist" : pnlColor(pnl)}`}>
                                  {pnl == null ? "—" : `${pnl >= 0 ? "+" : ""}${fmtInr(pnl, 2)}`}
                                </span>
                              </div>
                              <div className="flex gap-4 mt-1 font-display tabular-nums text-[10px] text-mist">
                                <span>Qty <span className="text-paper">{qty}</span></span>
                                <span>Avg <span className="text-paper">{avg != null ? `₹${avg.toFixed(2)}` : "—"}</span></span>
                                <span>LTP <span className="text-paper"><PriceRoll value={ltp} text={ltp != null ? `₹${ltp.toFixed(2)}` : "—"} /></span></span>
                                <span>Val <span className="text-paper">{p.cost_value != null ? fmtInr(p.cost_value, 0) : "—"}</span></span>
                              </div>
                              {sym !== "—" && <StockCandleBlock symbol={sym} defaultOpen={false} levels={positionLevels(avg, null, null)} />}
                            </GlintBox>
                          );
                        })}
                      </div>
                    )}
                  </div>

                  {/* Live holdings — raw, read-only snapshot straight from Dhan.
                      2026-09-17 note: this is deliberately NOT where you sell —
                      every holding here gets auto-imported into a tracked
                      position every cycle (see import_broker_holdings) and then
                      shows up below under "Demat Positions" with the same
                      manual Sell button and the same auto-pilot stop/target
                      checks every other position gets. This table exists only
                      so you can sanity-check what Dhan itself reports. */}
                  <div className="bg-graphite border border-slate rounded-2xl p-4">
                    <SectionHdr>Demat holdings ({liveHoldings.length})</SectionHdr>
                    <p className="font-display tabular-nums text-[9px] text-mist mb-2">
                      Read-only broker snapshot. To sell, use "Demat Positions" below — it's the
                      same holding, tracked and auto-pilot-managed.
                    </p>
                    {liveHoldings.length === 0 ? (
                      <p className="font-display tabular-nums text-[11px] text-mist">No holdings in demat.</p>
                    ) : (
                      <div className="overflow-x-auto">
                        <table className="w-full font-display tabular-nums text-[11px]">
                          <thead>
                            <tr className="text-mist border-b border-slate">
                              <th className="text-left py-1 pr-3">Symbol</th>
                              <th className="text-right pr-3">Qty</th>
                              <th className="text-right pr-3">Avg Cost</th>
                              <th className="text-right pr-3">LTP</th>
                              <th className="text-right">P&L</th>
                            </tr>
                          </thead>
                          <tbody>
                            {liveHoldings.map((h, i) => {
                              const sym = h.tradingSymbol || h.symbol || "—";
                              const totalQty = Number(h.totalQty || h.quantity || 0);
                              const avg = Number(h.avgCostPrice || h.averageBuyPrice || 0);
                              const ltp = Number(h.lastTradedPrice || h.ltp || 0);
                              const sold = soldTodayBySymbol.get(sym);
                              const soldQty = sold ? Math.min(sold.qty, totalQty) : 0;
                              const remainingQty = totalQty - soldQty;
                              const unrealizedPnl = remainingQty > 0 ? (ltp - avg) * remainingQty : null;
                              const realizedPnl = soldQty > 0 ? (sold!.avgPrice - avg) * soldQty : null;
                              return (
                                <tr key={i} className="border-b border-slate hover:bg-ink">
                                  <td className="py-1.5 pr-3 text-paper font-bold">
                                    {sym}
                                    {soldQty > 0 && (
                                      <span className="block font-normal text-[9px] text-signal-hold">
                                        sold {soldQty} today
                                      </span>
                                    )}
                                  </td>
                                  <td className="text-right pr-3 text-mist">
                                    {remainingQty}
                                    {soldQty > 0 && (
                                      <span className="block text-[9px] text-mist">
                                        ({totalQty} total · settling)
                                      </span>
                                    )}
                                  </td>
                                  <td className="text-right pr-3 text-mist">₹{avg.toFixed(2)}</td>
                                  <td className="text-right pr-3 text-mist">{remainingQty > 0 ? `₹${ltp.toFixed(2)}` : "—"}</td>
                                  <td className="text-right">
                                    {unrealizedPnl != null && (
                                      <div className={pnlColor(unrealizedPnl)}>
                                        {unrealizedPnl >= 0 ? "+" : ""}{fmtInr(unrealizedPnl, 0)}
                                      </div>
                                    )}
                                    {realizedPnl != null && (
                                      <div className={`text-[9px] ${pnlColor(realizedPnl)}`}>
                                        {realizedPnl >= 0 ? "+" : ""}{fmtInr(realizedPnl, 0)} realized
                                      </div>
                                    )}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>

                  {/* Live orders from Dhan — 2026-09-11 fix: grouped by
                      symbol with a buy/sell/P&L summary line after any
                      symbol that has a completed round trip today (see
                      groupDhanOrdersBySymbol). */}
                  <div className="bg-graphite border border-slate rounded-2xl p-4">
                    <SectionHdr>Today's orders at Dhan ({liveDhanOrders.length})</SectionHdr>
                    {liveDhanOrders.length === 0 ? (
                      <p className="font-display tabular-nums text-[11px] text-mist">No orders placed today.</p>
                    ) : (
                      <div className="space-y-3 max-h-60 overflow-y-auto">
                        {groupDhanOrdersBySymbol(liveDhanOrders, liveHoldings).map((grp) => (
                          <div key={grp.symbol} className="space-y-1.5">
                            {grp.orders.map((o, i) => {
                              const side = o.transactionType || o.side || "";
                              const qty = Number(o.quantity || 0);
                              const price = Number(o.price || o.averageTradedPrice || 0);
                              const status = (o.orderStatus || o.status || "").toUpperCase();
                              const statusColor = status === "TRADED" || status === "FILLED" ? "text-signal-buy"
                                : status === "REJECTED" || status === "CANCELLED" ? "text-signal-sell"
                                : "text-signal-hold";
                              return (
                                <div key={i} className="flex items-center justify-between bg-ink rounded-xl px-3 py-1.5 border border-slate">
                                  <div className="flex items-center gap-2 font-display tabular-nums text-[11px]">
                                    <span className={`font-bold ${side === "BUY" ? "text-signal-buy" : "text-signal-sell"}`}>{side}</span>
                                    <span className="text-paper">{grp.symbol}</span>
                                    <span className="text-mist">×{qty}</span>
                                    {price > 0 && <span className="text-mist">@ ₹{price.toFixed(2)}</span>}
                                  </div>
                                  <span className={`font-display tabular-nums text-[10px] ${statusColor}`}>{status}</span>
                                </div>
                              );
                            })}
                            {grp.pnl != null && (
                              <div className={`font-display tabular-nums text-[10px] px-3 ${grp.pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                                {grp.symbol}: bought @ ₹{grp.buyAvgPrice!.toFixed(2)}
                                {grp.buySource === "holding" ? " (holding)" : ""} → sold @ ₹{grp.sellAvgPrice!.toFixed(2)}
                                {" "}×{grp.matchedQty} = {grp.pnl >= 0 ? "profit" : "loss"} ₹{Math.abs(grp.pnl).toFixed(2)}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
  );
}
