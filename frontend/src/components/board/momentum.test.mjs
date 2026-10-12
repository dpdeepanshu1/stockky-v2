// Run: node --experimental-strip-types components/board/momentum.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { computeMomentum, fmtPct } from "./momentum.ts";

const bar = (i, o, c, v = 1000) => [i * 300, o, Math.max(o, c) + 0.1, Math.min(o, c) - 0.1, c, v];
const up = Array.from({ length: 10 }, (_, i) => bar(i, 100 + i * 0.5, 100.5 + i * 0.5, i === 9 ? 3000 : 1000));
const m = computeMomentum(up, 105.2, 102, 5);
assert.equal(m.label, "Strong up"); assert.equal(m.tone, "g"); assert.ok(m.score > 45);
assert.equal(m.greenOf5, 5); assert.ok(m.shortPct > 0 && m.longPct > 0 && m.vwapPct > 0); assert.ok(m.volRatio > 2);
assert.ok(m.dayPos > 0.9);

const dn = Array.from({ length: 10 }, (_, i) => bar(i, 110 - i * 0.5, 109.5 - i * 0.5));
const d = computeMomentum(dn, 105, 108, 5);
assert.equal(d.label, "Strong down"); assert.equal(d.tone, "r"); assert.ok(d.score < -45);

const flat = Array.from({ length: 10 }, (_, i) => bar(i, 100, i % 2 ? 100.05 : 99.95));
const f = computeMomentum(flat, 100, 100, 5);
assert.equal(f.label, "Flat"); assert.equal(f.tone, "flat");

// not enough bars / junk input never throws
assert.equal(computeMomentum([], 100, 100).label, "Warming up");
assert.equal(computeMomentum(null, null, null).label, "Warming up");
assert.equal(computeMomentum([[1, 2], "x", null], 1, 1).label, "Warming up");
assert.equal(computeMomentum(up.slice(0, 3), 101, null).bars, 3);
// no live price: falls back to the last close
assert.ok(computeMomentum(up, null, null).score > 0);
// no volume column
assert.equal(computeMomentum(up.map(k => k.slice(0, 5)), 105, 102).volRatio, null);

assert.equal(fmtPct(0.424), "+0.42%"); assert.equal(fmtPct(-1.1), "-1.10%"); assert.equal(fmtPct(null), "-"); assert.equal(fmtPct(NaN), "-");
console.log("momentum tests ok");
