// round 555 (P6): the stock popup's order-flow section. An estimate, hidden when the tape is not reliable, every number stamped with a time, a missing number is a dash.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { istClock, istStamp, liquidityView, popupFlowView, rupeesShort, DASH } from "./popupFlowView.ts";

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

// ── round 559 (P8): liquidity badge ──
const NOWS = NOW / 1000;                                    // 14:32 IST, in seconds
const liq = (o = {}) => ({ session: "2026-11-16", turnover: { value: 124000000, pct_rank: 72.5, of: 40, as_of: NOWS - 60 }, spread: { last_pct: 0.18, last_at: NOWS - 120, median_pct: 0.15, max_pct: 0.25, minutes: 12 }, ...o });

assert.equal(rupeesShort(124000000), "\u20B912.4 Cr");
assert.equal(rupeesShort(1240000000), "\u20B9124 Cr");
assert.equal(rupeesShort(860000), "\u20B98.6 L");
assert.equal(rupeesShort(54321), "\u20B954,321");
for (const bad of [0, -5, NaN, null, undefined, "x", Infinity]) assert.equal(rupeesShort(bad), DASH);
ok("rupees in Cr / L, a dash for a missing value");

assert.equal(istStamp(NOWS, NOW), "14:32 IST");
assert.equal(istStamp(NOWS - 3 * 86400, NOW), "13 Nov 14:32 IST", "another IST day says the date");
for (const bad of [0, -1, NaN, null, undefined, "x"]) assert.equal(istStamp(bad, NOW), "");
ok("a stamp is a time, with the date when it is not today");

const lv = liquidityView(liq(), NOW);
assert.deepEqual([lv.kind, lv.turnover, lv.spread], ["ok", "\u20B912.4 Cr", "0.18%"]);
assert.equal(lv.turnoverSub, "higher than 73% of 40 tracked stocks, as of 14:31 IST");
assert.equal(lv.spreadSub, "14:30 IST, session median 0.15%, widest minute 0.25%");
assert.doesNotMatch(lv.note + lv.turnoverSub + lv.spreadSub, /liquid|illiquid|buy|sell|safe|avoid/i, "facts only, no verdict");
ok("both facts, each with its time");

const noSpread = liquidityView(liq({ spread: null }), NOW);
assert.deepEqual([noSpread.kind, noSpread.turnover, noSpread.spread, noSpread.spreadSub], ["ok", "\u20B912.4 Cr", DASH, "no recorded reading this session"]);
const noRank = liquidityView(liq({ turnover: { value: 9000000, pct_rank: null, of: 12, as_of: NOWS } }), NOW);
assert.match(noRank.turnoverSub, /^rank not available, as of 14:32 IST$/);
assert.match(liquidityView(liq({ turnover: { value: 9000000, pct_rank: 0, of: 40, as_of: NOWS } }), NOW).turnoverSub, /^higher than 0% of 40/);
const noTime = liquidityView(liq({ turnover: { value: 9000000, pct_rank: 50, of: 40, as_of: null } }), NOW);
assert.deepEqual([noTime.kind, noTime.turnover], ["ok", DASH], "turnover without a time is not shown; the spread still is");
assert.equal(liquidityView(liq({ turnover: { value: 9000000, pct_rank: 50, of: 40, as_of: null }, spread: { last_pct: 0.1, last_at: null } }), NOW).kind, "hidden");
for (const d of [null, undefined, "x", 5, {}, { turnover: null, spread: null }, liq({ turnover: { value: 0, as_of: NOWS }, spread: { last_pct: -1, last_at: NOWS } })]) assert.equal(liquidityView(d, NOW).kind, "hidden");
assert.equal(liquidityView(liq({ spread: { last_pct: 0, last_at: NOWS, median_pct: 0, max_pct: 0, minutes: 1 } }), NOW).spread, "0.00%", "a recorded 0.00 % spread is shown, not dashed");
ok("missing = dash, no time = not shown, a real zero is kept");

const popup2 = readFileSync(new URL("./components/board/StockPopup.tsx", import.meta.url), "utf8").replace(/^\s*\/\/.*$/gm, "");
assert.match(popup2, /liquidityView\(v\?\.liquidity, Date\.now\(\)\)/);
assert.match(popup2, /liq\.kind === "ok"/);
assert.ok(!/method:\s*"(POST|PUT|DELETE|PATCH)"|setSwitch|placeOrder/i.test(popup2));
ok("wired into the popup, hidden unless a fact has a time");
