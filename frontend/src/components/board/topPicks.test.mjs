// Run: node --experimental-strip-types src/components/board/topPicks.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { normalizePicks, pickBadge, sparkPath, levelPosition } from "./topPicksView.ts";

const v = normalizePicks({
  as_of: 1000, market_open: true, engine_fresh: true, engine_picks: 1, preview_picks: 1,
  picks: [
    { pick: 1, symbol: "EICHERMOT", name: "Eicher Motors", sector: "Auto", side: "bull", source: "engine", status: "SELECTED", outcome: "ENTERED",
      score: 88.2, conviction: 97, rank: 1, ltp: 7050, change_pct: 4.17, zone_score: 80, entry: 7050, stop: 7001.45, target: 7150, rr: 2.05, spark: [1, 2, "x", 3], reasons: ["board conviction 97", 5] },
    { symbol: "TVSMOTOR", source: "weird", ltp: "n/a", spark: null },
    { symbol: "", ltp: 1 }, null,
  ],
});
assert.equal(v.picks.length, 2); assert.equal(v.asOf, 1_000_000); assert.equal(v.enginePicks, 1);
assert.deepEqual(v.picks[0].spark, [1, 2, 3]); assert.deepEqual(v.picks[0].reasons, ["board conviction 97"]);
assert.equal(v.picks[0].changePct, 4.17); assert.equal(v.picks[0].stop, 7001.45);
assert.equal(v.picks[1].pick, 2); assert.equal(v.picks[1].ltp, null); assert.equal(v.picks[1].source, "board"); assert.equal(v.picks[1].name, "TVSMOTOR");
assert.equal(normalizePicks(null).picks.length, 0); assert.equal(normalizePicks({ picks: "x" }).picks.length, 0);
assert.equal(normalizePicks({ note: "Market closed" }).note, "Market closed");

assert.deepEqual(pickBadge({ source: "engine", status: "SELECTED", outcome: "ENTERED" }), { label: "Entered", tone: "good" });
assert.deepEqual(pickBadge({ source: "engine", status: "SHORTLISTED", outcome: "SKIPPED:VWAP_GATE" }), { label: "Skipped · vwap gate", tone: "bad" });
assert.equal(pickBadge({ source: "engine", status: "SELECTED", outcome: "PLACEMENT_FAILED:x" }).tone, "bad");
assert.deepEqual(pickBadge({ source: "board", status: "PREVIEW", outcome: null }), { label: "Board preview", tone: "preview" });
assert.equal(pickBadge({ source: "engine", status: "SELECTED", outcome: null }).label, "First in line");
assert.equal(pickBadge({ source: "engine", status: "RESERVE", outcome: "NOT_NEEDED" }).label, "Not needed");

assert.equal(sparkPath([1], 10, 10), ""); assert.equal(sparkPath([], 10, 10), "");
assert.equal(sparkPath([0, 10], 100, 20), "0.0,19.0 100.0,1.0");
assert.equal(sparkPath([5, 5, 5], 100, 20).split(" ").length, 3);                 // flat series does not divide by zero

assert.equal(levelPosition(90, 95, 110), 0.25); assert.equal(levelPosition(90, 80, 110), 0); assert.equal(levelPosition(90, 120, 110), 1);
assert.equal(levelPosition(null, 95, 110), null); assert.equal(levelPosition(110, 95, 100), null);
console.log("topPicks tests passed");
