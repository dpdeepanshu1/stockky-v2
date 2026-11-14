// E538 (redesign round 5b): the "charges" sub-tab of Position Stocks, moved out of PositionStocksTab.tsx (JSX unchanged).
// All state and handlers come from usePositionStocks() through `s`, so the ten sub-tabs still share one set of state.
import { type ScalpChargesPeriod } from "../../positionStocksApi";
import { legCharges as calcLegCharges, type ChargeBreakdown } from "../../chargesRates";
import { fmtChargeDay, fmtInr } from "./parts";
import type { PsState } from "./usePositionStocks";

export function PsChargesTab({ s }: { s: PsState }) {
  const { status, ledger, dhanLive, cumCharges, cumChargesError, chargeTrades, loadDhanLive } = s;
  return (() => {
        // AUDIT ADD (this session), ported from Real Automatic Trade's
        // Charges tab — same Dhan NSE-equity rate card, but this service's
        // orders are ALWAYS intraday (config.SCALP_PRODUCT_TYPE = "INTRADAY",
        // FIXED session38 — was "INTRA", an invalid Dhan productType enum value,
        // never "CNC" — see main.py's /status pipeline_config), so there's
        // no delivery/intraday branch to pick here, unlike real-trade-
        // service which trades both. Built from dhanLive (Dhan's own
        // super-order-leg book, broker-side truth) — an ENTRY_LEG that
        // TRADED is the BUY, whichever of TARGET_LEG/STOP_LOSS_LEG actually
        // TRADED is the SELL.
        // group 262: rates live in ../chargesRates.ts (one card shared with Real Auto Trade and the backend ledger):
        // intraday STT is 0.025% on the SELL leg only, exchange 0.00297% + 0.0001% IPFT, GST also on SEBI.

        interface LegCharge { symbol: string; leg: string; side: string; qty: number; price: number; charges: ChargeBreakdown; }
        const filledLegs = (dhanLive?.orders ?? []).filter(o => {
          const qty = Number(o.quantity || 0);
          const price = Number(o.price || 0);
          const st = String(o.orderStatus ?? "").toUpperCase();
          return qty > 0 && price > 0 && (st === "TRADED" || st === "FILLED" || st === "COMPLETE");
        });
        const legCharges: LegCharge[] = filledLegs.map(o => {
          const side = String(o.transactionType ?? "").toUpperCase();
          const qty = Number(o.quantity || 0);
          const price = Number(o.price || 0);
          return {
            symbol: String(o.tradingSymbol ?? "—"), leg: String(o.legName ?? "—"), side, qty, price,
            charges: calcLegCharges(qty * price, side, false),      // every order here is INTRADAY (no DP)
          };
        });
        const totalBrokerage = legCharges.reduce((s, o) => s + o.charges.brokerage, 0);
        const totalSTT = legCharges.reduce((s, o) => s + o.charges.stt, 0);
        const totalExchange = legCharges.reduce((s, o) => s + o.charges.exchange, 0);
        const totalSEBI = legCharges.reduce((s, o) => s + o.charges.sebi, 0);
        const totalGST = legCharges.reduce((s, o) => s + o.charges.gst, 0);
        const totalStamp = legCharges.reduce((s, o) => s + o.charges.stamp, 0);
        const grandTotal = legCharges.reduce((s, o) => s + o.charges.total, 0);

        return (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <p className="dash-section-title">Dhan charges — Position Stocks (intraday only)</p>
              <button onClick={() => void loadDhanLive()}
                className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist">
                ↻ Refresh
              </button>
            </div>

            <div className="bg-graphite border border-slate rounded-2xl p-4">
              <p className="font-display tabular-nums text-[10px] text-mist uppercase tracking-widest mb-2">Live Dhan order book, right now (TRADED legs with a price only — a market/EOD exit with no price is missing here; the Charges today line below is the full-day figure)</p>
              <div className="grid grid-cols-2 gap-2 mb-3">
                <div className="bg-ink border border-slate rounded-xl p-3">
                  <p className="text-[9px] text-mist uppercase tracking-widest">Total charges</p>
                  <p className="font-display tabular-nums font-bold text-sm text-signal-sell">-{fmtInr(grandTotal, 2)}</p>
                  <p className="text-[9px] text-mist mt-0.5">{legCharges.length} filled legs</p>
                </div>
                <div className="bg-ink border border-slate rounded-xl p-3">
                  <p className="text-[9px] text-mist uppercase tracking-widest">Brokerage + GST</p>
                  <p className="font-display tabular-nums font-bold text-sm text-signal-sell">-{fmtInr(totalBrokerage + totalGST, 2)}</p>
                  <p className="text-[9px] text-mist mt-0.5">₹20 cap per leg, 18% GST</p>
                </div>
              </div>
              {ledger && (() => {
                // group 260: the order-book snapshot above only sees legs Dhan still lists as TRADED with a price
                // (a market/EOD exit often has none), so subtracting ITS total understated today's cost (BUY leg only).
                // Today's charges come from the ledger (buy + sell of every closed trade) when it has loaded.
                const chargesToday = cumCharges?.today ? cumCharges.today.all_charges : grandTotal;
                const net = ledger.realized_pnl_today - chargesToday;
                return (
                <div className="bg-ink border border-slate rounded-xl p-3 mb-3">
                  <div className="flex items-center justify-between font-display tabular-nums text-[11px] text-mist mb-1">
                    <span>Realized P&L today (price only)</span>
                    <span className={ledger.realized_pnl_today >= 0 ? "text-signal-buy" : "text-signal-sell"}>
                      {ledger.realized_pnl_today >= 0 ? "+" : ""}{fmtInr(ledger.realized_pnl_today, 2)}
                    </span>
                  </div>
                  <div className="flex items-center justify-between font-display tabular-nums text-[11px] text-mist mb-1">
                    <span>− Charges today (all closed trades, buy + sell)</span>
                    <span className="text-signal-sell">-{fmtInr(chargesToday, 2)}</span>
                  </div>
                  <div className="flex items-center justify-between font-display tabular-nums text-xs font-bold border-t border-slate pt-1.5 mt-1">
                    <span className="text-paper">Net (approx, after charges)</span>
                    <span className={net >= 0 ? "text-signal-buy" : "text-signal-sell"}>
                      {net >= 0 ? "+" : ""}{fmtInr(net, 2)}
                    </span>
                  </div>
                </div>
                );
              })()}
              <div className="border-t border-slate pt-3">
                <p className="font-display tabular-nums text-[10px] text-mist mb-2 uppercase tracking-widest">Full breakdown</p>
                <div className="space-y-1.5 font-display tabular-nums text-[11px]">
                  {[
                    { label: "Brokerage", val: totalBrokerage, note: "₹20 or 0.03%/leg, whichever lower" },
                    { label: "STT", val: totalSTT, note: "0.025% intraday, sell leg only" },
                    { label: "Exchange Txn", val: totalExchange, note: "0.00297% NSE + 0.0001% IPFT" },
                    { label: "SEBI charges", val: totalSEBI, note: "0.0001% on turnover" },
                    { label: "GST", val: totalGST, note: "18% on brokerage + exchange + SEBI" },
                    { label: "Stamp duty", val: totalStamp, note: "0.003% intraday (buy side)" },
                  ].map(row => (
                    <div key={row.label} className="flex items-center justify-between">
                      <div><span className="text-paper">{row.label}</span><span className="text-mist text-[9px] ml-2">{row.note}</span></div>
                      <span className={row.val > 0 ? "text-signal-sell" : "text-mist"}>{row.val > 0 ? `-${fmtInr(row.val, 2)}` : "—"}</span>
                    </div>
                  ))}
                  <div className="flex items-center justify-between border-t border-slate pt-2 mt-1">
                    <span className="font-bold text-paper">Total charges</span>
                    <span className="font-bold text-signal-sell">-{fmtInr(grandTotal, 2)}</span>
                  </div>
                </div>
              </div>
            </div>

            {/* group 259: charges since the first booked trade — date range, today, total (scalp_charges_ledger, survives 3-day retention) */}
            <div className="bg-graphite border border-slate rounded-2xl p-4">
              <p className="font-display tabular-nums text-[10px] text-mist uppercase tracking-widest mb-2">
                Charges since start{cumCharges?.since ? ` (from ${fmtChargeDay(cumCharges.since)})` : ""}
              </p>
              {cumChargesError && <p className="text-[10px] text-signal-sell mb-2">{cumChargesError}</p>}
              {cumCharges ? (
                <>
                  <div className="space-y-1.5 font-display tabular-nums text-[11px]">
                    <div className="flex items-center justify-between text-[9px] text-mist uppercase tracking-widest">
                      <span>Period</span><span>Brokerage · All charges · Net P&amp;L</span>
                    </div>
                    {([
                      cumCharges.history && { label: `${fmtChargeDay(cumCharges.history.from)} – ${fmtChargeDay(cumCharges.history.to)}`, p: cumCharges.history, bold: false },
                      { label: "Today", p: cumCharges.today, bold: false },
                      { label: "Total", p: cumCharges.total, bold: true },
                    ].filter(Boolean) as { label: string; p: ScalpChargesPeriod; bold: boolean }[]).map(r => (
                      <div key={r.label} className={`flex items-center justify-between ${r.bold ? "border-t border-slate pt-2 mt-1 font-bold" : ""}`}>
                        <span className="text-paper">{r.label} <span className="text-mist text-[9px] font-normal">{r.p.trades} trades</span></span>
                        <span className="text-mist">
                          <span className="text-signal-sell">-{fmtInr(r.p.brokerage, 2)}</span>
                          {" · "}<span className="text-signal-sell">-{fmtInr(r.p.all_charges, 2)}</span>
                          {" · "}<span className={r.p.net_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}>{r.p.net_pnl >= 0 ? "+" : "-"}{fmtInr(Math.abs(r.p.net_pnl), 2)}</span>
                        </span>
                      </div>
                    ))}
                  </div>
                  {cumCharges.total.components && (
                    <p className="font-display tabular-nums text-[10px] text-mist mt-2">
                      Total split: STT {fmtInr(cumCharges.total.components.stt, 2)} · GST {fmtInr(cumCharges.total.components.gst, 2)} · Exch+SEBI {fmtInr(cumCharges.total.components.exchange + cumCharges.total.components.sebi, 2)} · Stamp {fmtInr(cumCharges.total.components.stamp, 2)} · Brokerage {fmtInr(cumCharges.total.components.brokerage, 2)}
                    </p>
                  )}
                  {cumCharges.pnl && (
                    <div className="mt-3 pt-3 border-t border-slate space-y-1.5 font-display tabular-nums text-[11px]">
                      <div className="flex items-center justify-between text-mist">
                        <span>Realized P&amp;L all-time (price only)</span>
                        <span className={cumCharges.pnl.realized_gross_total >= 0 ? "text-signal-buy" : "text-signal-sell"}>
                          {cumCharges.pnl.realized_gross_total >= 0 ? "+" : ""}{fmtInr(cumCharges.pnl.realized_gross_total, 2)}
                        </span>
                      </div>
                      <div className="flex items-center justify-between text-mist">
                        <span>− Charges since start</span>
                        <span className="text-signal-sell">-{fmtInr(cumCharges.pnl.charges_total, 2)}</span>
                      </div>
                      <div className="flex items-center justify-between font-bold border-t border-slate pt-1.5">
                        <span className="text-paper">Net realized P&amp;L all-time (after charges)</span>
                        <span className={cumCharges.pnl.net_realized_total >= 0 ? "text-signal-buy" : "text-signal-sell"}>
                          {cumCharges.pnl.net_realized_total >= 0 ? "+" : ""}{fmtInr(cumCharges.pnl.net_realized_total, 2)}
                        </span>
                      </div>
                    </div>
                  )}
                  {cumCharges.recent_days.length > 0 && (
                    <div className="mt-3 pt-3 border-t border-slate">
                      <p className="text-[9px] text-mist uppercase tracking-widest mb-1.5">Recent days · Gross · Charges · Net</p>
                      <div className="space-y-1 font-display tabular-nums text-[10px]">
                        {cumCharges.recent_days.map(d => (
                          <div key={d.day} className="flex items-center justify-between">
                            <span className="text-paper">{fmtChargeDay(d.day)} <span className="text-mist text-[9px]">{d.trades} trades</span></span>
                            <span className="text-mist">
                              {d.gross_pnl != null && <span className={d.gross_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}>{d.gross_pnl >= 0 ? "+" : "-"}{fmtInr(Math.abs(d.gross_pnl), 2)}</span>}
                              {" · "}<span className="text-signal-sell">-{fmtInr(d.total_charges, 2)}</span>
                              {d.net_pnl != null && <>{" · "}<span className={d.net_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}>{d.net_pnl >= 0 ? "+" : "-"}{fmtInr(Math.abs(d.net_pnl), 2)}</span></>}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  <p className="font-display tabular-nums text-[9px] text-mist mt-2">{cumCharges.note}</p>
                </>
              ) : !cumChargesError ? (
                <p className="font-display tabular-nums text-[10px] text-mist">Loading…</p>
              ) : null}
            </div>

            {/* group 263: per-trade charges from the STORED ledger rows (full split, gross -> net), same columns as Real Auto Trade's "Per trade charges" */}
            {chargeTrades && chargeTrades.trades.length > 0 && (
              <div className="bg-graphite border border-slate rounded-2xl p-4">
                <p className="dash-section-title mb-3">Per trade charges ({fmtChargeDay(chargeTrades.day)})</p>
                <div className="space-y-2">
                  {chargeTrades.trades.map(t => (
                    <div key={t.position_id} className="bg-ink border border-slate rounded-xl p-3">
                      <div className="flex items-center justify-between mb-1">
                        <div className="flex items-center gap-2">
                          <span className="font-display tabular-nums text-sm font-bold text-paper">{t.symbol}</span>
                          <span className="font-display tabular-nums text-[9px] text-mist">MIS · buy + sell</span>
                        </div>
                        <span className="font-display tabular-nums text-xs text-signal-sell font-bold">-{fmtInr(t.total_charges, 2)}</span>
                      </div>
                      <div className="flex flex-wrap gap-x-3 font-display tabular-nums text-[10px] text-mist">
                        <span>Qty <span className="text-paper">{t.qty}</span></span>
                        <span>Buy <span className="text-paper">{t.buy_price != null ? `₹${t.buy_price.toFixed(2)}` : "—"}</span></span>
                        <span>Sell <span className="text-paper">{t.sell_price != null ? `₹${t.sell_price.toFixed(2)}` : "—"}</span></span>
                        <span>Value <span className="text-paper">{fmtInr(t.buy_value, 0)}</span></span>
                      </div>
                      <div className="flex flex-wrap gap-x-3 font-display tabular-nums text-[10px] text-mist mt-0.5">
                        <span>Brokerage <span className="text-paper">₹{t.brokerage.toFixed(2)}</span></span>
                        <span>STT <span className="text-paper">₹{t.stt.toFixed(2)}</span></span>
                        <span>Exch+SEBI <span className="text-paper">₹{(t.exchange + t.sebi).toFixed(2)}</span></span>
                        <span>GST+Stamp <span className="text-paper">₹{(t.gst + t.stamp).toFixed(2)}</span></span>
                      </div>
                      {t.gross_pnl != null && t.net_pnl != null && (
                        <div className="flex flex-wrap gap-x-3 font-display tabular-nums text-[10px] text-mist mt-0.5">
                          <span>Gross <span className={t.gross_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}>{t.gross_pnl >= 0 ? "+" : "-"}{fmtInr(Math.abs(t.gross_pnl), 2)}</span></span>
                          <span>Net <span className={t.net_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}>{t.net_pnl >= 0 ? "+" : "-"}{fmtInr(Math.abs(t.net_pnl), 2)}</span></span>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {legCharges.length > 0 ? (
              <div className="bg-graphite border border-slate rounded-2xl p-4">
                <p className="dash-section-title mb-3">Per-leg charges (live Dhan order book)</p>
                <div className="space-y-2">
                  {legCharges.map((oc, i) => (
                    <div key={i} className="bg-ink border border-slate rounded-xl p-3">
                      <div className="flex items-center justify-between mb-1">
                        <div className="flex items-center gap-2">
                          <span className={`font-display tabular-nums text-xs font-bold ${oc.side === "BUY" ? "text-signal-buy" : "text-signal-sell"}`}>{oc.side}</span>
                          <span className="font-display tabular-nums text-sm font-bold text-paper">{oc.symbol}</span>
                          <span className="font-display tabular-nums text-[9px] text-mist">{oc.leg} · MIS</span>
                        </div>
                        <span className="font-display tabular-nums text-xs text-signal-sell font-bold">-{fmtInr(oc.charges.total, 2)}</span>
                      </div>
                      <div className="flex gap-3 font-display tabular-nums text-[10px] text-mist">
                        <span>Qty <span className="text-paper">{oc.qty}</span></span>
                        <span>Price <span className="text-paper">₹{oc.price.toFixed(2)}</span></span>
                        <span>Value <span className="text-paper">{fmtInr(oc.qty * oc.price, 0)}</span></span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                <p className="font-display tabular-nums text-sm text-mist">No filled legs in the current Dhan order book snapshot.</p>
                <p className="font-display tabular-nums text-[10px] text-mist mt-1">Refresh Dhan Live Orders first, or check the Trade History tab for older closed trades (charges aren't retroactively computed there).</p>
              </div>
            )}
          </div>
        );
      })();
}
