// Run: node --experimental-strip-types components/board/flowGeometry.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { isNarrow, RAIL_W, laneWidth, busX, railRowHeight, sectorY, cubic, flowPath, busY, pointAt } from "./flowGeometry.ts";
import { ghostItems, mulberry32 } from "./ghostLayout.ts";
import { stripState, heat, convictionDots, istClock, sectorSpark, tweenInt } from "./boardState.ts";
import { squarify } from "./treemapLayout.ts";

// narrow layout is decided from the whole board width (no rail/stage flip-flop)
assert.equal(isNarrow(0), false); assert.equal(isNarrow(400), true); assert.equal(isNarrow(RAIL_W + 519), true); assert.equal(isNarrow(RAIL_W + 520), false);
// lane
assert.equal(laneWidth(1000, true), 0);
assert.equal(laneWidth(0, false), 0);
assert.equal(laneWidth(300, false), 90);
assert.equal(laneWidth(800, false), 160);
assert.equal(laneWidth(3000, false), 220);
assert.equal(busX(160), 88);

// rail rows: natural height when it fits, shrinks (never below 14) when many sectors
assert.equal(railRowHeight(10, 640), 26);
assert.ok(railRowHeight(40, 640) < 26 && railRowHeight(40, 640) >= 14);
assert.equal(railRowHeight(0, 640), 26);

// sector origin sits in its own rail row (row 0 is "All sectors") and stays inside the stage
const H = 640;
for (const n of [3, 12, 30, 60]) {
  let prev = -1;
  for (let i = 0; i < n; i++) {
    const y = sectorY(i, n, H);
    assert.ok(y >= prev, "rows go down");
    assert.ok(y > 0 && y <= H - 6, `inside stage n=${n} i=${i} y=${y}`);
    prev = y;
  }
}
assert.equal(sectorY(-1, 5, H), H / 2);
assert.equal(sectorY(9, 5, H), H / 2);

// bezier + path
const p = { x: 0, y: 0 }, q = { x: 10, y: 0 }, r = { x: 10, y: 10 }, s = { x: 20, y: 10 };
assert.deepEqual(cubic(p, q, r, s, 0), p);
assert.deepEqual(cubic(p, q, r, s, 1), s);
const src = { x: 0, y: 40 }, bus = { x: 90, y: 300 }, dst = { x: 400, y: 120 };
const path = flowPath(src, bus, dst, 28);
assert.equal(path.length, 29);
assert.deepEqual(path[0], src);
assert.deepEqual(path[path.length - 1], dst);
assert.ok(path.some(pt => Math.abs(pt.x - bus.x) < 1e-9 && Math.abs(pt.y - bus.y) < 1e-9), "passes through the bus");
for (let i = 1; i < path.length; i++) assert.ok(path[i].x >= path[i - 1].x - 1e-9, "x never goes backwards");
assert.equal(flowPath(src, bus, dst, 3).length, 5);          // tiny step counts still give a valid path

// bus pulls toward the centre
assert.equal(busY(H / 2, H), H / 2);
assert.ok(Math.abs(busY(0, H) - H / 2) < Math.abs(0 - H / 2));

// pointAt
assert.deepEqual(pointAt(path, 0), path[0]);
assert.deepEqual(pointAt(path, 1), path[path.length - 1]);
assert.deepEqual(pointAt(path, -5), path[0]);
assert.deepEqual(pointAt(path, 9), path[path.length - 1]);
assert.deepEqual(pointAt([], 0.5), { x: 0, y: 0 });
assert.deepEqual(pointAt([{ x: 0, y: 0 }, { x: 10, y: 20 }], 0.5), { x: 5, y: 10 });

// ghost: deterministic, positive, tiles the box
assert.deepEqual(ghostItems(30, 7), ghostItems(30, 7));
assert.notDeepEqual(ghostItems(30, 7), ghostItems(30, 8));
assert.equal(ghostItems(0, 1).length, 0);
assert.equal(ghostItems(-3, 1).length, 0);
assert.ok(ghostItems(50, 3).every(g => g.value > 0));
const a = mulberry32(5), b = mulberry32(5);
assert.equal(a(), b());
const rects = squarify(ghostItems(36, 11), 100, 0, 600, 300);
assert.equal(rects.length, 36);
let area = 0; for (const t of rects) area += t.w * t.h;
assert.ok(Math.abs(area - 600 * 300) < 1e-6);
assert.ok(rects.every(t => t.x >= 100 - 1e-9 && t.x + t.w <= 700 + 1e-9));

// header pill states
const feed = (o = {}) => ({ source: "angelone_ws", connected: true, last_tick_at: 1, tick_age_s: 2, ...o });
assert.deepEqual(stripState({ stale: false, market_open: true, feed: feed() }), { label: "LIVE", kind: "live" });
assert.equal(stripState({ stale: false, market_open: true, feed: feed({ tick_age_s: 45 }) }).kind, "stale");
assert.equal(stripState({ stale: false, market_open: true, feed: feed({ connected: false }) }).kind, "stale");
assert.equal(stripState({ stale: false, market_open: false, feed: feed({ tick_age_s: 999, connected: false }) }).kind, "closed");
assert.deepEqual(stripState({ stale: true, stale_day: "2026-10-09", market_open: false, feed: feed() }), { label: "LAST SESSION 2026-10-09", kind: "last" });
assert.equal(stripState({ stale: true, market_open: false, feed: feed() }).label, "LAST SESSION");

// heat / conviction dots / IST clock
assert.equal(heat(100), 1); assert.equal(heat(0), 0); assert.equal(heat(250), 1); assert.equal(heat(-5), 0);
assert.equal(heat(null), 0.5); assert.equal(heat(NaN), 0.5); assert.equal(heat(undefined), 0.5);
assert.equal(convictionDots(97), 5); assert.equal(convictionDots(0), 0); assert.equal(convictionDots(50), 3);
assert.equal(convictionDots(null), 0); assert.equal(convictionDots(400), 5); assert.equal(convictionDots(-9), 0);
assert.equal(istClock(Date.UTC(2026, 9, 9, 3, 45) / 1000), "09:15");   // 03:45 UTC = 09:15 IST, seconds
assert.equal(istClock(Date.UTC(2026, 9, 9, 3, 45)), "09:15");          // same instant, milliseconds
assert.equal(istClock(0), ""); assert.equal(istClock(NaN), "");
// sector composite spark + count-up tween
const rowsS = [{ sector: "IT", spark: [100, 101, 102, 104] }, { sector: "IT", spark: [50, 50, 49, 48] }, { sector: "IT", spark: null }, { sector: "Banks", spark: [10, 11] }, { sector: "IT", spark: [0, 1, 2] }];
const sp = sectorSpark(rowsS, "IT", 4);
assert.equal(sp.length, 4); assert.ok(Math.abs(sp[0]) < 1e-9);
assert.ok(Math.abs(sp[3] - ((4 + -4) / 2)) < 1e-9, "mean of +4% and -4% = 0");
assert.deepEqual(sectorSpark(rowsS, "Nope"), []); assert.deepEqual(sectorSpark(rowsS, null), []); assert.deepEqual(sectorSpark([], "IT"), []);
assert.deepEqual(sectorSpark(rowsS, "IT", 1), []);
assert.equal(tweenInt(0, 100, 0), 0); assert.equal(tweenInt(0, 100, 500), 100); assert.equal(tweenInt(0, 100, 9999), 100);
assert.ok(tweenInt(0, 100, 250) > 50 && tweenInt(0, 100, 250) < 100); assert.equal(tweenInt(5, 9, 10, 0), 9);
console.log("flowGeometry OK");
