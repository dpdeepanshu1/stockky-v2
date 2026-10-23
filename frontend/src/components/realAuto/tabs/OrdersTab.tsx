import type { OrderRow } from "../../../realTradeApi";
import { fmtDate, fmtTime } from "../format";
import { SectionHdr } from "../parts";
import type { Mode } from "../types";

// E58 - E31 step 2: the "orders" tab, moved out of RealAutoTrade.tsx (JSX unchanged).
// `isSameLocalDay` moved here with it: this tab was its only user.
const isSameLocalDay = (iso: string, ref: Date): boolean => {
  const d = new Date(iso);
  return d.getFullYear() === ref.getFullYear()
    && d.getMonth() === ref.getMonth()
    && d.getDate() === ref.getDate();
};

export interface OrdersTabProps {
  mode: Mode;
  orders: OrderRow[];
  orderRange: "today" | "all";
  setOrderRange: (r: "today" | "all") => void;
  loadPositionsAndOrders: (m: Mode) => Promise<void>;
  doCancelOrder: (o: OrderRow) => Promise<void>;
  actionBusy: string | null;
}

export function OrdersTab({ mode, orders, orderRange, setOrderRange, loadPositionsAndOrders, doCancelOrder, actionBusy }: OrdersTabProps) {
            const now = new Date();
            const todayOrders = orders.filter(o => isSameLocalDay(o.created_at, now));
            const visibleOrders = orderRange === "today" ? todayOrders : orders;
            return (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <SectionHdr>Recent orders — {mode}</SectionHdr>
                <button onClick={() => void loadPositionsAndOrders(mode)}
                  className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist">
                  ↻
                </button>
              </div>
              <div className="flex gap-1.5">
                <button onClick={() => setOrderRange("today")}
                  className={`font-display tabular-nums text-[10px] px-3 py-1.5 rounded-xl border ${orderRange === "today"
                    ? "bg-signal-buy/10 border-signal-buy/40 text-signal-buy"
                    : "bg-graphite border-slate text-mist"}`}>
                  Today ({todayOrders.length})
                </button>
                <button onClick={() => setOrderRange("all")}
                  className={`font-display tabular-nums text-[10px] px-3 py-1.5 rounded-xl border ${orderRange === "all"
                    ? "bg-signal-buy/10 border-signal-buy/40 text-signal-buy"
                    : "bg-graphite border-slate text-mist"}`}>
                  Last 2 days ({orders.length})
                </button>
              </div>
              {visibleOrders.length === 0 ? (
                <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                  <p className="font-display tabular-nums text-sm text-mist">
                    {orderRange === "today" ? "No orders today." : "No orders yet."}
                  </p>
                </div>
              ) : (
                <div className="bg-graphite border border-slate rounded-2xl overflow-hidden">
                  <table className="w-full font-display tabular-nums text-[11px]">
                    <thead className="border-b border-slate">
                      <tr className="text-mist">
                        <th className="text-left p-3">Symbol</th>
                        <th className="text-left p-3">Side</th>
                        <th className="text-right p-3">Qty</th>
                        <th className="text-right p-3">Limit</th>
                        <th className="text-right p-3">Current</th>
                        <th className="text-center p-3">Status</th>
                        <th className="text-right p-3">{orderRange === "today" ? "Time" : "Date · Time"}</th>
                        <th className="p-3" />
                      </tr>
                    </thead>
                    <tbody>
                      {visibleOrders.map(o => {
                        const sc = o.status === "FILLED" ? "text-signal-buy"
                          : o.status === "CANCELLED" || o.status === "REJECTED" ? "text-signal-sell"
                          : o.status === "PLACED" ? "text-signal-hold"
                          : "text-mist";
                        const waiting = o.status === "PENDING" || o.status === "PLACED";
                        return (
                          <tr key={o.id} className="border-b border-slate hover:bg-ink">
                            <td className="p-3 font-bold text-paper">
                              {o.symbol}
                              {o.execution_source === "MANUAL" && (
                                <span className="ml-1.5 font-display tabular-nums text-[9px] text-signal-prepare align-middle">MANUAL</span>
                              )}
                            </td>
                            <td className={`p-3 font-bold ${o.side === "BUY" ? "text-signal-buy" : "text-signal-sell"}`}>{o.side}</td>
                            <td className="p-3 text-right text-mist">{o.qty}</td>
                            <td className="p-3 text-right text-mist">{o.limit_price ? `₹${o.limit_price}` : "MKT"}</td>
                            <td className="p-3 text-right">
                              {waiting && o.current_price != null ? (
                                <span className={o.limit_distance_pct != null && o.limit_distance_pct <= 0 ? "text-signal-buy" : "text-mist"}>
                                  ₹{o.current_price.toFixed(2)}
                                  {o.limit_distance_pct != null && (
                                    <span className="text-[9px] text-mist ml-1">
                                      ({o.limit_distance_pct > 0 ? "+" : ""}{o.limit_distance_pct}%)
                                    </span>
                                  )}
                                </span>
                              ) : <span className="text-mist">—</span>}
                            </td>
                            <td className={`p-3 text-center ${sc}`}>{o.status}</td>
                            <td className="p-3 text-right text-mist">
                              {orderRange === "today" ? fmtTime(o.created_at) : `${fmtDate(o.created_at)} ${fmtTime(o.created_at)}`}
                            </td>
                            <td className="p-3 text-right">
                              {o.status === "PLACED" && (
                                <button onClick={() => void doCancelOrder(o)} disabled={actionBusy === `cancel:${o.id}`}
                                  className="text-signal-sell hover:text-signal-sell disabled:opacity-40">
                                  {actionBusy === `cancel:${o.id}` ? "…" : "✕"}
                                </button>
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
            );
}
