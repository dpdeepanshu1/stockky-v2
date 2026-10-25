// Run: node --experimental-strip-types src/serviceView.test.mjs
import assert from "node:assert/strict";
import { serviceKpis, checkAge } from "./components/serviceView.ts";

const NOW = 1_000_000_000;
assert.deepEqual(serviceKpis(null, NOW), []);
assert.deepEqual(serviceKpis(undefined, NOW), []);

// first load: no invented counts
const load = serviceKpis({ services: {}, loading: true, failed: false, checkedAt: null }, NOW);
assert.equal(load.length, 4);
assert.equal(load[0].value, "—"); assert.equal(load[0].sub, "waiting for the first check"); assert.equal(load[0].tone, "flat");
assert.equal(load[3].value, "Never"); assert.equal(load[3].sub, "checking now");
// the health call failed and nothing is known: say so, never "0/5"
const fail = serviceKpis({ services: {}, loading: false, failed: true, checkedAt: null }, NOW);
assert.equal(fail[0].value, "—"); assert.equal(fail[0].tone, "warn"); assert.match(fail[0].sub, /did not answer/);
assert.equal(fail[3].tone, "warn");
// answered with an empty list
assert.equal(serviceKpis({ services: {}, loading: false, failed: false, checkedAt: NOW }, NOW)[0].sub, "no services reported");

// all healthy
const ok = serviceKpis({ services: { "api-gateway": { ok: true, required: true, latency_ms: 40 }, "market-data": { ok: true, required: true, latency_ms: 220.4 }, prediction: { ok: true, required: false } }, loading: false, failed: false, checkedAt: NOW - 30_000 }, NOW);
assert.equal(ok[0].value, "3/3"); assert.equal(ok[0].tone, "info"); assert.equal(ok[0].sub, "all answering");
assert.equal(ok[1].value, "2/2"); assert.equal(ok[1].sub, "every required service answering");
assert.equal(ok[2].value, "220ms"); assert.equal(ok[2].sub, "market-data");
assert.equal(ok[3].value, "30s ago"); assert.equal(ok[3].tone, "flat");

// down services are named, required ones called out
const bad = serviceKpis({ services: { a: { ok: true, required: true }, b: { ok: false, required: true }, c: { ok: false }, d: { ok: false }, e: { ok: false } }, loading: false, failed: false, checkedAt: NOW }, NOW);
assert.equal(bad[0].value, "1/5"); assert.equal(bad[0].tone, "warn"); assert.equal(bad[0].sub, "Down: b, c, d +1 more");
assert.equal(bad[1].value, "1/2"); assert.equal(bad[1].tone, "warn"); assert.equal(bad[1].sub, "Required and down: b");
// a service with no entry value counts as down, not as healthy
assert.equal(serviceKpis({ services: { a: null }, loading: false, failed: false, checkedAt: NOW }, NOW)[0].value, "0/1");

// latency: a real 0 stays 0; absent is a dash, never "<100ms"
assert.equal(serviceKpis({ services: { a: { ok: true, latency_ms: 0 } }, loading: false, failed: false, checkedAt: NOW }, NOW)[2].value, "0ms");
const nolat = serviceKpis({ services: { a: { ok: true } }, loading: false, failed: false, checkedAt: NOW }, NOW)[2];
assert.equal(nolat.value, "—"); assert.equal(nolat.sub, "no latency reported");
assert.equal(serviceKpis({ services: { a: { ok: true, latency_ms: NaN } }, loading: false, failed: false, checkedAt: NOW }, NOW)[2].value, "—");
// required flag absent everywhere
assert.equal(serviceKpis({ services: { a: { ok: true } }, loading: false, failed: false, checkedAt: NOW }, NOW)[1].sub, "not reported");

// a failed refresh after a good one keeps the earlier answer but warns
const stale = serviceKpis({ services: { a: { ok: true } }, loading: false, failed: true, checkedAt: NOW - 120_000 }, NOW);
assert.equal(stale[0].value, "1/1"); assert.equal(stale[3].value, "2m ago"); assert.equal(stale[3].tone, "warn"); assert.match(stale[3].sub, /earlier answer/);
assert.equal(checkAge(-10), "just now"); assert.equal(checkAge(3 * 3_600_000), "3.0h ago");
for (const k of ok) assert.equal(typeof k.value, "string");
console.log("serviceView ok");
