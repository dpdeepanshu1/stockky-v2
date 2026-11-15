// Run: node --experimental-strip-types src/rowSpark.test.mjs   (E557, round 539 B3)
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { sparkKey, wantedSymbols, sparkFor, loadSparklines, resetSparklines, sparkPercents, sparkSummary, SPARK_BATCH, SPARK_TTL_MS, SPARK_RETRY_MS, SPARK_MAX_WANTED } from "./components/rowSpark.ts";

// names are cleaned and de-duplicated, order kept, empties dropped, capped
assert.equal(sparkKey(" tcs.NS "), "TCS");
assert.deepEqual(wantedSymbols(["tcs", "TCS", "", null, undefined, "m&m", "INFY.ns"]), ["TCS", "M&M", "INFY"]);
assert.equal(wantedSymbols(Array.from({ length: 500 }, (_, i) => "S" + i)).length, SPARK_MAX_WANTED);

// percent maths: starts at 0, ends at the whole-period change; bad input draws nothing
assert.deepEqual(sparkPercents([100, 110, 90]), [0, 10, -10]);
assert.equal(sparkPercents([100]), null); assert.equal(sparkPercents(null), null); assert.equal(sparkPercents([0, 5]), null); assert.equal(sparkPercents([NaN, 5]), null);
assert.match(sparkSummary("TCS", [100, 104.2]), /^TCS: \+4\.2% over the last 2 sessions, from 100\.00 to 104\.20$/);
assert.match(sparkSummary("TCS", [100, 95]), /-5\.0%/);
assert.equal(sparkSummary("X", null), "X: no recent price line");

// loading: one batched read, only for names that are neither fresh nor in flight; a name without history is remembered as "none"
let now = 1_000_000; const clock = () => now;
resetSparklines();
const calls = [];
const good = [100, 101, 102, 103, 104];
const fetcher = async (names) => { calls.push(names); return { series: Object.fromEntries(names.filter((n) => n !== "NONE").map((n) => [n, good])) }; };
assert.equal(await loadSparklines(["tcs", "INFY", "NONE"], fetcher, clock), true);
assert.deepEqual(calls, [["TCS", "INFY", "NONE"]]);
assert.deepEqual(sparkFor("TCS", now), good); assert.equal(sparkFor("NONE", now), null);
await loadSparklines(["TCS", "NONE", "INFY"], fetcher, clock);
assert.equal(calls.length, 1, "fresh names (and a remembered miss) are not asked again");
now += SPARK_TTL_MS + 1;
assert.equal(sparkFor("TCS", now), null, "stale after 30 minutes");
await loadSparklines(["TCS"], fetcher, clock);
assert.equal(calls.length, 2);

// batches of 40; a bad series (non-finite, too short) is dropped
resetSparklines(); calls.length = 0;
await loadSparklines(Array.from({ length: 90 }, (_, i) => "S" + i), fetcher, clock);
assert.deepEqual(calls.map((c) => c.length), [SPARK_BATCH, SPARK_BATCH, 10]);
resetSparklines();
await loadSparklines(["A", "B", "C"], async () => ({ series: { A: [1], B: [1, NaN, 3], C: [1, 2] } }), clock);
assert.deepEqual([sparkFor("A", now), sparkFor("B", now), sparkFor("C", now)], [null, null, [1, 2]]);

// a failed read never throws, leaves no line, and is retried only after two minutes
resetSparklines(); let n = 0;
const flaky = async () => { n++; throw new Error("down"); };
assert.equal(await loadSparklines(["TCS"], flaky, clock), true);
assert.equal(sparkFor("TCS", now), null);
await loadSparklines(["TCS"], flaky, clock); assert.equal(n, 1, "not asked again at once");
now += SPARK_RETRY_MS + 1;
await loadSparklines(["TCS"], async () => ({ series: { TCS: good } }), clock);
assert.deepEqual(sparkFor("TCS", now), good);

// in flight: a second call while the first is waiting asks for nothing
resetSparklines(); let release; const gate = new Promise((r) => (release = r)); let reads = 0;
const slow = async (names) => { reads++; await gate; return { series: {} }; };
const first = loadSparklines(["TCS"], slow, clock);
assert.equal(await loadSparklines(["TCS"], slow, clock), false);
release(); await first; assert.equal(reads, 1);

// wiring
const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
assert.match(rd("./api.ts"), /marketSparklines:[\s\S]*\/market\/sparklines\?symbols=/);
assert.match(rd("./components/HotStocks.tsx"), /useSparklines\(items\.map[\s\S]*spark=\{sparkOf\(item\.symbol\)\}/);
assert.match(rd("./components/ConvictionCard.tsx"), /spark \? <div className="mk-cc-trend">/);
assert.match(rd("./components/SurpriseStocks.tsx"), /key: "trend", header: "30d"[\s\S]*surpriseColumns\(sparkOf\)/);
assert.match(rd("./components/IpoTracker.tsx"), /<RowSpark symbol=\{ipo\.symbol\} closes=\{sparkOf\(ipo\.symbol\)\} \/>/);
assert.ok(!/\/market\/sparklines/.test(rd("./gatewayAuth.ts")), "a public read: no admin token is attached");
const css = rd("./styles/markets.css");
assert.ok(/\.mk-rowspark\s*\{/.test(css) && /\.mk-cc-trend\s*\{/.test(css));
console.log("rowSpark.test.mjs OK");
