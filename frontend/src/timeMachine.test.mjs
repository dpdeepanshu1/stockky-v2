// Run: node --experimental-strip-types timeMachine.test.mjs  (Node >= 22.6). E111: the Time Machine tab's pure helpers.
import assert from "node:assert/strict";
import { defaultForm, parseSymbols, formProblem, buildBody, errorText, verdictView, pct, rText, toneOfR, histBars, gapLine, controlLine, MAX_COMPARE, gateModeView, gateTimerLine, gateCountsLine } from "./timeMachineView.ts";
import { readFileSync } from "node:fs";

const ok = (o = {}) => ({ ...defaultForm(null), symbol: "RELIANCE", ...o });

// defaults come from the server's status when it is there
assert.equal(defaultForm(null).entryTime, "10:00");
assert.equal(defaultForm(null).sims, "10000");
const st = { enabled: true, sims_default: 20000, slippage_pct: 0.05, lookback: "2mo", default_value_rs: 50000, defaults: { side: "SHORT", stop_pct: 0.8, target_pct: 1.6, entry_time: "11:15", flat_by_long: "15:00", flat_by_short: "14:55" }, max_compare_symbols: 15, recent: [] };
const d = defaultForm(st);
assert.deepEqual([d.side, d.stopPct, d.targetPct, d.entryTime, d.valueRs, d.sims, d.lookback], ["SHORT", "0.8", "1.6", "11:15", "50000", "20000", "2mo"]);

// symbols
assert.deepEqual(parseSymbols("reliance.ns, tcs ,TCS; m&m bajaj-auto  ../x"), ["RELIANCE", "TCS", "M&M", "BAJAJ-AUTO"]);
assert.deepEqual(parseSymbols(""), []);

// form validation mirrors the server limits
assert.equal(formProblem(ok(), "run"), null);
assert.match(formProblem(ok({ symbol: "" }), "run"), /one NSE symbol/);
assert.match(formProblem(ok({ symbol: "a b" }), "run"), /one NSE symbol/);
assert.match(formProblem(ok({ stopPct: "0" }), "run"), /Stop %/);
assert.match(formProblem(ok({ stopPct: "11" }), "run"), /Stop %/);
assert.match(formProblem(ok({ targetPct: "x" }), "run"), /Target %/);
assert.match(formProblem(ok({ entryTime: "9:5" }), "run"), /look like 10:00/);
assert.match(formProblem(ok({ entryTime: "10:07" }), "run"), /5-minute mark/);
assert.match(formProblem(ok({ entryTime: "09:10" }), "run"), /5-minute mark/);
assert.match(formProblem(ok({ entryTime: "15:00" }), "run"), /before the square-off/);
assert.equal(formProblem(ok({ entryTime: "14:50" }), "run"), null);
assert.match(formProblem(ok({ entryTime: "11:00", flatBy: "10:30" }), "run"), /before the flat-by/);
assert.match(formProblem(ok({ flatBy: "16:00" }), "run"), /Flat-by/);
assert.equal(formProblem(ok({ flatBy: "14:55" }), "run"), null);
assert.match(formProblem(ok({ valueRs: "10" }), "run"), /Trade value/);
assert.match(formProblem(ok({ sims: "500" }), "run"), /Made-up sessions/);
assert.match(formProblem(ok({ sims: "2000.5" }), "run"), /Made-up sessions/);
assert.match(formProblem(ok({ compareSymbols: "TCS" }), "compare"), /at least two/);
assert.equal(formProblem(ok({ compareSymbols: "TCS, INFY" }), "compare"), null);
assert.match(formProblem(ok({ compareSymbols: Array.from({ length: MAX_COMPARE + 1 }, (_, i) => `S${i}`).join(",") }), "compare"), /at most 15/);

// request bodies
const b = buildBody(ok({ symbol: " tcs.ns ", flatBy: "" }), "run");
assert.deepEqual(b, { side: "LONG", stop_pct: 1, target_pct: 2, entry_time: "10:00", value_rs: 20000, sims: 10000, lookback: "1mo", symbol: "TCS" });
assert.equal("flat_by" in b, false, "an empty flat-by lets the server pick 15:00 / 14:55");
const c = buildBody(ok({ compareSymbols: "tcs, infy", flatBy: "14:55", side: "SHORT" }), "compare");
assert.deepEqual(c.symbols, ["TCS", "INFY"]); assert.equal(c.flat_by, "14:55"); assert.equal(c.side, "SHORT"); assert.equal("symbol" in c, false);

// server errors
assert.equal(errorText(new Error('{"code":"BUSY","error":"two runs are in progress"}')), "two runs are in progress");
assert.equal(errorText(new Error("502 Bad Gateway: oops")), "502 Bad Gateway: oops");
assert.equal(errorText(null), "Time Machine failed");

// verdict, numbers
assert.equal(verdictView("FAVOURS").tone, "good"); assert.equal(verdictView("AGAINST").tone, "bad");
assert.equal(verdictView("MIXED").tone, "warn"); assert.equal(verdictView("NOT_ENOUGH_DATA").tone, "mute"); assert.equal(verdictView(undefined).label, "No verdict");
assert.equal(pct(0.3456), "35%"); assert.equal(pct(0.3456, 1), "34.6%"); assert.equal(pct(null), "-"); assert.equal(pct(NaN), "-");
assert.equal(rText(0.1234), "+0.12 R"); assert.equal(rText(-0.5), "-0.50 R"); assert.equal(rText(undefined), "-");
assert.equal(toneOfR(0.2), "good"); assert.equal(toneOfR(-0.2), "bad"); assert.equal(toneOfR(0.01), "warn"); assert.equal(toneOfR(null), "mute");

// histogram bars: widest bin = 100, stop side and target side are marked
const bars = histBars([{ lo: -1.25, hi: -1, n: 50, share: 0.5 }, { lo: -0.25, hi: 0, n: 10, share: 0.1 }, { lo: 1.75, hi: 2, n: 40, share: 0.4 }], 2);
assert.deepEqual(bars.map((x) => [x.widthPct, x.kind]), [[100, "stop"], [20, "mid"], [80, "target"]]);
assert.deepEqual(histBars([], 2), []); assert.deepEqual(histBars(undefined, 2), []);

// plain-language lines
assert.match(gapLine(null), /No real history/);
assert.match(gapLine(2), /within 2 points/);
assert.match(gapLine(12), /12 points more often/);
assert.match(gapLine(-9), /9 points less often/);
const doc = { control: { side: "SHORT", simulation: { n: 10, avg_r_net: 0.2 }, history: { n: 0 } }, simulation: { n: 10, avg_r_net: -0.1 }, params: {} };
assert.match(controlLine(doc), /drifting window/);
assert.match(controlLine({ ...doc, simulation: { n: 10, avg_r_net: 0.3 } }), /this side \+0.30 R/);
assert.equal(controlLine(null), "");

// wiring: the tab, the three client calls and the test script exist
const root = new URL(".", import.meta.url);
const api = readFileSync(new URL("positionStocksApi.ts", root), "utf8");
for (const route of ["/time-machine/status", "/time-machine/run", "/time-machine/compare"]) assert.ok(api.includes(route), route);
const tab = readFileSync(new URL("components/PositionStocksTab.tsx", root), "utf8");
assert.ok(tab.includes('"timemachine"') && tab.includes("TimeMachinePanel"), "tab wired");
assert.ok(readFileSync(new URL("../package.json", root), "utf8").includes("timeMachine.test.mjs"), "in npm test");
// E115: the live picking gate card
assert.equal(gateModeView("live").tone, "good"); assert.equal(gateModeView("log").tone, "warn"); assert.equal(gateModeView("off").tone, "mute"); assert.equal(gateModeView(undefined).label, "OFF");
const gd = { mode: "live", auto_enabled: true, timer: { prefetch: "09:05", first_scan: "09:16", last_scan: "14:30", every_min: 15, threads_running: true }, counters: { gate_checks: 20, rejects: 3, passes: 12, no_opinion: 5, would_reject_log: 0 }, refusal_rate: 0.15, cached_verdicts: 7, pending_jobs: 0, recent_checks: [], recent_scans: [] };
assert.ok(gateTimerLine(gd).includes("09:16") && gateTimerLine(gd).includes("15 min") && !gateTimerLine(gd).includes("not running"));
assert.ok(gateTimerLine({ ...gd, timer: { ...gd.timer, threads_running: false } }).includes("threads not running"));
assert.ok(gateTimerLine({ ...gd, auto_enabled: false }).startsWith("Timer off"));
assert.ok(gateCountsLine(gd).includes("20 checks") && gateCountsLine(gd).includes("3 refused (15%)") && gateCountsLine(gd).includes("7 verdicts"));
assert.equal(gateCountsLine(null), "-"); assert.ok(gateCountsLine({ ...gd, refusal_rate: null }).includes("(-)"));
for (const route of ["/time-machine/gate", "/time-machine/scan"]) assert.ok(api.includes(route), route);
const panel = readFileSync(new URL("components/TimeMachinePanel.tsx", root), "utf8");
assert.ok(panel.includes("timeMachineGate") && panel.includes("tm-gate-counts") && panel.includes("Scan now"), "gate card wired");
console.log("timeMachine.test.mjs: ok");
