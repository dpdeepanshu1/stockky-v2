// Run: node --experimental-strip-types src/trainingView.test.mjs
import assert from "node:assert/strict";
import { trainingKpis, trainingAge, fmtMetric, fmtFixed, outcomeMark, predictionRowView } from "./components/trainingView.ts";
import { readFileSync } from "node:fs";

const NOW = Date.parse("2026-10-25T10:00:00Z");
assert.deepEqual(trainingKpis(null, NOW), []);
assert.deepEqual(trainingKpis({ status: null }, NOW), []);

// nothing trained yet: no invented zeros or rates
const none = trainingKpis({ status: { production_model_exists: false, last_training: null } }, NOW);
assert.equal(none.length, 4);
assert.equal(none[0].value, "None deployed"); assert.equal(none[0].tone, "warn");
assert.equal(none[1].value, "Never"); assert.equal(none[1].tone, "warn");
assert.equal(none[2].value, "—"); assert.equal(none[2].sub, "symbol count not reported");
assert.equal(none[3].value, "—"); assert.match(none[3].sub, /no walk-forward metrics/);

// deployed model with figures
const ok = trainingKpis({ status: { production_model_exists: true, model_version: "v12", last_training: "2026-10-25T04:00:00Z", dataset_size: 5400, num_symbols: 220, metrics: { WinRate: 0.553, SharpeRatio: 1.234, DirectionalAccuracy: 0.6 } } }, NOW);
assert.equal(ok[0].value, "Deployed"); assert.equal(ok[0].sub, "version v12");
assert.equal(ok[1].value, "6.0h ago"); assert.match(ok[1].sub, /2026/);
assert.equal(ok[2].value, "5400"); assert.equal(ok[2].sub, "220 symbols");
assert.equal(ok[3].value, "55.3%"); assert.equal(ok[3].sub, "Sharpe 1.23 · direction 60.0%");
// production version wins over model_version; missing version is said
assert.equal(trainingKpis({ status: { production_model_exists: true, production_model_version: "p1", model_version: "m1" } }, NOW)[0].sub, "version p1");
assert.equal(trainingKpis({ status: { production_model_exists: true } }, NOW)[0].sub, "version not reported");
// zeros are readings
const z = trainingKpis({ status: { production_model_exists: true, dataset_size: 0, num_symbols: 0, metrics: { WinRate: 0 } } }, NOW);
assert.equal(z[2].value, "0"); assert.equal(z[2].sub, "0 symbols"); assert.equal(z[3].value, "0.0%");
// non-numeric metric is missing, not 0
assert.equal(trainingKpis({ status: { metrics: { WinRate: NaN } } }, NOW)[3].value, "—");
// unreadable date
const bad = trainingKpis({ status: { last_training: "garbage" } }, NOW)[1];
assert.equal(bad.value, "—"); assert.equal(bad.sub, "date unreadable"); assert.equal(bad.tone, "warn");
assert.equal(trainingAge(-1), "just now"); assert.equal(trainingAge(3 * 3_600_000 * 24), "3.0d ago");

// live run tile only while running
assert.equal(trainingKpis({ status: {} }, NOW).length, 4);
const run = trainingKpis({ status: { training_in_progress: true }, progress: { stage: "walk_forward", percent: 42.4 } }, NOW)[4];
assert.equal(run.value, "42%"); assert.equal(run.sub, "walk forward"); assert.equal(run.tone, "info");
assert.equal(trainingKpis({ status: {}, training: true, progress: null }, NOW)[4].value, "Running");
assert.equal(trainingKpis({ status: {}, training: true, progress: { stage: "idle", percent: 0 } }, NOW)[4].value, "0%");
const err = trainingKpis({ status: {}, training: true, progress: { stage: "error", error: "boom" } }, NOW)[4];
assert.equal(err.value, "Error"); assert.equal(err.tone, "warn"); assert.equal(err.sub, "boom");
for (const x of ok) assert.equal(typeof x.value, "string");
// E164: card bodies
assert.equal(fmtMetric("WinRate", 0.5234), "52.34%"); assert.equal(fmtMetric("MaximumDrawdown", -0.12), "-12.00%");
assert.equal(fmtMetric("SharpeRatio", 1.23456), "1.235"); assert.equal(fmtMetric("RMSE", 0.12345678), "0.1235");
assert.equal(fmtMetric("WinRate", 0), "0.00%"); assert.equal(fmtMetric("SharpeRatio", 0), "0.000");
assert.equal(fmtMetric("ProfitFactor", Infinity), "∞"); assert.equal(fmtMetric("RMSE", Infinity), null);
for (const bad of [undefined, null, NaN, "1.2", {}, -Infinity]) assert.equal(fmtMetric("WinRate", bad), null, `metric ${String(bad)} hides the tile`);
assert.equal(fmtFixed(0, 3), "0.000"); assert.equal(fmtFixed(undefined, 3), "—"); assert.equal(fmtFixed("x", 3), "—"); assert.equal(fmtFixed(NaN, 3), "—");
assert.deepEqual(outcomeMark(1), { glyph: "✅", word: "Hit", tone: "up" }); assert.deepEqual(outcomeMark(2), { glyph: "❌", word: "Miss", tone: "down" });
assert.deepEqual(outcomeMark(0), { glyph: "⏳", word: "Pending", tone: "flat" });
for (const unk of [null, undefined, 3, "1", NaN]) assert.deepEqual(outcomeMark(unk), { glyph: "—", word: "Not evaluated", tone: "flat" }, `${String(unk)} is not a miss`);
const row = predictionRowView({ price: 1234.5, t1_success: 1, t5_success: 0, outcomes: [{ period: "T+1", return_pct: 0.5 }, { period: "T+5", return_pct: 0 }, { period: "T+10" }] });
assert.equal(row.price, "₹1234.50"); assert.equal(row.t1.word, "Hit"); assert.equal(row.t5.word, "Pending"); assert.equal(row.outcomes, "T+1: 0.50% | T+5: 0.00% | T+10: —");
const rb = predictionRowView({ price: "x", outcomes: "no" }); assert.equal(rb.price, "—"); assert.equal(rb.outcomes, "—"); assert.equal(rb.t1.word, "Not evaluated");
assert.equal(predictionRowView({ price: 0 }).price, "—"); assert.equal(predictionRowView(null).price, "—");
for (const txt of [rb.price, rb.outcomes, row.outcomes]) assert.ok(!/undefined|NaN|null|₹—|—%/.test(txt));
const tsx = readFileSync(new URL("./components/Training.tsx", import.meta.url), "utf8");
const css = readFileSync(new URL("./design-v7.css", import.meta.url), "utf8");
assert.ok(/predictionRowView\(p\)/.test(tsx) && /fmtMetric\(key, metrics\[key\]\)/.test(tsx) && !/p\.price\?\.toFixed/.test(tsx) && !/formatValue\(/.test(tsx), "cells read the helpers, no raw toFixed on API values");
assert.ok(!/p\.t1_success === 1 \? "✅"/.test(tsx) && /T\+1 Success/.test(tsx) && /T\+5 Success/.test(tsx), "old glyph chain gone, headings kept");
assert.ok(!/animation\s*:/.test(css.slice(css.indexOf("E164"))), "E164 CSS adds no animation");
console.log("trainingView ok");
