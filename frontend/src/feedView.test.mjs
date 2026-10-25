// Run: node --experimental-strip-types src/feedView.test.mjs
import assert from "node:assert/strict";
import { feedKpis, feedAge } from "./components/feedView.ts";

const NOW = Date.parse("2026-10-25T10:00:00Z");
const base = { status: "idle", processed: 0, total: 0, pct: 0, partial: false };

assert.deepEqual(feedKpis(null, NOW), []);
assert.deepEqual(feedKpis(undefined, NOW), []);

// nothing loaded yet: no invented zeros
const empty = feedKpis({ ...base, job: null, meta: null }, NOW);
assert.equal(empty.length, 4);
assert.equal(empty[0].value, "—"); assert.equal(empty[0].sub, "symbols stored");
assert.equal(empty[1].value, "Never"); assert.equal(empty[1].tone, "warn");
assert.equal(empty[2].value, "Idle"); assert.equal(empty[2].tone, "flat");
assert.equal(empty[3].value, "—"); assert.equal(empty[3].sub, "no run yet");

// a real 0 stocks stored is a reading, not missing
assert.equal(feedKpis({ ...base, meta: { last_count: 0 } }, NOW)[0].value, "0");
// fall back to the job's ok_count, then the checkpoint list
assert.equal(feedKpis({ ...base, job: { ok_count: 7 } }, NOW)[0].value, "7");
assert.equal(feedKpis({ ...base, job: { checkpoint: { done: ["A", "B", "C"] } } }, NOW)[0].value, "3");
assert.equal(feedKpis({ ...base, meta: { last_count: 120, universe_size: 250 } }, NOW)[0].sub, "of 250 in the universe");

// fresh success: age + IST time, no warning
const fresh = feedKpis({ ...base, meta: { last_success_at: "2026-10-25T08:30:00Z" } }, NOW)[1];
assert.equal(fresh.value, "1.5h ago"); assert.equal(fresh.tone, "info"); assert.match(fresh.sub, /2026/);
// stale (older than 24h) is a warning in words
const stale = feedKpis({ ...base, meta: { last_success_at: "2026-10-23T08:30:00Z" } }, NOW)[1];
assert.equal(stale.value, "2.1d ago"); assert.equal(stale.tone, "warn"); assert.match(stale.sub, /^Stale - older than 24h/);
// unreadable date is never shown as a time
const badDate = feedKpis({ ...base, meta: { last_success_at: "not a date" } }, NOW)[1];
assert.equal(badDate.value, "—"); assert.equal(badDate.sub, "date unreadable"); assert.equal(badDate.tone, "warn");
// a clock a little ahead of the server reads "just now", never a negative age
assert.equal(feedAge(-5000), "just now"); assert.equal(feedAge(30_000), "just now"); assert.equal(feedAge(5 * 60_000), "5m ago");

// job states
assert.equal(feedKpis({ ...base, status: "running", job: { message: "feeding" } }, NOW)[2].tone, "info");
for (const s of ["error", "stalled", "stopped"]) assert.equal(feedKpis({ ...base, status: s }, NOW)[2].tone, "warn");
assert.equal(feedKpis({ ...base, status: "done" }, NOW)[2].value, "Done");
const long = feedKpis({ ...base, status: "running", job: { message: "x".repeat(100) } }, NOW)[2];
assert.equal(long.sub.length, 60); assert.ok(long.sub.endsWith("..."));

// progress: partial and stopped warns; running or complete does not
const part = feedKpis({ ...base, status: "stopped", processed: 40, total: 100, pct: 40, partial: true }, NOW)[3];
assert.equal(part.value, "40/100"); assert.equal(part.tone, "warn"); assert.match(part.sub, /partial, press Resume/);
const run = feedKpis({ ...base, status: "running", processed: 40, total: 100, pct: 40, partial: true }, NOW)[3];
assert.equal(run.tone, "flat"); assert.equal(run.sub, "40%");
const done = feedKpis({ ...base, status: "done", processed: 100, total: 100, pct: 100 }, NOW)[3];
assert.equal(done.value, "100/100"); assert.equal(done.tone, "flat");
// a processed count of 0 with a known total is shown, not hidden
assert.equal(feedKpis({ ...base, status: "running", processed: 0, total: 50, pct: 5 }, NOW)[3].value, "0/50");

// live quote feed tile only when the socket state exists
assert.equal(feedKpis({ ...base, wsFeed: null }, NOW).length, 4);
const up = feedKpis({ ...base, wsFeed: { connected: true, subscribed_count: 500, live_symbols_count: 0 } }, NOW)[4];
assert.equal(up.value, "0 ticking"); assert.equal(up.sub, "of 500 subscribed"); assert.equal(up.tone, "info");
const unk = feedKpis({ ...base, wsFeed: { connected: true } }, NOW)[4];
assert.equal(unk.value, "—"); assert.equal(unk.sub, "subscribed count unknown");
const down = feedKpis({ ...base, wsFeed: { connected: false } }, NOW)[4];
assert.equal(down.value, "Reconnecting"); assert.equal(down.tone, "warn"); assert.match(down.sub, /REST/);

for (const x of feedKpis({ ...base, meta: { last_count: 1 }, wsFeed: { connected: true } }, NOW)) assert.equal(typeof x.value, "string");
console.log("feedView ok");
