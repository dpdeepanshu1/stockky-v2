// round 555 (P6): the stock popup's order-flow section. An estimate, hidden when the tape is not reliable, every number stamped with a time, a missing number is a dash.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { istClock, popupFlowView, DASH } from "./popupFlowView.ts";

const ok = (m) => console.log("ok - " + m);
const NOW = Date.UTC(2026, 10, 16, 9, 2, 0);           // 14:32 IST
const doc = (o = {}) => ({
  symbol: "SBIN", available: true, reliable: true, reason: null, coverage_pct: 48.2, tape_minutes: 31.5, price: 812.4,
  profile: { poc: 808, vah: 811, val: 804, location: "above_value", hvn_above: 815.5, hvn_below: 808 },
  signals: { buy_pct_10m: 41, delta_agrees: false }, checklist: { long: { against: 4 } }, ...o,
});

assert.equal(istClock(NOW), "14:32");
for (const bad of [0, -1, NaN, null, undefined, "x", Infinity]) assert.equal(istClock(bad), "");
ok("clock in IST");

const v = popupFlowView(doc(), NOW);
assert.equal(v.kind, "ok");
assert.equal(v.title, "Order flow (estimate)");
assert.equal(v.asOf, "14:32");
const by = Object.fromEntries(v.lines.map((l) => [l.key, l.value]));
assert.deepEqual(by, { value: "804.00 to 811.00", poc: "808.00", loc: "above value", hvn_up: "815.50", hvn_dn: "808.00", buy: "41%", delta: "disagrees with the 20 min move", cov: "48%" });
assert.match(v.note, /^ESTIMATE from quote snapshots, not a trade feed\. Read 14:32 IST, 31\.5 min of tape\./);
assert.ok(!/long|short|against|buy signal|sell signal/i.test(v.lines.map((l) => l.value).join(" ")), "no verdict, no six-check judgement in the popup");
ok("a reliable tape: six facts, the estimate label and the read time");

// nothing at all when it cannot be trusted
for (const d of [null, undefined, "x", {}, doc({ available: false }), doc({ reliable: false, reason: "thin_coverage" }), doc({ reliable: undefined }), doc({ reliable: "true" })]) {
  const h = popupFlowView(d, NOW);
  assert.deepEqual([h.kind, h.lines.length, h.title, h.note], ["hidden", 0, "", ""], "unavailable / unreliable tape shows nothing");
}
ok("unavailable or unreliable tape shows nothing");

// no number without a time
for (const t of [undefined, 0, NaN]) assert.equal(popupFlowView(doc(), t).kind, "hidden");
ok("no read time, no section");

// missing numbers are dashes, never 0
const thin = popupFlowView(doc({ coverage_pct: null, tape_minutes: undefined, profile: { poc: 0, val: 804 }, signals: { buy_pct_10m: null, delta_agrees: null } }), NOW);
const t = Object.fromEntries(thin.lines.map((l) => [l.key, l.value]));
assert.deepEqual(t, { value: DASH, poc: DASH, loc: DASH, hvn_up: DASH, hvn_dn: DASH, buy: DASH, delta: "no read", cov: DASH });
assert.doesNotMatch(thin.note, /min of tape/);
assert.equal(popupFlowView(doc({ signals: { buy_pct_10m: 0, delta_agrees: true } }), NOW).lines.find((l) => l.key === "buy").value, "0%", "a real 0 % buy share is shown as 0%");
ok("missing is a dash, a real zero is kept");

// wired: read only, inside the popup, admin route, hidden-or-login behaviour
const comp = readFileSync(new URL("./components/board/PopupFlow.tsx", import.meta.url), "utf8").replace(/^\s*\/\/.*$/gm, "");
assert.ok(!/method:\s*"(POST|PUT|DELETE|PATCH)"|\/control\/|setSwitch|placeOrder|buy\(|sell\(/i.test(comp), "the section only reads");
assert.match(comp, /positionStocksApi\.flowSymbol\(symbol\)/);
assert.match(comp, /if \(v\.kind !== "ok"\) return null;/);
assert.match(comp, /catch \{ if \(alive\) \{ setDoc\(null\); setAt\(undefined\)/, "a failed read clears the section, never shows the old read as current");
const popup = readFileSync(new URL("./components/board/StockPopup.tsx", import.meta.url), "utf8");
assert.match(popup, /<PopupFlow symbol=\{row\.symbol\} \/>/);
ok("wired into the popup, read-only, clears on a failed read");
