import type { ChargesPeriod, CumulativeBrokerage, GateStatus, OrderRow } from "../../../realTradeApi";
import { chargesForLegs, sumCharges, DP_CHARGE_INCL_GST, type ChargeBreakdown, type ChargeLeg } from "../../../chargesRates";
import { fmtChargeDay, fmtInr, pnlColor } from "../format";
import { SectionHdr, StatCard } from "../parts";
import type { Mode } from "../types";

// E63 - E31 step 7: the "charges" tab, moved out of RealAutoTrade.tsx (body unchanged: it was an inline function in the JSX).
export interface ChargesTabProps {
  mode: Mode;
  loggedIn: boolean;
  status: GateStatus | null;
  orders: OrderRow[];
  liveDhanOrders: any[];
  liveLoading: boolean;
  loadLiveDhanData: () => Promise<void>;
  cumBrokerage: CumulativeBrokerage | null;
  cumBrokerageError: string | null;
}

export function ChargesTab({ mode, loggedIn, status, orders, liveDhanOrders, liveLoading, loadLiveDhanData, cumBrokerage, cumBrokerageError }: ChargesTabProps) {
            // group 262: rates and the DP rule live in ../chargesRates.ts (one card shared with Position Stocks and
            // matching the backend ledger): delivery STT 0.1% on BUY and SELL, intraday STT on the SELL only,
            // exchange 0.00297% + 0.0001% IPFT, GST also on SEBI, DP Rs 12.50 + GST once per scrip per day and not
            // for shares bought the same day.

            // Compute charges from all orders in Dhan + our own orders
            // Build from liveDhanOrders (real broker data) when in REAL mode
            interface OrderCharge {
              symbol: string; side: string; qty: number; price: number;
              isDelivery: boolean; charges: ChargeBreakdown; time: string;
            }

            // group 261: Dhan's order book is the WHOLE shared account, so it also lists position-stocks-service's
            // MIS legs (e.g. TRIVENI). /dhan/orders now tags each order `ours` (its orderId is one of this
            // service's trade_orders). Only `ours !== false` orders count here; an untagged list (older backend,
            // failed lookup) keeps every order as before. The rest is shown as a separate note, not summed.
            const filledDhanOrders = liveDhanOrders.filter(o => {
              const qty = Number(o.quantity || 0);
              const price = Number(o.price || o.averageTradedPrice || 0);
              const st = (o.orderStatus || o.status || "").toUpperCase();
              return qty > 0 && price > 0 && (st === "TRADED" || st === "FILLED");
            });
            const toLeg = (o: any): ChargeLeg & { time: string } => {
              const product = (o.productType || o.positionType || "").toUpperCase();
              return {
                symbol: o.tradingSymbol || o.symbol || "—",
                side: (o.transactionType || o.side || "").toUpperCase(),
                qty: Number(o.quantity || 0),
                price: Number(o.price || o.averageTradedPrice || 0),
                isDelivery: product === "CNC" || product === "DELIVERY",
                time: o.createTime || o.updateTime || "",
              };
            };
            const priceLegs = (orders: any[]): OrderCharge[] => {
              const legs = orders.map(toLeg);
              const priced = chargesForLegs(legs);       // DP needs the whole day's legs, so price them together
              return legs.map((l, i) => ({ ...l, charges: priced[i] }));
            };
            const orderCharges: OrderCharge[] = priceLegs(filledDhanOrders.filter(o => o.ours !== false));
            const otherServiceCharges: OrderCharge[] = priceLegs(filledDhanOrders.filter(o => o.ours === false));
            const otherServiceTotal = otherServiceCharges.reduce((s, o) => s + o.charges.total, 0);

            // Today's totals. The backend ledger prices EVERY filled order of this service (including market sells the
            // live order book lists without a price), so it is the headline whenever it has at least as many orders as
            // the live list; the live list is the fallback (ledger not loaded yet / lagging a fresh fill).
            const liveTotals = sumCharges(orderCharges.map(o => o.charges));
            const ledgerToday = cumBrokerage?.today;
            const useLedgerToday = !!ledgerToday && ledgerToday.orders >= orderCharges.length;
            const todayCount = useLedgerToday && ledgerToday ? ledgerToday.orders : orderCharges.length;
            const T: ChargeBreakdown = useLedgerToday && ledgerToday
              ? { ...ledgerToday.components, total: ledgerToday.all_charges }
              : liveTotals;
            const totalBrokerage = T.brokerage;
            const totalSTT = T.stt;
            const totalExchange = T.exchange;
            const totalSEBI = T.sebi;
            const totalGST = T.gst;
            const totalStamp = T.stamp;
            const totalDP = T.dp;
            const grandTotal = T.total;

            // Pair BUY+SELL for same symbol to get round-trip charges
            const roundTripMap: Record<string, { buy?: OrderCharge; sell?: OrderCharge }> = {};
            for (const oc of orderCharges) {
              if (!roundTripMap[oc.symbol]) roundTripMap[oc.symbol] = {};
              if (oc.side === "BUY") roundTripMap[oc.symbol].buy = oc;
              else roundTripMap[oc.symbol].sell = oc;
            }

            return (
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <SectionHdr>Dhan charges breakdown</SectionHdr>
                  {mode === "REAL" && loggedIn && (
                    <button onClick={() => void loadLiveDhanData()} disabled={liveLoading}
                      className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist disabled:opacity-40">
                      {liveLoading ? "…" : "↻ Refresh"}
                    </button>
                  )}
                </div>

                {mode !== "REAL" ? (
                  <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                    <p className="font-display tabular-nums text-sm text-mist">Switch to REAL mode to see actual Dhan charges.</p>
                  </div>
                ) : !loggedIn ? (
                  <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                    <p className="font-display tabular-nums text-sm text-mist">Log in to view charges.</p>
                  </div>
                ) : (
                  <>
                    {/* Summary cards */}
                    <div className="bg-graphite border border-slate rounded-2xl p-4">
                      {/* BUG FIX (2026-09-07): header used to say "All-time charges (today's
                          Dhan orders)" — self-contradictory, and misleading: Dhan's order-list
                          API (what liveDhanOrders is built from) only ever returns TODAY's
                          orders, there is no true all-time total anywhere in this view. Labeled
                          accurately now; a real all-time figure would need Dhan's separate
                          trade-history/ledger API (date-range), which isn't wired up yet. */}
                      <SectionHdr>Today's Dhan charges</SectionHdr>
                      <div className="grid grid-cols-2 gap-2 mb-3">
                        <StatCard label="Total charges paid" value={`-${fmtInr(grandTotal, 2)}`} color="text-signal-sell" sub={`${todayCount} filled Real Auto Trade orders today`} />
                        <StatCard label="Brokerage" value={`-${fmtInr(totalBrokerage, 2)}`} color="text-signal-sell" sub="₹20 cap per leg" />
                        <StatCard label="STT" value={`-${fmtInr(totalSTT, 2)}`} color="text-signal-sell" sub="Securities Transaction Tax" />
                        <StatCard label="GST + Exchange" value={`-${fmtInr(totalGST + totalExchange, 2)}`} color="text-signal-sell" sub="18% GST on brokerage + exchange + SEBI" />
                      </div>
                      {/* NEW (2026-09-07): the Overview/Positions P&L is raw (exit_price -
                          entry_price) * qty — it never subtracts brokerage/STT/GST/DP. This is
                          the one place in the app that nets today's realized P&L against
                          today's actual charges, so "did I really make/lose money today" has an
                          honest answer somewhere. */}
                      {status?.account?.realized_pnl_today != null && (
                        <div className="bg-ink border border-slate rounded-xl p-3 mb-3">
                          <div className="flex items-center justify-between font-display tabular-nums text-[11px] text-mist mb-1">
                            <span>Realized P&L today (price only)</span>
                            <span className={pnlColor(status.account.realized_pnl_today)}>
                              {status.account.realized_pnl_today >= 0 ? "+" : ""}{fmtInr(status.account.realized_pnl_today, 2)}
                            </span>
                          </div>
                          <div className="flex items-center justify-between font-display tabular-nums text-[11px] text-mist mb-1">
                            <span>− Charges today</span>
                            <span className="text-signal-sell">-{fmtInr(grandTotal, 2)}</span>
                          </div>
                          <div className="flex items-center justify-between font-display tabular-nums text-xs font-bold border-t border-slate pt-1.5 mt-1">
                            <span className="text-paper">Net realized P&L today (after charges)</span>
                            <span className={pnlColor(status.account.realized_pnl_today - grandTotal)}>
                              {(status.account.realized_pnl_today - grandTotal) >= 0 ? "+" : ""}{fmtInr(status.account.realized_pnl_today - grandTotal, 2)}
                            </span>
                          </div>
                        </div>
                      )}
                      {otherServiceCharges.length > 0 && (
                        <p className="font-display tabular-nums text-[10px] text-mist mb-3">
                          Not counted above: {otherServiceCharges.length} filled order{otherServiceCharges.length === 1 ? "" : "s"} on the same Dhan
                          account that Real Auto Trade did not place (e.g. Position Stocks), -{fmtInr(otherServiceTotal, 2)} — see the
                          Position Stocks Charges tab.
                        </p>
                      )}
                      <div className="border-t border-slate pt-3">
                        <p className="font-display tabular-nums text-[10px] text-mist mb-2 uppercase tracking-widest">Full breakdown</p>
                        <div className="space-y-1.5 font-display tabular-nums text-[11px]">
                          {[
                            { label: "Brokerage", val: totalBrokerage, note: "₹0 delivery, ₹20 or 0.03%/leg intraday" },
                            { label: "STT", val: totalSTT, note: "0.025% intraday (sell only), 0.1% delivery (buy + sell)" },
                            { label: "Exchange Txn", val: totalExchange, note: "0.00297% NSE + 0.0001% IPFT" },
                            { label: "SEBI charges", val: totalSEBI, note: "0.0001% on turnover" },
                            { label: "GST", val: totalGST, note: "18% on brokerage + exchange + SEBI" },
                            { label: "Stamp duty", val: totalStamp, note: "0.015% delivery, 0.003% intraday (buy side)" },
                            { label: "DP charges", val: totalDP, note: `₹12.50 + GST (₹${DP_CHARGE_INCL_GST.toFixed(2)}) per scrip sold from demat per day` },
                          ].map(row => (
                            <div key={row.label} className="flex items-center justify-between">
                              <div>
                                <span className="text-paper">{row.label}</span>
                                <span className="text-mist text-[9px] ml-2">{row.note}</span>
                              </div>
                              <span className={row.val > 0 ? "text-signal-sell" : "text-mist"}>
                                {row.val > 0 ? `-${fmtInr(row.val, 2)}` : "—"}
                              </span>
                            </div>
                          ))}
                          <div className="flex items-center justify-between border-t border-slate pt-2 mt-1">
                            <span className="font-bold text-paper">Total charges</span>
                            <span className="font-bold text-signal-sell">-{fmtInr(grandTotal, 2)}</span>
                          </div>
                        </div>
                      </div>
                    </div>

                    {/* group 259: charges since the first filled order — date range, today, total */}
                    <div className="bg-graphite border border-slate rounded-2xl p-4">
                      <SectionHdr>Charges since start{cumBrokerage?.since ? ` (from ${fmtChargeDay(cumBrokerage.since)})` : ""}</SectionHdr>
                      {cumBrokerageError && <p className="font-display tabular-nums text-[10px] text-signal-sell mb-2">{cumBrokerageError}</p>}
                      {cumBrokerage ? (
                        <>
                          <div className="space-y-1.5 font-display tabular-nums text-[11px]">
                            <div className="flex items-center justify-between text-[9px] text-mist uppercase tracking-widest">
                              <span>Period</span><span>Brokerage · All charges</span>
                            </div>
                            {([
                              cumBrokerage.history && { label: `${fmtChargeDay(cumBrokerage.history.from)} – ${fmtChargeDay(cumBrokerage.history.to)}`, p: cumBrokerage.history, bold: false },
                              { label: "Today", p: cumBrokerage.today, bold: false },
                              { label: "Total", p: cumBrokerage.total, bold: true },
                            ].filter(Boolean) as { label: string; p: ChargesPeriod; bold: boolean }[]).map(r => (
                              <div key={r.label} className={`flex items-center justify-between ${r.bold ? "border-t border-slate pt-2 mt-1 font-bold" : ""}`}>
                                <span className="text-paper">{r.label}</span>
                                <span className="text-mist">
                                  <span className={r.p.brokerage > 0 ? "text-signal-sell" : "text-mist"}>{r.p.brokerage > 0 ? `-${fmtInr(r.p.brokerage, 2)}` : "—"}</span>
                                  {" · "}<span className="text-signal-sell">-{fmtInr(r.p.all_charges, 2)}</span>
                                </span>
                              </div>
                            ))}
                          </div>
                          <p className="font-display tabular-nums text-[10px] text-mist mt-2">
                            Total split: STT {fmtInr(cumBrokerage.total.components.stt, 2)} · DP {fmtInr(cumBrokerage.total.components.dp, 2)} · GST {fmtInr(cumBrokerage.total.components.gst, 2)} · Exch+SEBI {fmtInr(cumBrokerage.total.components.exchange + cumBrokerage.total.components.sebi, 2)} · Stamp {fmtInr(cumBrokerage.total.components.stamp, 2)}
                          </p>
                          {cumBrokerage.pnl && (
                            <div className="bg-ink border border-slate rounded-xl p-3 mt-3">
                              <div className="flex items-center justify-between font-display tabular-nums text-[11px] text-mist mb-1">
                                <span>Realized P&L all-time (price only)</span>
                                <span className={pnlColor(cumBrokerage.pnl.realized_gross_total)}>
                                  {cumBrokerage.pnl.realized_gross_total >= 0 ? "+" : ""}{fmtInr(cumBrokerage.pnl.realized_gross_total, 2)}
                                </span>
                              </div>
                              <div className="flex items-center justify-between font-display tabular-nums text-[11px] text-mist mb-1">
                                <span>− Charges since start</span>
                                <span className="text-signal-sell">-{fmtInr(cumBrokerage.pnl.charges_total, 2)}</span>
                              </div>
                              <div className="flex items-center justify-between font-display tabular-nums text-xs font-bold border-t border-slate pt-1.5 mt-1">
                                <span className="text-paper">Net realized P&L all-time (after charges)</span>
                                <span className={pnlColor(cumBrokerage.pnl.net_realized_total)}>
                                  {cumBrokerage.pnl.net_realized_total >= 0 ? "+" : ""}{fmtInr(cumBrokerage.pnl.net_realized_total, 2)}
                                </span>
                              </div>
                            </div>
                          )}
                          {cumBrokerage.pnl_daily && cumBrokerage.pnl_daily.length > 0 && (
                            <div className="mt-3 pt-3 border-t border-slate">
                              <p className="text-[9px] text-mist uppercase tracking-widest mb-1.5">Recent days (stored) · Gross · Charges · Net</p>
                              <div className="space-y-1 font-display tabular-nums text-[10px]">
                                {cumBrokerage.pnl_daily.map(d => (
                                  <div key={d.day} className="flex items-center justify-between">
                                    <span className="text-paper">{fmtChargeDay(d.day)} <span className="text-mist text-[9px]">{d.orders} orders</span></span>
                                    <span className="text-mist">
                                      {d.realized_gross != null ? <span className={pnlColor(d.realized_gross)}>{d.realized_gross >= 0 ? "+" : "-"}{fmtInr(Math.abs(d.realized_gross), 2)}</span> : "—"}
                                      {" · "}<span className="text-signal-sell">-{fmtInr(d.charges, 2)}</span>
                                      {" · "}{d.net_realized != null ? <span className={pnlColor(d.net_realized)}>{d.net_realized >= 0 ? "+" : "-"}{fmtInr(Math.abs(d.net_realized), 2)}</span> : "—"}
                                    </span>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                          {(cumBrokerage.orders_estimated ?? 0) > 0 && (
                            <p className="font-display tabular-nums text-[9px] text-mist mt-2">
                              {cumBrokerage.orders_estimated} older sell order{cumBrokerage.orders_estimated === 1 ? "" : "s"} had no recorded price and {cumBrokerage.orders_estimated === 1 ? "is" : "are"} priced at the last buy of that stock (estimate).
                            </p>
                          )}
                          <p className="font-display tabular-nums text-[9px] text-mist mt-2">
                            Real Auto Trade orders only. Delivery brokerage is ₹0, so the cost is mostly STT + DP. {cumBrokerage.note}
                          </p>
                        </>
                      ) : !cumBrokerageError ? (
                        <p className="font-display tabular-nums text-[10px] text-mist">Loading…</p>
                      ) : null}
                    </div>

                    {/* Per-order breakdown */}
                    {orderCharges.length > 0 && (
                      <div className="bg-graphite border border-slate rounded-2xl p-4">
                        <SectionHdr>Per trade charges (today)</SectionHdr>
                        <div className="space-y-2">
                          {orderCharges.map((oc, i) => (
                            <div key={i} className="bg-ink border border-slate rounded-xl p-3">
                              <div className="flex items-center justify-between mb-2">
                                <div className="flex items-center gap-2">
                                  <span className={`font-display tabular-nums text-xs font-bold ${oc.side === "BUY" ? "text-signal-buy" : "text-signal-sell"}`}>{oc.side}</span>
                                  <span className="font-display tabular-nums text-sm font-bold text-paper">{oc.symbol}</span>
                                  <span className="font-display tabular-nums text-[9px] text-mist">{oc.isDelivery ? "CNC" : "MIS"}</span>
                                </div>
                                <span className="font-display tabular-nums text-xs text-signal-sell font-bold">-{fmtInr(oc.charges.total, 2)}</span>
                              </div>
                              <div className="flex gap-3 font-display tabular-nums text-[10px] text-mist mb-2">
                                <span>Qty <span className="text-paper">{oc.qty}</span></span>
                                <span>Price <span className="text-paper">₹{oc.price.toFixed(2)}</span></span>
                                <span>Value <span className="text-paper">{fmtInr(oc.qty * oc.price, 0)}</span></span>
                              </div>
                              <div className="grid grid-cols-4 gap-1.5 font-display tabular-nums text-[9px] text-mist">
                                <div>Brokerage<br/><span className="text-paper">₹{oc.charges.brokerage.toFixed(2)}</span></div>
                                <div>STT<br/><span className="text-paper">₹{oc.charges.stt.toFixed(2)}</span></div>
                                <div>Exch+SEBI<br/><span className="text-paper">₹{(oc.charges.exchange + oc.charges.sebi).toFixed(2)}</span></div>
                                <div>GST+Stamp{oc.charges.dp > 0 ? "+DP" : ""}<br/><span className="text-paper">₹{(oc.charges.gst + oc.charges.stamp + oc.charges.dp).toFixed(2)}</span></div>
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {orderCharges.length === 0 && (
                      <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                        <p className="font-display tabular-nums text-sm text-mist">No filled orders today — charges appear once orders execute.</p>
                        <p className="font-display tabular-nums text-[10px] text-mist mt-1">Refresh Live Dhan data first if orders are missing.</p>
                      </div>
                    )}

                    {/* Rate reference card */}
                    <div className="bg-graphite border border-slate rounded-2xl p-4">
                      <SectionHdr>Dhan charge rates reference (NSE equity)</SectionHdr>
                      <div className="space-y-1.5 font-display tabular-nums text-[11px]">
                        {[
                          { type: "Intraday (MIS)", brok: "₹20 or 0.03%/leg", stt: "0.025% on sell", stamp: "0.003% buy", dp: "—" },
                          { type: "Delivery (CNC)", brok: "FREE", stt: "0.1% on buy + sell", stamp: "0.015% buy", dp: `₹${DP_CHARGE_INCL_GST.toFixed(2)}/scrip sold (₹12.50 + GST)` },
                        ].map(r => (
                          <div key={r.type} className="bg-ink border border-slate rounded-xl p-3">
                            <p className="font-bold text-paper mb-1.5">{r.type}</p>
                            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[10px] text-mist">
                              <span>Brokerage: <span className="text-paper">{r.brok}</span></span>
                              <span>STT: <span className="text-paper">{r.stt}</span></span>
                              <span>Exchange: <span className="text-paper">0.00297% + IPFT 0.0001% + SEBI 0.0001%</span></span>
                              <span>GST: <span className="text-paper">18% on brokerage+exch+SEBI</span></span>
                              <span>Stamp: <span className="text-paper">{r.stamp}</span></span>
                              <span>DP: <span className="text-paper">{r.dp}</span></span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </>
                )}
              </div>
            );
}
