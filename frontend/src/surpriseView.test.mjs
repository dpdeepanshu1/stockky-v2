// Run: node --experimental-strip-types src/surpriseView.test.mjs
import assert from "node:assert/strict";
import { surpriseKpis, surpriseRowView } from "./components/surpriseView.ts";
import { readFileSync } from "node:fs";

assert.deepEqual(surpriseKpis([], null), []);
assert.deepEqual(surpriseKpis(null, undefined), []);
const rows = [{ symbol: "AAA", score: 71, rvol: 3.26, tier: "breakout" }, { symbol: "BBB", score: 64, rvol: 5.04, tier: "building" }, { symbol: "CCC", score: 80, rvol: 0, tier: "building" }];
const k = surpriseKpis(rows, { quotes_ok: 180, universe_scanned: 200, static_loaded: 190 });
assert.equal(k[0].value, "3"); assert.equal(k[0].sub, "breakout 1 · building 2");
assert.equal(k[1].value, "80"); assert.equal(k[1].sub, "CCC");
assert.equal(k[2].value, "5.0x"); assert.equal(k[2].sub, "BBB");
assert.equal(k[3].value, "180/200"); assert.equal(k[3].tone, "flat"); assert.match(k[3].sub, /baselines 190/);
// zero is a reading, not missing
const z = surpriseKpis([{ symbol: "Z", score: 0, rvol: 0 }], null);
assert.equal(z[1].value, "0"); assert.equal(z[2].value, "0.0x");
// nothing numeric -> dashes
const n = surpriseKpis([{ symbol: "N", score: null, rvol: undefined }], null);
assert.equal(n[1].value, "—"); assert.equal(n[2].value, "—"); assert.equal(n[3].value, "—");
// no live quotes is a warning, also spelled in words
const w = surpriseKpis([], { quotes_ok: 0, universe_scanned: 300 });
assert.equal(w[3].tone, "warn"); assert.match(w[3].sub, /No live quotes/); assert.equal(w[3].value, "0/300");
// missing quotes_ok is not "no quotes"
const m = surpriseKpis([], { universe_scanned: 300 });
assert.equal(m[3].tone, "flat"); assert.equal(m[3].value, "—/300");
for (const x of k) assert.equal(typeof x.value, "string");
// E161: row view - dashes for missing, a real 0 stays 0, words and glyphs carry the meaning
const full = surpriseRowView({ score: 72.4, tier: "breakout", change_pct: 3.456, rvol: 4.2, rvol_slope: 0.31, target_1: 105, trailing_stop: 98.5 });
assert.equal(full.score.text, "72/100"); assert.equal(full.score.word, "Strong"); assert.equal(full.score.tone, "buy");
assert.deepEqual(full.tier, { label: "Breakout", glyph: "▲", tone: "buy" });
assert.deepEqual(full.change, { text: "+3.46%", dir: "up" }); assert.equal(full.rvol, "4.20x"); assert.equal(full.slope, "↑0.31");
assert.equal(full.target, "₹105.00"); assert.equal(full.stop, "₹98.50");
const empty = surpriseRowView({ score: null, change_pct: undefined, rvol: NaN, target_1: "x", trailing_stop: null });
assert.equal(empty.score, null); assert.deepEqual(empty.change, { text: "—", dir: null }); assert.equal(empty.rvol, "—");
assert.equal(empty.target, "—"); assert.equal(empty.stop, "—"); assert.equal(empty.slope, null);
for (const bad of [empty.rvol, empty.target, empty.stop, empty.change.text]) assert.ok(!/NaN|null|undefined/.test(bad));
const zero = surpriseRowView({ score: 0, tier: "building", change_pct: 0, rvol: 0, rvol_slope: 0, target_1: 0 });
assert.equal(zero.score.text, "0/100"); assert.equal(zero.score.word, "Weak"); assert.equal(zero.change.text, "0.00%"); assert.equal(zero.change.dir, "flat");
assert.deepEqual(zero.tier, { label: "Building", glyph: "◆", tone: "warn" }); assert.equal(zero.rvol, "0.00x"); assert.equal(zero.slope, null); assert.equal(zero.target, "₹0.00"); assert.equal(zero.tier.label, "Building");
assert.equal(surpriseRowView({ change_pct: -1.2 }).change.text, "-1.20%"); assert.equal(surpriseRowView({ change_pct: -1.2 }).change.dir, "down");
assert.equal(surpriseRowView({ score: 150 }).score.text, "100/100"); assert.equal(surpriseRowView({ score: 55 }).score.word, "Good"); assert.equal(surpriseRowView({ score: 40 }).score.word, "Mixed");
// source guards
const tsx = readFileSync(new URL("./components/SurpriseStocks.tsx", import.meta.url), "utf8");
const css = readFileSync(new URL("./design-v7.css", import.meta.url), "utf8");
assert.ok(/surpriseRowView\(s\)/.test(tsx) && !/Number\(s\.(change_pct|rvol|target_1|trailing_stop)\)/.test(tsx), "cells read the row view, no raw Number(x).toFixed");
assert.ok(/v7-tier/.test(tsx) && /v\.tier\.glyph/.test(tsx) && /aria-label=\{`Score /.test(tsx), "tier has a glyph, score has a spoken label");
assert.ok(/Target \+5%/.test(tsx) && /Trail \(VWAP\)/.test(tsx), "column headings unchanged");
assert.ok(!/animation\s*:/.test(css.slice(css.indexOf("E161"))), "E161 CSS adds no animation");
console.log("surpriseView ok");
