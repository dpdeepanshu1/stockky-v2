// Run: node --experimental-strip-types sellOrders.test.mjs  (Node >= 22.6). E243: the SELL orders card helpers on the Positions tab.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { sellOrdersView, brokerStateView } from "./sellOrdersView.ts";

let n = 0;
const t = (name, fn) => { fn(); n++; console.log("ok", name); };
const summary = (o = {}) => ({ probe_runs: 0, shorts_open: 0, sell_orders_at_dhan: 0, sell_orders_waiting: 0, ...o });
const doc = (o = {}) => ({ probes: [], shorts: [], broker_rows: [], errors: [], summary: summary(), ...o });

t("no data -> unavailable; an empty doc says nothing was sent", () => {
  assert.equal(sellOrdersView(null).kind, "unavailable");
  const v = sellOrdersView(doc());
  assert.equal(v.kind, "empty"); assert.match(v.headline, /No SELL order/);
});
t("a resting SELL at Dhan reads as RESTING, never as nothing", () => {
  const b = brokerStateView({ order_id: "1", status: "PENDING", state: "WAITING", order_type: "LIMIT", price: 300.4, quantity: 1, filled_qty: 0 });
  assert.equal(b.tone, "warn"); assert.match(b.text, /RESTING/); assert.match(b.text, /300\.40/);
  assert.match(brokerStateView(null).text, /not in Dhan's book/);
  assert.equal(brokerStateView({ order_id: "1", status: "REJECTED", state: "REJECTED" }).tone, "bad");
  assert.match(brokerStateView({ order_id: "1", status: "TRADED", state: "FILLED", average_price: 299.9 }).text, /filled at 299\.90/);
});
t("the failed NTPC probe shows as a SELL PROBE line: FAIL, never filled, entry type and price", () => {
  const v = sellOrdersView(doc({
    summary: summary({ probe_runs: 1 }),
    probes: [{ kind: "PROBE", symbol: "NTPC", at: "2026-10-28T04:44:00+00:00", order_id: "34326102815775", verdict: "FAIL", reason: "the SELL entry did not show as filled",
      entry_order_type: "LIMIT", entry_price_sent: 299.5, ref_price: 300.4, qty: 1,
      broker: { order_id: "34326102815775", status: "CANCELLED", state: "CANCELLED", filled_qty: 0 } }],
  }));
  assert.equal(v.kind, "active"); assert.equal(v.lines.length, 1);
  const l = v.lines[0];
  assert.equal(l.tag, "SELL PROBE"); assert.equal(l.symbol, "NTPC"); assert.equal(l.tone, "bad"); assert.equal(l.state, "FAIL");
  assert.match(l.detail, /LIMIT @ 299\.50/); assert.match(l.detail, /never filled/); assert.match(l.detail, /did not show as filled/);
});
t("live shorts and probes are merged newest first; waiting orders are counted in the headline", () => {
  const v = sellOrdersView(doc({
    summary: summary({ probe_runs: 1, shorts_open: 1, sell_orders_waiting: 1 }),
    probes: [{ kind: "PROBE", symbol: "ITC", at: "2026-10-28T04:00:00+00:00", verdict: "PASS", entry_fill: 250.1, cover_fill: 250.2 }],
    shorts: [{ kind: "SHORT", id: 7, symbol: "SBIN", status: "OPEN", quantity: 1, entry_price: 800, stop_price: 808, target_price: 780, opened_at: "2026-10-28T05:00:00+00:00",
      broker: { order_id: "9", status: "PENDING", state: "WAITING", order_type: "LIMIT", price: 799 } }],
  }));
  assert.deepEqual(v.lines.map((x) => x.symbol), ["SBIN", "ITC"]);
  assert.equal(v.lines[0].tag, "SHORT"); assert.equal(v.lines[0].tone, "warn"); assert.equal(v.lines[1].tone, "good");
  assert.match(v.headline, /1 short open/); assert.match(v.headline, /1 SELL order RESTING unfilled at Dhan/);
  assert.equal(v.waiting, 1);
});
t("the card is wired into the Positions sub-tab and reads /short/orders", () => {
  const tab = readFileSync(new URL("./components/PositionStocksTab.tsx", import.meta.url), "utf8");
  assert.match(tab, /<SellOrdersCard active=\{subTab === "positions"\}/);
  assert.match(readFileSync(new URL("./positionStocksApi.ts", import.meta.url), "utf8"), /shortOrders: \(\) => psRequest<any>\("\/short\/orders"\)/);
});
console.log(`${n} passed`);
