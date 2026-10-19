// Run: node --experimental-strip-types calibration.test.mjs  (Node >= 22.6). group 442: pure helpers of the Real Money Calibration page.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { tradesFromRealTrade, tradesFromPositionStocks, dayTotals, bucketBy, tradesCsv, istDay, istHm, relTime, fmtDur } from "./calibrationTrades.ts";
import { PLAN, JOB_SERVICES, jobAppliesTo, GATE_GLOSSARY } from "./calibrationPlan.ts";

// real-trade records: UTC timestamps -> IST day / time, net P&L preferred, bad rows dropped
const rt = tradesFromRealTrade({ records: [
  { position_id: 1, symbol: "AAA", opened_at: "2026-10-19T03:50:00+00:00", closed_at: "2026-10-19T05:20:00+00:00", net_pnl: 120.5, gross_pnl: 150, return_pct: 1.2, exit_reason: "target_hit", quantity: 10 },
  { position_id: 2, symbol: "BBB", opened_at: "2026-10-19 04:10:00", closed_at: "2026-10-19 04:40:00", gross_pnl: -80, exit_reason: "stop_hit" },
  { position_id: 3, symbol: "CCC", opened_at: "bad", closed_at: "2026-10-19T05:00:00Z", net_pnl: 5 },
  { position_id: 4, symbol: "DDD", opened_at: "2026-10-19T04:00:00Z", closed_at: "2026-10-19T05:00:00Z" },
] });
assert.equal(rt.length, 2);
assert.equal(rt[0].pnl, 120.5);
assert.equal(istHm(rt[0].entry), "09:20");
assert.equal(istHm(rt[0].exit), "10:50");
assert.equal(rt[1].pnl, -80);
assert.equal(rt[1].day, "2026-10-19");
assert.equal(Math.round(rt[0].heldMin), 90);

// a close after 18:30 UTC belongs to the NEXT IST day
const late = tradesFromRealTrade([{ position_id: 9, symbol: "Z", opened_at: "2026-10-19T17:00:00Z", closed_at: "2026-10-19T19:00:00Z", net_pnl: 1 }]);
assert.equal(late[0].day, "2026-10-20");
assert.equal(istDay(new Date("2026-10-19T18:29:00Z")), "2026-10-19");

// position stocks: only settled rows with exit price and realised P&L
const ps = tradesFromPositionStocks({ trades: [
  { id: 1, symbol: "P1", status: "CLOSED", entry_price: 100, exit_price: 101, realized_pnl: 50, opened_at: "2026-10-19T04:00:00Z", closed_at: "2026-10-19T04:10:00Z", exit_reason: "target" },
  { id: 2, symbol: "P2", status: "ERROR", entry_price: 100, exit_price: 99, realized_pnl: -5, opened_at: "2026-10-19T04:00:00Z", closed_at: "2026-10-19T04:10:00Z" },
  { id: 3, symbol: "P3", status: "OPEN", opened_at: "2026-10-19T04:00:00Z" },
  { id: 4, symbol: "P4", status: "CLOSED", entry_price: 100, exit_price: null, realized_pnl: null, opened_at: "2026-10-19T04:00:00Z", closed_at: "2026-10-19T04:10:00Z" },
] });
assert.equal(ps.length, 1);
assert.equal(ps[0].retPct, 1);
assert.equal(ps[0].service, "position-stocks");

const all = [...rt, ...ps];
const t = dayTotals(all);
assert.equal(t.length, 1);
assert.equal(t[0].n, 3);
assert.equal(t[0].wins, 2);
assert.equal(t[0].losses, 1);
assert.equal(Math.round(t[0].net * 10) / 10, 90.5);
assert.equal(t[0].firstEntry, "09:20");
assert.equal(t[0].lastExit, "10:50");
const hours = bucketBy(all, (r) => istHm(r.entry).slice(0, 2));
assert.deepEqual(hours.map((b) => [b.key, b.n]), [["09", 3]]);

const csv = tradesCsv(all).trim().split("\n");
assert.equal(csv.length, 4);
assert.match(csv[0], /^day_ist,service,symbol,entry_ist,exit_ist/);
assert.ok(csv.every((l) => !l.includes("undefined")));

assert.equal(relTime(null), "never");
assert.equal(relTime("2026-10-19T00:00:00Z", Date.parse("2026-10-19T00:00:30Z")), "30s ago");
assert.equal(fmtDur(30), "30 s");
assert.equal(fmtDur(7200), "2.0 h");

// the plan only names jobs / packs that exist in the runner's catalog (jobs.py)
const jobsPy = readFileSync(new URL("../../services/calibration-runner-service/jobs.py", import.meta.url), "utf8");
const jobIds = new Set([...jobsPy.matchAll(/_job\("([a-z0-9_]+)"/g)].map((m) => m[1]));
const packIds = new Set([...jobsPy.matchAll(/\{"id": "([a-z_]+)", "title": "[^"]+", "when"/g)].map((m) => m[1]));
assert.ok(jobIds.size >= 30 && packIds.size === 5, `${jobIds.size} jobs ${packIds.size} packs`);
for (const sec of PLAN) for (const row of sec.rows) {
  if (row.pack) assert.ok(packIds.has(row.pack), `unknown pack ${row.pack}`);
  for (const j of row.jobs || []) assert.ok(jobIds.has(j), `unknown job ${j}`);
}
for (const id of Object.keys(JOB_SERVICES)) assert.ok(jobIds.has(id), `JOB_SERVICES names unknown job ${id}`);
assert.equal(jobAppliesTo("opening_replay", "rt"), false);
assert.equal(jobAppliesTo("opening_replay", "ps"), true);
assert.equal(jobAppliesTo("control_snapshot", "rt"), true);
assert.equal(GATE_GLOSSARY.filter((g) => !g.exists).length, 1);
console.log("calibration.test.mjs: ok");
