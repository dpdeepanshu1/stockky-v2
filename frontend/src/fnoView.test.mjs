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

// round 262, slice 3: readiness checklist helpers
import { readinessChip, readinessGroups, readinessHeadline, confirmQuestion } from "./fnoView.ts";
assert.deepEqual(readinessChip(true), { tone: "good", text: "DONE" });
assert.deepEqual(readinessChip(false), { tone: "bad", text: "NOT MET" });
assert.equal(readinessChip(null).text, "NOT SHOWN YET"); assert.equal(readinessChip(undefined).tone, "warn");   // unknown is never shown as done
const RI = (key, group, ok, o = {}) => ({ key, group, label: key, ok, hard: true, detail: "", ...o });
const grp = readinessGroups([RI("a", "DEMO evidence", true), RI("b", "Dhan", null), RI("c", "DEMO evidence", false)]);
assert.deepEqual(grp.map((g) => g.group), ["DEMO evidence", "Dhan"]); assert.deepEqual(grp[0].items.map((i) => i.key), ["a", "c"]);
assert.deepEqual(readinessGroups(null), []); assert.deepEqual(readinessGroups(undefined), []);
const RD = (o) => ({ ts: 1, items: [], blockers: [], all_green: false, counts: { green: 0, red: 0, unknown: 0, hard_open: 3 }, real_waits_for_it: true, locks: L({}), thresholds: {}, ...o });
assert.equal(readinessHeadline(null).tone, "off");
assert.match(readinessHeadline(RD({})).text, /3 hard items are not done yet: REAL orders stay closed/);
assert.match(readinessHeadline(RD({ counts: { green: 5, red: 0, unknown: 1, hard_open: 1 } })).text, /1 hard item is not done/);
assert.match(readinessHeadline(RD({ real_waits_for_it: false })).text, /only advises/);
const ok0 = readinessHeadline(RD({ all_green: true, counts: { green: 9, red: 0, unknown: 0, hard_open: 0 } }));
assert.equal(ok0.tone, "good"); assert.match(ok0.text, /not a promise of profit/); assert.ok(!/safe/i.test(ok0.text));
assert.match(confirmQuestion(RI("dhan_static_ip", "x", null, { label: "Dhan static IP is whitelisted" }), true), /checked this yourself/);
assert.match(confirmQuestion(RI("k", "x", true, { label: "L" }), false), /Withdraw/);
console.log("fnoView round 262 slice 3 ok");

// ---- Round 264, slice 2: pipeline stages ----
import { stageChip, stageAge, firstProblem, stageHeadline } from "./fnoView.ts";
const SG = (key, status, over = {}) => ({ key, label: key.toUpperCase(), status, detail: `${key} detail`, age_s: null, last_decision: null, count_today: null, ...over });
assert.deepEqual(stageChip("OK"), { tone: "good", text: "OK" });
assert.deepEqual(stageChip("PROBLEM"), { tone: "bad", text: "PROBLEM" });
assert.deepEqual(stageChip("idle"), { tone: "neutral", text: "idle" });       // idle is never green
assert.deepEqual(stageChip(undefined), { tone: "neutral", text: "idle" });
assert.equal(stageAge(null), "no run yet"); assert.equal(stageAge(NaN), "no run yet");
assert.equal(stageAge(20), "20 s ago"); assert.equal(stageAge(300), "5 min ago"); assert.equal(stageAge(7200), "2 h ago"); assert.equal(stageAge(-5), "0 s ago");
assert.equal(firstProblem(null), null); assert.equal(firstProblem([SG("a", "OK"), SG("b", "idle")]), null);
assert.equal(firstProblem([SG("a", "OK"), SG("b", "PROBLEM"), SG("c", "PROBLEM")]).key, "b");     // the earliest stage in the flow
assert.equal(stageHeadline(null).tone, "off"); assert.equal(stageHeadline([]).tone, "off");
const bad = stageHeadline([SG("a", "OK"), SG("b", "PROBLEM", { label: "Chains", detail: "no chain" }), SG("c", "PROBLEM")]);
assert.equal(bad.tone, "bad"); assert.match(bad.text, /2 stages with a problem; start with Chains: no chain/);
assert.match(stageHeadline([SG("a", "PROBLEM", { label: "X", detail: "d" })]).text, /^1 stage with a problem/);
const good = stageHeadline([SG("a", "OK"), SG("b", "idle")]);
assert.equal(good.tone, "good"); assert.match(good.text, /1 OK, 1 idle/); assert.match(good.text, /not that it works/); assert.ok(!/safe/i.test(good.text));
console.log("fnoView round 264 slice 2 ok");

// ---- Round 265: probe result and charges check ----
import { probeVerdictChip, parseNoteInputs, probeHeadline, PROBE_LINES } from "./fnoView.ts";
assert.equal(PROBE_LINES.length, 7);
assert.deepEqual(probeVerdictChip("MATCHED"), { tone: "good", text: "MATCHED" });
assert.deepEqual(probeVerdictChip("DIFFERS"), { tone: "bad", text: "DIFFERS" });
assert.equal(probeVerdictChip("INCOMPLETE").tone, "warn"); assert.equal(probeVerdictChip("NO_NOTE").text, "NO NOTE YET");
assert.equal(probeVerdictChip("NOT_COMPARABLE").tone, "neutral"); assert.equal(probeVerdictChip(undefined).tone, "neutral");
assert.deepEqual(parseNoteInputs({ brokerage: " 40 ", stt: "Rs 1,234.50", exchange: "", sebi: "₹0.01" }), { lines: { brokerage: 40, stt: 1234.5, sebi: 0.01 }, errors: [] });
const bad2 = parseNoteInputs({ brokerage: "forty", stt: "-3", gst: "8.2" });
assert.deepEqual(bad2.lines, { gst: 8.2 }); assert.equal(bad2.errors.length, 2); assert.match(bad2.errors[0], /Brokerage/);
assert.deepEqual(parseNoteInputs({}), { lines: {}, errors: [] });
const PD = (o = {}, probe = {}) => ({ dir: "/d", dir_exists: true, files: [], note: null, comparison: null, probe: { file: "f", live: true, underlying: "NIFTY", side: "CE", contract: "NIFTY 24500 CE", exit_code: 0, outcome: "round trip done", orders: [], dhan_hint: "", pnl: null, still_open: false, dhan_net_position_after: 0, ...probe }, ...o });
assert.equal(probeHeadline(null).tone, "off");
assert.match(probeHeadline(PD({ probe: null })).text, /No probe result yet/);
assert.match(probeHeadline(PD({ probe: null, dir_exists: false })).text, /mkdir -p data\/fno_real_probe/);
assert.equal(probeHeadline(PD({}, { still_open: true, exit_code: 3 })).tone, "bad"); assert.match(probeHeadline(PD({}, { still_open: true })).text, /NIFTY 24500 CE/);
assert.match(probeHeadline(PD({}, { live: false })).text, /dry run/);
// Round 269: a refused run says why (the reason is saved in the result), a good run does not
assert.match(probeHeadline(PD({}, { exit_code: 2, outcome: "refused, nothing is open", reason: "REFUSED: outside the live window 09:30-14:45 IST on a weekday." })).text, /^Probe: refused, nothing is open\. REFUSED: outside the live window/);
assert.equal(probeHeadline(PD({}, { exit_code: 0, outcome: "round trip done", reason: "stale" })).text, "Probe: round trip done."); assert.equal(probeHeadline(PD({}, { exit_code: 2, outcome: "refused" })).text, "Probe: refused.");
assert.equal(probeHeadline(PD({}, {})).tone, "good"); assert.equal(probeHeadline(PD({}, { exit_code: 2, outcome: "refused" })).tone, "warn");
console.log("fnoView round 265 ok");

// Round 289: an exit SELL in doubt is shown, with what to look for in the Dhan app.
import { exitDoubtView, exitDoubtCount } from "./fnoView.ts";
const Q = (o) => P({ status: "OPEN", label: "NIFTY 24500 CE", ...o });
assert.equal(exitDoubtView(Q({})), null);
assert.equal(exitDoubtView(Q({ exit_doubt: false, broker_order_ids: { entry: "1" } })), null);
const dv = exitDoubtView(Q({ exit_doubt: true, broker_order_ids: { exit_doubt: "stky-77", exit_doubt_reason: "SL", exit_doubt_px: "74.5" } }));
assert.equal(dv.correlationId, "stky-77"); assert.equal(dv.reason, "SL"); assert.equal(dv.limitPx, 74.5);
assert.match(dv.text, /correlationId stky-77/); assert.match(dv.text, /limit 74\.50/); assert.match(dv.text, /NOT there/);
const dq = exitDoubtView(Q({ exit_doubt: true, broker_order_ids: { exit_doubt: "?" } }));       // mark without detail: still shown, no invented id or price
assert.equal(dq.limitPx, null); assert.doesNotMatch(dq.text, /correlationId/); assert.doesNotMatch(dq.text, /limit /);
assert.ok(exitDoubtView(Q({ broker_order_ids: { exit_doubt: "x" } })));                          // the ids alone are enough
assert.equal(exitDoubtCount([Q({}), Q({ exit_doubt: true, broker_order_ids: { exit_doubt: "a" } }), Q({ exit_doubt: true, broker_order_ids: { exit_doubt: "b" } })]), 2);
assert.equal(exitDoubtCount(null), 0); assert.equal(exitDoubtCount([]), 0);
console.log("fnoView round 289 ok");

// Round 290: settings that cannot work together are shown, not only logged.
import { settingsProblemText } from "./fnoView.ts";
assert.equal(settingsProblemText(undefined), null); assert.equal(settingsProblemText(null), null); assert.equal(settingsProblemText([]), null); assert.equal(settingsProblemText(["", "  "]), null);
const spText = settingsProblemText(["last entry 15:20 must be before the square-off 15:15 (FNO_ENTRY_END / FNO_SQUAREOFF_AT)", "stop loss 150 % must be between 0 and 100 (FNO_SL_PCT)"]);
assert.match(spText, /refuses every new entry/); assert.match(spText, /FNO_ENTRY_END/); assert.match(spText, /FNO_SL_PCT/); assert.match(spText, /;/);
console.log("fnoView round 290 ok");

// Round 291: the same warning on Real Money Calibration > F&O (source checks: components are not rendered here)
import { readFileSync as readSrc291 } from "node:fs";
const calSrc = readSrc291(new URL("./components/calibration/FnoCalibration.tsx", import.meta.url), "utf8");
const warnSrc = readSrc291(new URL("./components/fno/FnoSettingsWarning.tsx", import.meta.url), "utf8");
assert.match(calSrc, /import FnoSettingsWarning from "\.\.\/fno\/FnoSettingsWarning"/); assert.match(calSrc, /<FnoSettingsWarning \/>/);
assert.match(warnSrc, /settingsProblemText\(ov\.data\?\.param_problems\)/); assert.match(warnSrc, /if \(!text\) return null/); assert.match(warnSrc, /fnoApi\.overview/);
console.log("fnoView round 291 ok");

// Round 292: a stop order in doubt is shown with its correlationId; the exit-doubt and stop-doubt views are independent.
import { stopDoubtView, stopDoubtCount } from "./fnoView.ts";
assert.equal(stopDoubtView(Q({})), null);
assert.equal(stopDoubtView(Q({ broker_order_ids: { sl: "5" } })), null);
const sdv = stopDoubtView(Q({ stop_doubt: true, broker_order_ids: { sl_doubt: "sl1a2b", sl_doubt_ts: "1789000000.0" } }));
assert.equal(sdv.correlationId, "sl1a2b"); assert.match(sdv.text, /correlationId sl1a2b/); assert.match(sdv.text, /no second stop/); assert.match(sdv.text, /only if none is there/);
const sdq = stopDoubtView(Q({ stop_doubt: true }));                                              // flag without detail: shown, no invented id
assert.equal(sdq.correlationId, "?"); assert.doesNotMatch(sdq.text, /correlationId/);
assert.ok(stopDoubtView(Q({ broker_order_ids: { sl_doubt: "x" } })));
assert.equal(stopDoubtCount([Q({}), Q({ stop_doubt: true }), Q({ broker_order_ids: { sl_doubt: "x" } })]), 2); assert.equal(stopDoubtCount(null), 0);
assert.equal(exitDoubtView(Q({ stop_doubt: true, broker_order_ids: { sl_doubt: "x" } })), null);   // a stop in doubt is not an exit in doubt
console.log("fnoView round 292 ok");

// Round 293: an entry whose fill is unknown is shown with its correlationId; it is independent of the exit-doubt and stop-doubt views.
import { entryDoubtView, entryDoubtCount } from "./fnoView.ts";
assert.equal(entryDoubtView(Q({})), null);
assert.equal(entryDoubtView(Q({ broker_order_ids: { entry: "5" } })), null);
const edv = entryDoubtView(Q({ broker_order_ids: { entry_state: "UNKNOWN", entry_cid: "bu1a2b" } }));
assert.equal(edv.correlationId, "bu1a2b"); assert.match(edv.text, /correlationId bu1a2b/); assert.match(edv.text, /no second BUY/); assert.match(edv.text, /no stop/);
const edq = entryDoubtView(Q({ broker_order_ids: { entry_state: "UNKNOWN", entry: "9" } }));       // an older unknown entry (order id known, fill unknown): shown, no invented correlationId
assert.equal(edq.correlationId, null); assert.doesNotMatch(edq.text, /correlationId/);
assert.equal(entryDoubtCount([Q({}), Q({ broker_order_ids: { entry_state: "UNKNOWN" } }), Q({ broker_order_ids: { entry_state: "UNKNOWN", entry_cid: "x" } })]), 2); assert.equal(entryDoubtCount(null), 0);
assert.equal(stopDoubtView(Q({ broker_order_ids: { entry_state: "UNKNOWN" } })), null); assert.equal(exitDoubtView(Q({ broker_order_ids: { entry_state: "UNKNOWN" } })), null);
console.log("fnoView round 293 ok");
