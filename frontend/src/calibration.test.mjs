// Run: node --experimental-strip-types calibration.test.mjs  (Node >= 22.6). group 442: pure helpers of the Real Money Calibration page.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { tradesFromRealTrade, tradesFromPositionStocks, dayTotals, bucketBy, tradesCsv, istDay, istHm, relTime, fmtDur, isUnbooked, rMultiple, directionalPct, CSV_HEAD } from "./calibrationTrades.ts";
import { PLAN, JOB_SERVICES, jobAppliesTo, GATE_GLOSSARY, planRunAll } from "./calibrationPlan.ts";

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

// E213: the CSV carries what a loss analysis needs; the first 10 columns are unchanged
{
  assert.equal(CSV_HEAD.slice(0, 10).join(","), "day_ist,service,symbol,entry_ist,exit_ist,held_min,pnl,return_pct,reason,qty");
  assert.equal(CSV_HEAD.length, 22);
  assert.equal(isUnbooked({ net_pnl: 0, return_pct: null }), true);
  assert.equal(isUnbooked({ net_pnl: 0, return_pct: 0 }), false);
  assert.equal(isUnbooked({ net_pnl: -3.2 }), false);
  const g = tradesFromRealTrade({ records: [
    { position_id: 7, symbol: "HDFCLIFE", opened_at: "2026-10-19T07:40:00Z", closed_at: "2026-10-20T03:27:00Z", net_pnl: 0, gross_pnl: 0, return_pct: null, exit_reason: "eod_squareoff", quantity: 5 },
    { position_id: 8, symbol: "NYKAA", opened_at: "2026-10-21T03:46:00Z", closed_at: "2026-10-21T06:11:00Z", net_pnl: -71.72, return_pct: -3.514, exit_reason: "stop_hit", quantity: 6,
      entry_price: 340.1, exit_price: 328.2, stop_pct: 2.5, max_gain_pct: 0.4, max_drawdown_pct: -3.6, entry_slippage_pct: 0.12, exit_slippage_pct: -0.9, charges: 3.1, stop_basis: "atr" } ] });
  assert.equal(g.length, 1);
  assert.equal(Math.round(rMultiple(g[0]) * 100) / 100, -1.41);
  const cols = tradesCsv(g).trim().split("\n")[1].split(",");
  assert.equal(cols.length, 22);
  assert.equal(cols[15], "-1.41");
  // position-stocks: long and short, same raw numbers
  const p = tradesFromPositionStocks({ trades: [
    { id: 1, symbol: "L", status: "STOP_HIT", exit_reason: "stop_hit", side: "BUY", entry_price: 100, exit_price: 98.8, realized_pnl: -6, quantity: 5, opened_at: "2026-10-26T04:50:00Z", closed_at: "2026-10-26T04:55:00Z",
      adaptive_stop_pct: 1, adaptive_target_pct: 2, max_gain_pct: 0.3, max_drawdown_pct: -1.2, signal_price: 99.9, stop_price: 99 },
    { id: 2, symbol: "S", status: "STOP_HIT", exit_reason: "stop_hit", side: "SHORT", entry_price: 100, exit_price: 101.2, realized_pnl: -6, quantity: 5, opened_at: "2026-10-26T04:50:00Z", closed_at: "2026-10-26T04:55:00Z",
      adaptive_stop_pct: 1, max_gain_pct: 1.2, max_drawdown_pct: -0.3, signal_price: 100.1, stop_price: 101 } ] });
  const r6 = (x) => Math.round(x * 1e6) / 1e6;
  assert.equal(r6(p[0].retPct), -1.2); assert.equal(r6(p[1].retPct), -1.2, "a short that rose 1.2 % lost 1.2 %");
  assert.equal(r6(rMultiple(p[0])), -1.2);
  assert.equal(r6(p[1].maxGainPct), 0.3); assert.equal(r6(p[1].maxDrawdownPct), -1.2, "short: best = the low, worst = the high");
  assert.equal(Math.round(p[0].entrySlipPct * 100) / 100, 0.1); assert.equal(Math.round(p[1].entrySlipPct * 100) / 100, 0.1, "worse fill = positive for both sides");
  assert.ok(p[0].exitSlipPct < 0 && p[1].exitSlipPct < 0, "both stops filled through the stop price");
  assert.equal(directionalPct("LONG", 0, 5), null);
  // E215: position-stocks charges come from /trades/history; a row without the field stays blank, never NaN
  const pc = tradesFromPositionStocks({ trades: [
    { id: 3, symbol: "C", status: "STOP_HIT", exit_reason: "stop_hit", side: "BUY", entry_price: 100, exit_price: 99, realized_pnl: -5, quantity: 5, opened_at: "2026-10-26T04:50:00Z", closed_at: "2026-10-26T04:55:00Z", charges: 3.42 },
    { id: 4, symbol: "D", status: "STOP_HIT", exit_reason: "stop_hit", side: "BUY", entry_price: 100, exit_price: 99, realized_pnl: -5, quantity: 5, opened_at: "2026-10-26T04:50:00Z", closed_at: "2026-10-26T04:55:00Z" } ] });
  assert.equal(pc[0].charges, 3.42); assert.equal(pc[1].charges, null);
  assert.ok(tradesCsv(pc).split("\n")[1].includes(",3.42,"));
  assert.ok(tradesCsv(p).split("\n").every((l) => !l.includes("undefined") && !l.includes("NaN")));
}
assert.equal(relTime(null), "never");
assert.equal(relTime("2026-10-19T00:00:00Z", Date.parse("2026-10-19T00:00:30Z")), "30s ago");
assert.equal(fmtDur(30), "30 s");
assert.equal(fmtDur(7200), "2.0 h");

// the plan only names jobs / packs that exist in the runner's catalog (jobs.py)
const jobsPy = readFileSync(new URL("../../services/calibration-runner-service/jobs.py", import.meta.url), "utf8");
const jobIds = new Set([...jobsPy.matchAll(/_job\("([a-z0-9_]+)"/g)].map((m) => m[1]));
const packIds = new Set([...jobsPy.matchAll(/\{"id": "([a-z0-9_]+)", "title": "[^"]+", "when"/g)].map((m) => m[1]));
assert.ok(jobIds.size >= 30 && packIds.size === 11, `${jobIds.size} jobs ${packIds.size} packs`);
for (const sec of PLAN) for (const row of sec.rows) {
  if (row.pack) assert.ok(packIds.has(row.pack), `unknown pack ${row.pack}`);
  for (const j of row.jobs || []) assert.ok(jobIds.has(j), `unknown job ${j}`);
}
for (const id of Object.keys(JOB_SERVICES)) assert.ok(jobIds.has(id), `JOB_SERVICES names unknown job ${id}`);
assert.equal(jobAppliesTo("opening_replay", "rt"), false);
assert.equal(jobAppliesTo("opening_replay", "ps"), true);
assert.equal(jobAppliesTo("control_snapshot", "rt"), true);
assert.equal(GATE_GLOSSARY.filter((g) => !g.exists).length, 1);
// group 466: Resume. The runner marks the jobs whose scripts save each finished symbol; the page offers Resume only for those and sends resume=true.
const resumable = [...jobsPy.matchAll(/_job\("([a-z0-9_]+)"[\s\S]*?(?=\n    _job\("|\n\]\nJOBS)/g)].filter((m) => /resumable=True/.test(m[0])).map((m) => m[1]).sort();
assert.deepEqual(resumable, ["ab_report", "calibration_full", "entry_rules", "legacy_gate", "ml_v2", "p8_retest", "structure_backtest", "swing_backtest"]);   // E142: swing_backtest saves each finished symbol (E131)
assert.equal(jobAppliesTo("swing_backtest", "ps"), false);
assert.equal(jobAppliesTo("swing_shadow_report", "ps"), false);
assert.equal(jobAppliesTo("swing_universe", "rt"), true);
const api = readFileSync(new URL("./calibrationApi.ts", import.meta.url), "utf8");
assert.match(api, /resume = false\) =>\s+R<RunRec>\("\/runs", \{ method: "POST", body: JSON\.stringify\(\{ kind, target, params, confirm, resume \}\) \}\)/);
assert.match(api, /resumable\?: boolean/);
const panel = readFileSync(new URL("./components/calibration/RunsPanel.tsx", import.meta.url), "utf8");
assert.match(panel, /isResumable\(r, catalog\)/);
assert.match(panel, /start\(r\.kind, r\.target, r\.params, true\)/);
assert.match(panel, /Resume \(reuse saved symbols\)/);
const ctxSrc = readFileSync(new URL("./components/calibration/RunnerContext.tsx", import.meta.url), "utf8");
assert.match(ctxSrc, /runnerApi\.start\(run\.kind, run\.target, params, true, resume\)/, "the market-hours confirm must keep the resume flag");

// group 471: "Run all test cases" plan: never the data-changing "model" group, heavy only on request, a job after the jobs it needs, per-service filter
const J = (id, group, heavy = false, needs = []) => ({ id, group, heavy, needs });
const cat = [J("records_report", "weekly", false, ["export_records"]), J("export_records", "daily"), J("train_model", "model", true), J("fetch_history", "weekly", true),
  J("ab_report", "weekly", true, ["fetch_history"]), J("opening_replay", "weekly"), J("services_health", "ops")];
const light = planRunAll(cat, "rt", false);
assert.deepEqual(light.ids, ["export_records", "records_report", "services_health"], "needs first, no model group, no heavy, ps-only job left out of rt");
assert.deepEqual(light.heavySkipped.sort(), ["ab_report", "fetch_history"]);
const full = planRunAll(cat, "ps", true);
assert.ok(full.ids.indexOf("fetch_history") < full.ids.indexOf("ab_report") && !full.ids.includes("train_model") && full.ids.includes("opening_replay"));
assert.equal(new Set(full.ids).size, full.ids.length);
// the real catalog: the planner picks no model-group job and every job it picks exists
const realGroups = Object.fromEntries([...jobsPy.matchAll(/_job\("([a-z0-9_]+)", "[^"]*", "([a-z]+)"/g)].map((m) => [m[1], m[2]]));
const realCat = Object.entries(realGroups).map(([id, group]) => J(id, group));
for (const svc of ["rt", "ps"]) { const r = planRunAll(realCat, svc, true); assert.ok(r.ids.length > 10 && r.ids.every((id) => realGroups[id] !== "model")); }
// UI wiring
const bar = readFileSync(new URL("./components/calibration/RunAllBar.tsx", import.meta.url), "utf8");
assert.match(bar, /Run all test cases/);
const tab = readFileSync(new URL("./components/CalibrationControlTab.tsx", import.meta.url), "utf8");
assert.match(tab, /section !== \"runs\" && section !== \"alerts\" && section !== \"weather\" && section !== \"swing\" && section !== \"reconcile\" && section !== \"probes\" && <RunAllBar svc=\{svc\} \/>/, "Run all shows on every tab except Runs & downloads, the scheduler Alert switches (E51), Market Weather (not per service) and the three read-only status sections added in E144 (Swing lane, Reconciliation, Status probes: they start no test)");
// E51: the scheduler's alert switches go through the gateway proxy, never straight to the scheduler
assert.match(tab, /section === \"alerts\" && <SwitchesPanel svc=\"sch\" \/>/);
assert.match(api, /`\/scheduler-control\$\{path\}`/, "alert switches use the gateway proxy path");
assert.match(api, /export type SwitchSvc = Svc \| "sch"/);
assert.match(panel, /Download all latest results/);
assert.match(api, /\/results\/latest\.zip/);
assert.match(ctxSrc, /for \(let i = 0; i < ids\.length && !stopAll\.current/, "tests are started one after the other");
// E231: position-stocks' realized_pnl is before charges; the Trades CSV / totals subtract the row's own charges, and a stagnation exit carries which rule fired
{
  const n = tradesFromPositionStocks({ trades: [
    { id: 1, symbol: "AAA", status: "STAGNATION_EXIT", entry_price: 100, exit_price: 100.1, realized_pnl: 2.52, charges: 2.74, quantity: 21, exit_rule: "no_followthrough", opened_at: "2026-10-26T06:20:00Z", closed_at: "2026-10-26T06:51:00Z" },
    { id: 2, symbol: "BBB", status: "TARGET_HIT", entry_price: 100, exit_price: 101, realized_pnl: 10, charges: null, quantity: 10, opened_at: "2026-10-26T06:20:00Z", closed_at: "2026-10-26T06:51:00Z" },
    { id: 3, symbol: "CCC", status: "STAGNATION_EXIT", entry_price: 100, exit_price: 100, realized_pnl: 0, charges: 2, quantity: 10, opened_at: "2026-10-26T06:20:00Z", closed_at: "2026-10-26T06:51:00Z" },
  ] });
  const by = Object.fromEntries(n.map((r) => [r.symbol, r]));
  assert.equal(by.AAA.pnl, -0.22, "a gross win smaller than its charges is a small loss");
  assert.equal(by.AAA.reason, "stagnation_exit:no_followthrough");
  assert.equal(by.BBB.pnl, 10, "no charges known: left as is");
  assert.equal(by.CCC.reason, "stagnation_exit", "an older row without a rule keeps the plain reason");
}
console.log("calibration.test.mjs: ok");
