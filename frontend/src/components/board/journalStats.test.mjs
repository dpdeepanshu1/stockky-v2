// Run: node --experimental-strip-types components/board/journalStats.test.mjs
import assert from "node:assert/strict";
import { istDay, byDay, byTag } from "./journalStats.ts";

assert.equal(istDay("2026-10-12T20:00:00"), "2026-10-13");          // 20:00 UTC = 01:30 IST next day
assert.equal(istDay("2026-10-12T03:00:00+00:00"), "2026-10-12");
assert.equal(istDay("garbage"), "");
const t = [
  { realized_pnl: 100, window_source: "5m", closed_at: "2026-10-12T05:00:00", opened_at: "x", status: "CLOSED" },
  { realized_pnl: -40, window_source: "5m", closed_at: "2026-10-12T06:00:00", opened_at: "x", status: "CLOSED" },
  { realized_pnl: 10, window_source: "1m", closed_at: null, opened_at: "2026-10-13T05:00:00", status: "CLOSED" },
  { realized_pnl: null, window_source: "1m", closed_at: null, opened_at: "2026-10-13T05:00:00", status: "OPEN" },
];
const d = byDay(t);
assert.deepEqual(d.map(x => [x.day, x.pnl, x.trades, x.wins]), [["2026-10-12", 60, 2, 1], ["2026-10-13", 10, 1, 1]]);
const g = byTag(t);
assert.equal(g[0].tag, "5m"); assert.equal(g[0].win_rate, 0.5); assert.equal(g[1].trades, 1);
console.log("journalStats ok");
