// round 552 (P3): the Chain tab's ATM IV, straddle / expected move, change in OI and the two charts. Pure words and geometry; a missing number is a dash or a gap, never 0.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { chainRows, expectedMoveView, ivPaths, ivPoints, oiBars, oiChange, oiChangePoints, oiChangeTotals, signedN0 } from "./fnoChainView.ts";

const ok = (m) => console.log("ok - " + m);
const MINUS = "−";

assert.equal(signedN0(1234.4), "+1,234");
assert.equal(signedN0(-1234.4), `${MINUS}1,234`);
assert.equal(signedN0(0), "0", "a real zero is 0");
for (const bad of [null, undefined, NaN, Infinity, "x"]) assert.equal(signedN0(bad), "-");
assert.notEqual(signedN0(-5)[0], "-", "a negative number never starts with the dash used for missing");
ok("signed numbers: real minus sign, a real 0 stays 0, missing is a dash");

assert.equal(oiChange({ oi: 150, prev_oi: 100 }), 50);
assert.equal(oiChange({ oi: 80, prev_oi: 100 }), -20);
assert.equal(oiChange({ oi: 0, prev_oi: 0 }), 0);
for (const s of [null, undefined, {}, { oi: 5 }, { prev_oi: 5 }, { oi: NaN, prev_oi: 1 }, { oi: 5, prev_oi: null }]) assert.equal(oiChange(s), null);
ok("change in OI needs both numbers");

const strikes = [
  { strike: 24400, ce: { oi: 300, prev_oi: 200, iv: 15 }, pe: { oi: 100, prev_oi: 150, iv: 16 } },
  { strike: 24500, ce: { oi: 500, prev_oi: 500, iv: 14 }, pe: { oi: 400, prev_oi: 300, iv: 0 } },
  { strike: 24600, ce: { oi: 200, prev_oi: null, iv: null }, pe: { oi: 250, prev_oi: 100, iv: 13 } },
];
const rows = chainRows(strikes, 24500, "NIFTY");
const t = oiChangeTotals(rows);
assert.deepEqual(t, { ce: 100, pe: 200, strikes: 2 });                       // ce: +100 +0 (24600 has no prev_oi, left out); pe: -50 +100 +150
assert.deepEqual(oiChangeTotals([]), { ce: null, pe: null, strikes: 0 });
assert.deepEqual(oiChangeTotals(chainRows([{ strike: 24500, ce: { oi: 5 }, pe: null }], 24500, "NIFTY")), { ce: null, pe: null, strikes: 0 }, "no usable strike = null, not 0");
ok("totals: strikes without both numbers are left out; none at all is null");

const em = expectedMoveView({ atm_iv: 14.2, atm_strike: 24500, straddle: 210.5, expected_move_pct: 0.859 }, 24500);
assert.deepEqual(em.iv, { value: "14.20%", sub: "average of the 24500 call and put" });
assert.deepEqual(em.straddle, { value: "210.50", sub: "±0.86%, 24,290 to 24,711" });
assert.deepEqual(expectedMoveView({ straddle: 100 }, null).straddle, { value: "100.00", sub: "call + put at the money" });
assert.deepEqual(expectedMoveView({ straddle: 100, expected_move_pct: 0.4 }, 25000).straddle.sub, "±0.40%, 24,900 to 25,100");
for (const bad of [null, undefined, {}, { atm_iv: 0, straddle: 0 }, { atm_iv: NaN, straddle: -3 }]) {
  const v = expectedMoveView(bad, 24500);
  assert.equal(v.iv.value, "-"); assert.equal(v.straddle.value, "-");
  assert.doesNotMatch(JSON.stringify(v), /NaN|undefined|Infinity|null/);
}
ok("ATM IV and straddle: the move and the range only from real numbers; missing is a dash");

const ob = oiBars(oiChangePoints(rows), 600, 150);
assert.equal(ob.max, 150);
assert.equal(ob.zeroY, 75);
const by = (side, k) => ob.bars.find((b) => b.side === side && b.strike === k);
assert.ok(by("ce", 24400).y < 75 && by("pe", 24400).y === 75, "added OI goes up, removed OI goes down");
assert.equal(by("ce", 24500), undefined, "a change of exactly 0 draws no bar");
assert.equal(by("ce", 24600), undefined, "a missing change draws no bar");
assert.ok(by("pe", 24600).h > by("pe", 24400).h && by("pe", 24600).h === (150 / 150) * (75 - 3), "the biggest move fills the height; one scale for calls and puts");
assert.deepEqual(oiBars([], 600, 150).bars, []);
assert.equal(oiBars(oiChangePoints(chainRows([{ strike: 24500, ce: null, pe: null }], 24500, "NIFTY")), 600, 150).max, 0);
ok("OI bars: up for added, down for removed, no bar for zero or missing, one shared scale");

const ip = ivPoints(rows);
assert.deepEqual(ip.map((p) => [p.ce, p.pe]), [[15, 16], [14, null], [null, 13]], "an IV of 0 or missing is a gap");
const lines = ivPaths(ip, 600, 150);
assert.equal(lines.lo, 13); assert.equal(lines.hi, 16);
assert.equal(lines.atmX, 300, "the ATM strike (the middle of three) is marked");
assert.equal((lines.pe.match(/M/g) || []).length, 2, "the put line is broken where the IV is missing (two pieces)");
assert.equal((lines.ce.match(/M/g) || []).length, 1);
assert.deepEqual(ivPaths([], 600, 150), { ce: "", pe: "", lo: null, hi: null, atmX: null });
assert.doesNotMatch(JSON.stringify(ivPaths(ivPoints(chainRows([{ strike: 24500, ce: { iv: 12 }, pe: { iv: 12 } }], 24500, "NIFTY")), 600, 150)), /NaN|Infinity/);
ok("IV lines: gaps where missing, never a point at 0; one flat value does not divide by zero");

const comp = readFileSync(new URL("./components/fno/FnoChain.tsx", import.meta.url), "utf8");
const charts = readFileSync(new URL("./components/fno/ChainCharts.tsx", import.meta.url), "utf8");
const view = readFileSync(new URL("./fnoChainView.ts", import.meta.url), "utf8");
for (const label of ["ATM IV", "ATM straddle", "Call OI change", "Put OI change"]) assert.match(comp, new RegExp(`label="${label}"`));
assert.match(comp, /<ChainCharts rows=\{rows\} \/>/);
assert.match(comp, /expectedMoveView\(s, d\?\.chain\.spot\)/);
assert.match(view, /prev_oi\?: number \| null/);
assert.match(charts, /role="img"/);
assert.ok(!/fetch\(|fnoApi|localStorage|sessionStorage/.test(charts + view.slice(view.indexOf("round 552"))), "no call and nothing stored");
ok("wired: four stats and the charts on the Chain tab, accessible chart labels, pure helpers");
console.log("chainNumbers.test.mjs: 8 ok");
