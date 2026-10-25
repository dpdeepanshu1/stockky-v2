// design v7 logic guards (run with: node --experimental-strip-types src/v7.test.mjs)
import assert from "node:assert/strict";
import { railView, exposureView, ladderView, ladderMode, evidenceView, capitalView, beatKind, sideFrom } from "./v7View.ts";

let n = 0;
const t = (name, fn) => { fn(); n++; console.log("ok", name); };

t("long rail: stop is left, target right, profit moves right", () => {
  const a = railView({ side: "LONG", entry: 100, stop: 98, target: 106, price: 103 });
  assert.equal(a.ok, true); assert.equal(a.entryPct, 25); assert.equal(a.pricePct, 62.5); assert.equal(a.state, "ahead"); assert.equal(a.arrow, "▲");
  assert.equal(a.rewardDonePct, 50); assert.equal(a.riskUsedPct, 0);
  const b = railView({ side: "LONG", entry: 100, stop: 98, target: 106, price: 99 });
  assert.equal(b.state, "under"); assert.equal(b.arrow, "▼"); assert.equal(b.riskUsedPct, 50);
});
t("short rail: same picture, profit still to the right (price falling)", () => {
  const a = railView({ side: "SHORT", entry: 100, stop: 102, target: 94, price: 97 });
  assert.equal(a.ok, true); assert.equal(a.entryPct, 25); assert.equal(a.pricePct, 62.5); assert.equal(a.state, "ahead"); assert.equal(a.side, "SHORT");
  const b = railView({ side: "short", entry: 100, stop: 102, target: 94, price: 101.5 });
  assert.equal(b.state, "near_stop"); assert.equal(b.riskUsedPct, 75);
});
t("rail edge states: at stop, at target, flat, no price", () => {
  assert.equal(railView({ entry: 100, stop: 98, target: 106, price: 97 }).state, "at_stop");
  assert.equal(railView({ entry: 100, stop: 98, target: 106, price: 107 }).state, "at_target");
  assert.equal(railView({ entry: 100, stop: 98, target: 106, price: 107 }).pricePct, 100);
  assert.equal(railView({ entry: 100, stop: 98, target: 106, price: 100 }).state, "flat");
  const np = railView({ entry: 100, stop: 98, target: 106 }); assert.equal(np.ok, true); assert.equal(np.state, "unknown");
});
t("rail refuses levels it cannot draw honestly (missing, zero, wrong order for the direction)", () => {
  for (const p of [{}, { entry: 100 }, { entry: 100, stop: 98 }, { entry: 100, stop: 0, target: 106 }, { entry: NaN, stop: 98, target: 106 }, { entry: 100, stop: 100, target: 100 }]) assert.equal(railView(p).ok, false);
  assert.equal(railView({ side: "LONG", entry: 100, stop: 107, target: 106 }).ok, false);   // a long with its stop past the target
  assert.equal(railView({ side: "SHORT", entry: 100, stop: 93, target: 94 }).ok, false);    // a short with its stop past the target
  assert.equal(railView({ side: "LONG", entry: 100, stop: 98, target: 99 }).ok, false);     // a long whose target is below its entry
  assert.equal(railView({ side: "SHORT", entry: 100, stop: 102, target: 101 }).ok, false);  // a short whose target is above its entry
  assert.equal(sideFrom(" short "), "SHORT"); assert.equal(sideFrom(undefined), "LONG"); assert.equal(sideFrom("weird"), "LONG");
});
t("trailed stop (breakeven or profit locked) still draws: rail starts at the entry, stop gets its own tick, long and short", () => {
  const a = railView({ side: "LONG", entry: 100, stop: 102, target: 106, price: 104 });   // stop trailed above entry
  assert.equal(a.ok, true); assert.equal(a.locked, true); assert.equal(a.entryPct, 0); assert.equal(a.stopPct, 33.3); assert.equal(a.pricePct, 66.7);
  assert.equal(a.state, "ahead"); assert.equal(a.riskUsedPct, 0); assert.match(a.words, /profit is locked in/);
  const b = railView({ side: "LONG", entry: 100, stop: 100, target: 106, price: 101 });   // exactly breakeven
  assert.equal(b.ok, true); assert.equal(b.locked, true); assert.equal(b.stopPct, 0); assert.equal(b.state, "ahead");
  const c = railView({ side: "SHORT", entry: 100, stop: 98, target: 94, price: 96 });     // short, stop trailed below entry
  assert.equal(c.ok, true); assert.equal(c.locked, true); assert.equal(c.entryPct, 0); assert.equal(c.stopPct, 33.3); assert.equal(c.pricePct, 66.7); assert.equal(c.state, "ahead");
  // price at or through a trailed stop = the stop is being hit, never "under water" and never a near-stop warning
  const d = railView({ side: "LONG", entry: 100, stop: 102, target: 106, price: 101 }); assert.equal(d.state, "at_stop"); assert.equal(d.riskUsedPct, 0);
  const e = railView({ side: "LONG", entry: 100, stop: 102, target: 106, price: 99 }); assert.equal(e.state, "at_stop"); assert.equal(e.riskUsedPct, 0); assert.equal(e.pricePct, 0);
  const f = railView({ side: "SHORT", entry: 100, stop: 98, target: 94, price: 99 }); assert.equal(f.state, "at_stop");
  assert.equal(railView({ side: "LONG", entry: 100, stop: 102, target: 106, price: 107 }).state, "at_target");
  const g = railView({ side: "LONG", entry: 100, stop: 102, target: 106 }); assert.equal(g.ok, true); assert.equal(g.state, "unknown"); assert.equal(g.locked, true);
  // a normal (untrailed) rail is untouched
  const h = railView({ side: "LONG", entry: 100, stop: 98, target: 106, price: 103 }); assert.equal(h.locked, false); assert.equal(h.stopPct, 0); assert.equal(h.entryPct, 25);
});
t("exposure: money in = entry x qty, long vs short split, near-stop list, biggest first", () => {
  const v = exposureView([
    { symbol: "AAA", qty: 10, entry: 100, pnl: 50, riskUsedPct: 10 },
    { symbol: "BBB", side: "SHORT", qty: 5, entry: 200, pnl: -20, riskUsedPct: 80 },
    { symbol: "CCC", qty: 1, entry: 100, pnl: 0 },
  ]);
  assert.equal(v.count, 3); assert.equal(v.longCount, 2); assert.equal(v.shortCount, 1); assert.equal(v.longValue, 1100); assert.equal(v.shortValue, 1000); assert.equal(v.totalValue, 2100);
  assert.equal(v.pnlTotal, 30); assert.deepEqual(v.nearStop, ["BBB"]);
  assert.deepEqual(v.segments.map((s) => s.symbol), ["AAA", "BBB", "CCC"]);
  assert.deepEqual(v.segments.map((s) => s.tone), ["up", "down", "flat"]); assert.deepEqual(v.segments.map((s) => s.arrow), ["▲", "▼", "•"]);
  assert.ok(Math.abs(v.segments.reduce((a, s) => a + s.widthPct, 0) - 100) < 0.5);
});
t("exposure tolerates junk and empty input", () => {
  for (const x of [null, undefined, [], [null], [{}], "x"]) { const v = exposureView(x); assert.equal(v.count, 0); assert.equal(v.segments.length, 0); }
  const z = exposureView([{ symbol: "ZZZ", qty: 0, entry: 0 }]); assert.equal(z.count, 1); assert.equal(z.segments[0].widthPct, 100); assert.equal(z.pnlTotal, null);
});
t("ladder: exactly one current rung; unknown reads as the safe OFF rung; only live has orders", () => {
  for (const m of ["off", "shadow", "live"]) { const v = ladderView(m, "X"); assert.equal(v.steps.filter((s) => s.state === "current").length, 1); assert.equal(v.mode, m); }
  assert.equal(ladderView("live").orders, true); assert.equal(ladderView("shadow").orders, false); assert.equal(ladderView("off").orders, false);
  for (const m of [null, undefined, "", "junk", "unavailable"]) assert.equal(ladderMode(m), "off");
  assert.deepEqual(ladderView("shadow").steps.map((s) => s.state), ["past", "current", "future"]);
  assert.match(ladderView("live", "Swing lane").words, /Swing lane is LIVE/); assert.equal(ladderView("live").tone, "bad");
});
t("evidence: the slower of trades and days is the overall figure; ready needs both", () => {
  const a = evidenceView(15, 30, 5, 20); assert.equal(a.tradesPct, 50); assert.equal(a.daysPct, 25); assert.equal(a.overallPct, 25); assert.equal(a.ready, false);
  assert.equal(evidenceView(30, 30, 19, 20).ready, false); assert.equal(evidenceView(30, 30, 20, 20).ready, true);
  assert.equal(evidenceView(99, 30, 99, 20).tradesPct, 100); const z = evidenceView(undefined, 0, "x", null); assert.equal(z.overallPct, 0); assert.equal(z.ready, false);
});
t("capital ring: shares add to 100, free part never exceeds the pool, unknown balance draws nothing", () => {
  const v = capitalView(100000, 50000, 20000, 50); assert.equal(v.ok, true); assert.equal(v.psPct, 50); assert.equal(v.rtPct, 50); assert.equal(v.psFreePct, 20);
  assert.equal(capitalView(100000, 50000, 90000, 50).psFreePct, 50);
  assert.equal(capitalView(100000, null, null, 40).psPct, 40);
  for (const x of [null, 0, -5, NaN, undefined]) assert.equal(capitalView(x, 1, 1, 50).ok, false);
  assert.equal(capitalView(1000, 5000, 0, 50).psPct, 100);
});
t("drift beat: error = alarm, drift with warnings = warn, clean/steady = steady, anything else idle", () => {
  assert.equal(beatKind("error", 0), "alarm"); assert.equal(beatKind("drift", 2), "warn"); assert.equal(beatKind("drift", 0), "steady");
  assert.equal(beatKind("clean", 0), "steady"); for (const k of ["unavailable", null, undefined, "x"]) assert.equal(beatKind(k, 3), "idle");
});
console.log(`${n} v7 logic tests passed`);
