// Run: node --experimental-strip-types src/trainingView.test.mjs
import assert from "node:assert/strict";
import { trainingKpis, trainingAge } from "./components/trainingView.ts";

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
console.log("trainingView ok");
