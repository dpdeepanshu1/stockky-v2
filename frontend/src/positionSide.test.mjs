// Short selling step 11: direction helpers for Position Stocks trades, the live-mode view of the Short selling panel, and the guards that keep long rows unchanged.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { sideOf, isShort, legLabels, pnlPctFromPrices, sideBadge, shortModeView } from "./positionSide.ts";
import { shortShadowView } from "./shortShadowView.ts";

let n = 0;
const t = (name, fn) => { fn(); n++; console.log("ok", name); };

t("legacy and blank sides are LONG, only SHORT is short (any case)", () => {
  for (const p of [undefined, null, {}, { side: null }, { side: "" }, { side: "LONG" }, { side: "long" }, { side: "weird" }]) assert.equal(sideOf(p), "LONG");
  for (const p of [{ side: "SHORT" }, { side: " short " }]) assert.equal(sideOf(p), "SHORT");
  assert.equal(isShort({ side: "SHORT" }), true); assert.equal(isShort({}), false);
});
t("a long keeps Buy / Sell and no badge; a short is Sell / Cover with a SHORT badge", () => {
  assert.deepEqual(legLabels({}), { entry: "Buy", exit: "Sell" }); assert.equal(sideBadge({}), "");
  assert.deepEqual(legLabels({ side: "SHORT" }), { entry: "Sell", exit: "Cover" }); assert.equal(sideBadge({ side: "SHORT" }), "SHORT");
});
t("closed P&L % from prices: long = exit - entry, short = entry - exit", () => {
  assert.equal(pnlPctFromPrices({ entry_price: 100, exit_price: 102 }), 2);
  assert.equal(pnlPctFromPrices({ side: "SHORT", entry_price: 100, exit_price: 98 }), 2);
  assert.equal(pnlPctFromPrices({ side: "SHORT", entry_price: 100, exit_price: 101 }), -1);
  for (const p of [{}, { entry_price: 100 }, { entry_price: 0, exit_price: 5 }, { entry_price: NaN, exit_price: 5 }]) assert.equal(pnlPctFromPrices(p), null);
});
t("mode view: live = real orders (bad tone), shadow = no order, anything else = off", () => {
  assert.equal(shortModeView("live").orders, true); assert.equal(shortModeView(" LIVE ").tone, "bad"); assert.match(shortModeView("live").title, /LIVE/);
  assert.equal(shortModeView("shadow").orders, false); assert.match(shortModeView("shadow").title, /shadow only/);
  for (const m of ["off", "", null, undefined, "junk"]) { assert.equal(shortModeView(m).mode, "off"); assert.equal(shortModeView(m).orders, false); }
});
t("the short view calls live mode live and off mode off", () => {
  const base = { gate_counters: {} };
  assert.equal(shortShadowView({ ...base, mode: "live" }).kind, "live"); assert.match(shortShadowView({ ...base, mode: "live" }).headline, /LIVE/);
  assert.equal(shortShadowView({ ...base, mode: "off" }).kind, "off");
});
t("the Positions tab uses the helpers (badge, leg labels, short-aware P&L %) and has no long-only P&L formula left", () => {
  const src = readFileSync(new URL("./components/PositionStocksTab.tsx", import.meta.url), "utf8");
  assert.ok(src.includes('from "../positionSide"') && src.includes("sideBadge(p)") && src.includes("legLabels(p)") && src.includes("pnlPctFromPrices(p)"));
  assert.ok(!src.includes("((p.exit_price - p.entry_price) / p.entry_price) * 100"));
});
t("the Short selling panel can still only switch SHORT_MODE OFF (never shadow or live) and names the cap switch", () => {
  const src = readFileSync(new URL("./components/calibration/ShortSellingPanel.tsx", import.meta.url), "utf8");
  const sets = [...src.matchAll(/controlApi\.set\([^)]*\)/g)].map((m) => m[0]);
  assert.equal(sets.length, 1); assert.match(sets[0], /"SHORT_MODE", "off"/);
  assert.match(src, /SHORT_LIVE_QTY_CAP/);
});
console.log("positionSide.test.mjs: " + n + " tests passed");
