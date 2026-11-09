// Run: node --experimental-strip-types fnoView.test.mjs  (Node >= 22.6). Pure helpers of the F&O pages.
import assert from "node:assert/strict";
import { istDay, istHm, inr, signed, lockSummary, positionProgress, dayTotals, tradesCsv, CSV_HEAD, btLabel, pcrReading, splitEvent } from "./fnoView.ts";

// 2026-11-09 03:50 UTC = 09:20 IST; 2026-11-09 19:00 UTC = next IST day 00:30
assert.equal(istDay(Date.UTC(2026, 10, 9, 3, 50) / 1000), "2026-11-09");
assert.equal(istHm(Date.UTC(2026, 10, 9, 3, 50) / 1000), "09:20");
assert.equal(istDay(Date.UTC(2026, 10, 9, 19, 0) / 1000), "2026-11-10");
assert.equal(istDay(null), "-");
assert.equal(inr(-1234.5), "-₹1,235"); assert.equal(inr(null), "-"); assert.equal(signed(1.234), "+1.23"); assert.equal(signed(-2, 0), "-2");

const L = (o) => ({ engine_enabled: true, mode: "DEMO", real_enabled: false, armed_today: false, broker_configured: false, real_orders_possible: false, closed_locks: [], ...o });
assert.equal(lockSummary(null).tone, "off");
assert.equal(lockSummary(L({ engine_enabled: false })).tone, "off");
assert.equal(lockSummary(L({})).tone, "demo");
assert.equal(lockSummary(L({ mode: "REAL", closed_locks: ["ARM"] })).tone, "real-blocked");
assert.match(lockSummary(L({ mode: "REAL", closed_locks: ["ARM"] })).text, /ARM/);
assert.equal(lockSummary(L({ mode: "REAL", real_orders_possible: true })).tone, "real");

const P = (o) => ({ id: 1, underlying: "NIFTY", expiry: "2026-11-10", strike: 24500, opt_type: "CE", lot_size: 65, lots: 2, entry_ts: Date.UTC(2026, 10, 9, 4, 0) / 1000, entry_premium: 100,
  sl_price: 75, target_price: 150, mode: "DEMO", status: "CLOSED", max_premium: 0, trail_armed: false, trail_stop: null, exit_ts: Date.UTC(2026, 10, 9, 5, 0) / 1000, exit_premium: 120,
  exit_reason: "TARGET", gross_pnl: 2600, charges: 100, net_pnl: 2500, signal_score: 50, spot_at_entry: 24500, note: "", label: "x", ...o });
assert.deepEqual(positionProgress(P({ premium: 112.5 })), { pct: 50, pnl: 1625 });
assert.equal(positionProgress(P({ premium: 60 })).pct, 0);
assert.equal(positionProgress(P({ premium: 400 })).pct, 100);
assert.equal(positionProgress(P({ premium: null, unrealized: 7 })).pnl, 7);

const d = dayTotals([P({}), P({ id: 2, net_pnl: -400 }), P({ id: 3, status: "OPEN" }), P({ id: 4, net_pnl: null, gross_pnl: 50 })]);
assert.equal(d.length, 1); assert.equal(d[0].trades, 3); assert.equal(d[0].wins, 2); assert.equal(d[0].net, 2150);
const csv = tradesCsv([P({})]).split("\n");
assert.equal(csv[0], CSV_HEAD); assert.equal(csv[1].split(",").length, CSV_HEAD.split(",").length);
assert.equal(btLabel("fno_sim").label, "SIMULATED"); assert.equal(btLabel("SIM").label, "SIMULATED"); assert.equal(btLabel("REPLAY").label, "RECORDED"); assert.equal(btLabel("EOD").label, "COARSE");
assert.equal(btLabel("ROLLING").label, "REAL PREMIUMS"); assert.equal(btLabel("fno_rolling").label, "REAL PREMIUMS");   // round 253: a ROLLING result used to show as UNKNOWN
assert.equal(btLabel("zzz").label, "UNKNOWN"); assert.equal(btLabel("fno_replay").label, "RECORDED");
assert.match(pcrReading(1.5), /bullish/); assert.match(pcrReading(0.5), /bearish/); assert.equal(pcrReading(1), "balanced"); assert.equal(pcrReading(null), "-");
console.log("fnoView.test.mjs: all passed");

// round 236: the Signals page shows the strategy as its own column; the rest of the event stays as detail
const sp = splitEvent({ id: 9, ts: 1, kind: "veto", underlying: "NIFTY", strategy: "MOMENTUM_SURGE", direction: "UP", chain: -30 });
assert.equal(sp.strategy, "MOMENTUM_SURGE"); assert.deepEqual(sp.rest, { direction: "UP", chain: -30 });
assert.equal(splitEvent({ id: 1, ts: 1, kind: "signal", underlying: "NIFTY", score: 40 }).strategy, "");
assert.deepEqual(splitEvent({ id: 1, ts: 1, kind: "signal", underlying: "NIFTY", score: 40 }).rest, { score: 40 });
assert.equal(splitEvent(null).strategy, "");
console.log("fnoView round 236 ok");
