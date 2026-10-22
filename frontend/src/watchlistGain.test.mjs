// Run: node --experimental-strip-types watchlistGain.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { parseAdded, prune, withBaseline, pctSince, formatPct, tone, isBigMove, label } from "./watchlistGain.ts";

const rec = { price: 100, ts: 1, source: "added" };
assert.ok(Math.abs(pctSince(rec, 131.74) - 31.74) < 1e-9);
assert.ok(Math.abs(pctSince(rec, 90) + 10) < 1e-9);
for (const bad of [0, -1, NaN, null, undefined, "x", Infinity]) assert.equal(pctSince(rec, bad), null);
assert.equal(pctSince(null, 10), null); assert.equal(pctSince({ ...rec, price: 0 }, 10), null);

assert.equal(formatPct(31.74), "+31.74%"); assert.equal(formatPct(-3.1), "-3.10%"); assert.equal(formatPct(0), "0.00%"); assert.equal(formatPct(null), "—");
assert.equal(tone(1), "up"); assert.equal(tone(-1), "down"); assert.equal(tone(0), "flat"); assert.equal(tone(null), "none");
assert.equal(isBigMove(5), true); assert.equal(isBigMove(-5.1), true); assert.equal(isBigMove(4.99), false); assert.equal(isBigMove(null), false); assert.equal(isBigMove(2, 2), true);

// baselines: first one wins, an explicit add replaces a first-seen guess, bad prices are ignored, input is not mutated
const m0 = {};
const m1 = withBaseline(m0, "tcs", 100, "first_seen", 5);
assert.deepEqual(m0, {}); assert.equal(m1.TCS.price, 100); assert.equal(m1.TCS.source, "first_seen");
assert.equal(withBaseline(m1, "TCS", 200, "first_seen", 6), m1);                       // unchanged
const m2 = withBaseline(m1, "TCS", 120, "added", 7); assert.equal(m2.TCS.price, 120); assert.equal(m2.TCS.source, "added");
assert.equal(withBaseline(m2, "TCS", 300, "added", 8), m2);                           // an explicit baseline is never overwritten
assert.equal(withBaseline(m0, "X", 0, "added", 1), m0); assert.equal(withBaseline(m0, "X", NaN, "added", 1), m0);

assert.deepEqual(Object.keys(prune(m2, ["infy"])), []); assert.deepEqual(Object.keys(prune({ ...m2, INFY: rec }, ["tcs"])), ["TCS"]);

// storage parsing never throws and drops junk
assert.deepEqual(parseAdded("not json"), {}); assert.deepEqual(parseAdded(null), {}); assert.deepEqual(parseAdded("[1]"), {});
const parsed = parseAdded(JSON.stringify({ tcs: rec, BAD: { price: -1, ts: 1, source: "added" }, ODD: { price: 1, ts: 1, source: "x" } }));
assert.deepEqual(Object.keys(parsed), ["TCS"]);

assert.equal(label(rec), "since added"); assert.equal(label({ ...rec, source: "first_seen" }), "since first seen"); assert.equal(label(null), "");
console.log("watchlistGain: ok");
