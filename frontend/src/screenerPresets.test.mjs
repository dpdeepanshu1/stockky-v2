// Run: node --experimental-strip-types screenerPresets.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { PRESETS, getPreset, applyPreset, missingFundamentals } from "./screenerPresets.ts";

const row = (symbol, m) => ({ symbol, fundamental_metrics: m });
const rows = [
  row("GOOD", { earnings_growth: 20, revenue_growth: 12, roe: 18, debt_to_equity: 0.3, pe_ratio: 15 }),
  row("DEBTY", { earnings_growth: 25, revenue_growth: 10, roe: 20, debt_to_equity: 1.8, pe_ratio: 12 }),
  row("SHRINK", { earnings_growth: -5, revenue_growth: -2, roe: 5, debt_to_equity: 0.2, pe_ratio: 30 }),
  row("NULLS", { earnings_growth: null, revenue_growth: 10, roe: null, debt_to_equity: 0.1, pe_ratio: null }),
  row("NOMETRICS", null),
  { symbol: "UNDEF" },
  row("EDGE", { earnings_growth: 10, revenue_growth: 0, roe: 15, debt_to_equity: 0.5, pe_ratio: 20 }),
  row("NANROW", { earnings_growth: NaN, revenue_growth: 5, roe: 15, debt_to_equity: 0.5, pe_ratio: -3 }),
];
const syms = (id) => applyPreset(rows, id).map((r) => r.symbol);

assert.deepEqual(syms("profit_up_debt_low"), ["GOOD"]);                 // EDGE: growth exactly 10 and revenue exactly 0 fail the strict ">"
assert.deepEqual(syms("quality"), ["GOOD", "EDGE", "NANROW"]);       // ROE>=15, D/E<=0.5 inclusive; NANROW only has a NaN in earnings, which quality does not use
assert.deepEqual(syms("growth"), []);                                   // nobody has both >= 15
assert.deepEqual(syms("value"), ["GOOD", "DEBTY", "EDGE"]);          // PE<=20 inclusive, ROE>=12, no debt rule; negative PE excluded
assert.equal(applyPreset(rows, null), rows);                             // no preset -> same array
assert.equal(applyPreset(rows, "nope"), rows);                           // unknown id -> same array
assert.equal(getPreset("quality").label, "Quality");
assert.equal(getPreset(undefined), null);
assert.equal(missingFundamentals(rows), 2);
assert.equal(new Set(PRESETS.map((p) => p.id)).size, PRESETS.length);
assert.equal(applyPreset([], "quality").length, 0);
console.log("screenerPresets: ok");
