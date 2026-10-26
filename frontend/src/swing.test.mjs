// Run: node --experimental-strip-types swing.test.mjs  (Node >= 22.6). E144: the "Swing lane" card helpers (swingView.ts).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { swingView, verdictView, ledgerLines, netTone, inr, SWING_MIN_TRADES, SWING_MIN_DAYS } from "./swingView.ts";

const stats = (o = {}) => ({ n: 0, days: 0, mean: null, median: null, win_pct: null, ci90: null, ...o });
const report = (o = {}) => ({ verdict: "NOT_ENOUGH_DATA", n: 0, days: 0, net: stats(), gross: stats(), reasons: ["0 closed trades on 0 entry days; needs 30 trades on 20 days"],
  stopped_pct: null, gap_through: 0, since: null, by_status: {}, kind: "SHADOW", note: "", ...o });
const doc = (o = {}) => ({ mode: "shadow", live_real_enabled: false, max_trade_value: 20000, max_positions: 2, max_hold_days: 5, universe_size: 118, universe_source: "built-in",
  scan: { day: "2026-10-23", ok: true, summary: "ranked 112, screen 9" }, scan_universe: "builtin118", runtime: { running: false, last_result: null },
  shadow: report(), pause: { DEMO: { paused: false, why: "" }, REAL: { paused: false, why: "" } }, open_positions: [], planned: [],
  wiring: { problems: [], modes: {} }, ...o });

// unavailable: no doc, a failure doc, a non-object
assert.equal(swingView(null).kind, "unavailable");
assert.equal(swingView(undefined).kind, "unavailable");
assert.equal(swingView("x").kind, "unavailable");
const failed = swingView({ error: "boom" });
assert.equal(failed.kind, "unavailable");
assert.match(failed.headline, /failed: boom/);

// shadow, nothing closed: the normal first weeks
const first = swingView(doc());
assert.equal(first.kind, "shadow");
assert.match(first.headline, /No order is placed/);
assert.equal(first.verdict.code, "NOT_ENOUGH_DATA");
assert.equal(first.verdict.tone, "warn");
assert.equal(first.progress.needed, SWING_MIN_TRADES);
assert.equal(first.progress.daysNeeded, SWING_MIN_DAYS);
assert.equal(first.progress.pct, 0);
assert.deepEqual(first.numbers, [], "no numbers when nothing closed");
assert.match(first.scan.text, /session: ranked 112, screen 9, stock list: builtin118\. Universe: 118 stocks \(built-in\)/);
assert.equal(first.scan.tone, undefined);

// off
const off = swingView(doc({ mode: "off" }));
assert.equal(off.kind, "off");
assert.match(off.headline, /SWING_MODE is off/);

// live: says what it may do, and that REAL is blocked until enabled
const live = swingView(doc({ mode: "live" }));
assert.equal(live.kind, "live");
assert.match(live.headline, /DEMO \(REAL stays blocked/);
assert.match(live.headline, /2 positions of ₹20,000 each, held up to 5 sessions/);
assert.match(swingView(doc({ mode: "live", live_real_enabled: true })).headline, /DEMO and REAL,/);

// numbers once trades closed; progress capped at 100; status counts biggest first, ties by name
const rich = swingView(doc({ shadow: report({ verdict: "NO_EDGE", n: 42, days: 25, net: stats({ n: 42, days: 25, mean: -0.12, median: -0.3, win_pct: 45.2, ci90: [-0.5, 0.2] }),
  gross: stats({ n: 42, days: 25, mean: 0.4 }), reasons: ["net mean per trade: the 90% interval does not stay above zero", "gross mean 0.4% is positive but costs eat it"], stopped_pct: 38.1, gap_through: 2,
  by_status: { CLOSED: 42, PLANNED: 3, OPEN: 3, SKIPPED: 0 } }) }));
assert.equal(rich.verdict.tone, "bad");
assert.match(rich.verdict.text, /^NO_EDGE: net mean.*; gross mean 0\.4% is positive but costs eat it$/);
assert.equal(rich.progress.pct, 100);
assert.deepEqual(rich.statusCounts, [{ status: "CLOSED", count: 42 }, { status: "OPEN", count: 3 }, { status: "PLANNED", count: 3 }]);
const nm = Object.fromEntries(rich.numbers.map((n) => [n.label, n.value]));
assert.equal(nm["net mean per trade"], "-0.12%");
assert.equal(nm["gross mean"], "+0.40%");
assert.equal(nm["win rate"], "45.2%");
assert.equal(nm["stopped out"], "38.1%");
assert.equal(nm["gapped through the stop"], "2");
assert.equal(nm["90% interval (net mean)"], "-0.50% to +0.20%");
// no interval yet -> says so instead of printing "- to -"
assert.equal(Object.fromEntries(swingView(doc({ shadow: report({ n: 3, days: 3, net: stats({ n: 3, days: 3, mean: 0.5, ci90: null }) }) })).numbers.map((n) => [n.label, n.value]))["90% interval (net mean)"], "needs more entry days");

// USEFUL is good but still tells a person to check the day spread
const useful = verdictView("USEFUL", []);
assert.equal(useful.tone, "good");
assert.match(useful.text, /one or two days/);
assert.equal(verdictView("???", null).tone, "warn");
assert.equal(verdictView(undefined, undefined).code, "UNKNOWN");

// a shadow report that itself failed must not print numbers or a verdict
const badRep = swingView(doc({ shadow: { error: "db down" } }));
assert.equal(badRep.verdict.code, "UNKNOWN");
assert.deepEqual(badRep.numbers, []);

// wiring problems pass through untouched; paused modes only
const prob = swingView(doc({ wiring: { problems: ["DEMO: the gate is not armed", 7], modes: {} }, pause: { DEMO: { paused: true, why: "3 losses in a row" }, REAL: { paused: false, why: "" } } }));
assert.deepEqual(prob.problems, ["DEMO: the gate is not armed"], "non-string problems dropped");
assert.deepEqual(prob.pauses, [{ mode: "DEMO", why: "3 losses in a row" }]);

// scan: none stored yet / failed scan
// E220: scan_universe is {source, size} from the backend: never "[object Object]"
const objSame = swingView(doc({ scan_universe: { source: "env", size: 744 }, universe_size: 744, universe_source: "env" }));
assert.doesNotMatch(objSame.scan.text, /object Object/);
assert.doesNotMatch(objSame.scan.text, /stock list/);
const objDiff = swingView(doc({ scan_universe: { source: "builtin", size: 118 }, universe_size: 744, universe_source: "env" }));
assert.match(objDiff.scan.text, /stock list used: 118 stocks \(builtin\)/);
assert.doesNotMatch(objDiff.scan.text, /object Object/);
const noscan = swingView(doc({ scan: { day: null, ok: null, summary: null }, scan_universe: null }));
assert.equal(noscan.scan.tone, "warn");
assert.match(noscan.scan.text, /No evening scan stored yet/);
assert.equal(swingView(doc({ scan: { day: "2026-10-23", ok: false, summary: "no data" } })).scan.tone, "bad");

// open positions and planned picks are worded, missing numbers become "-"
const pos = swingView(doc({ open_positions: [{ mode: "DEMO", symbol: "AAA", qty: 12, entry: 101.5, stop: 96, opened_at: "x" }, { mode: "REAL", symbol: "BBB", qty: null, entry: null, stop: null, opened_at: "y" }],
  planned: [{ symbol: "CCC", rank: 2, signal_day: "2026-10-23", stop_pct: 3.456 }, { symbol: "DDD", rank: null, signal_day: null, stop_pct: null }] }));
assert.deepEqual(pos.openPositions[0], { mode: "DEMO", symbol: "AAA", qty: "12", entry: "101.50", stop: "96.00" });
assert.deepEqual(pos.openPositions[1], { mode: "REAL", symbol: "BBB", qty: "-", entry: "-", stop: "-" });
assert.deepEqual(pos.planned[0], { symbol: "CCC", rank: "2", signalDay: "2026-10-23", stopPct: "3.5%" });
assert.deepEqual(pos.planned[1], { symbol: "DDD", rank: "-", signalDay: "-", stopPct: "-" });

// malformed pieces never throw
assert.doesNotThrow(() => swingView({ mode: "shadow", shadow: null, pause: null, open_positions: null, planned: null, wiring: null, scan: null }));
assert.equal(swingView({ mode: "shadow" }).kind, "shadow");

// ledger table lines
const lines = ledgerLines([
  { id: 3, symbol: "AAA", status: "CLOSED", signal_day: "2026-10-20", entry_day: "2026-10-21", exit_day: "2026-10-23", exit_reason: "stop", skip_reason: null, net_pct: -2.4 },
  { id: 2, symbol: "BBB", status: "SKIPPED", signal_day: "2026-10-20", entry_day: null, exit_day: null, exit_reason: null, skip_reason: "gap_up", net_pct: null },
  { id: 1, symbol: "CCC", status: "CLOSED", signal_day: "2026-10-19", entry_day: "2026-10-20", exit_day: "2026-10-24", exit_reason: "time", skip_reason: null, net_pct: 1.234 },
]);
assert.deepEqual(lines.map((l) => [l.symbol, l.why, l.net, l.netTone]), [["AAA", "stop", "-2.40%", "bad"], ["BBB", "gap_up", "-", undefined], ["CCC", "time", "+1.23%", "good"]]);
assert.deepEqual(ledgerLines(null), []);
assert.equal(netTone(0), undefined);
assert.equal(inr(20000), "₹20,000");
assert.equal(inr(null), "-");

// CONTRACT with the backend: every key the TypeScript type promises is a key swing_status() / swing.engine really writes, so the page cannot read a field that never arrives.
const root = new URL("../../", import.meta.url);
const main = readFileSync(new URL("services/real-trade-service/main.py", root), "utf8");
const status = main.slice(main.indexOf('async def swing_status'), main.indexOf('async def swing_ledger'));
for (const k of ["mode", "live_real_enabled", "max_trade_value", "max_positions", "max_hold_days", "universe_size", "universe_source", "scan", "scan_universe", "runtime", "shadow", "pause", "open_positions", "planned", "wiring"]) {
  assert.ok(status.includes(`"${k}"`), `swing_status no longer sends "${k}"`);
}
const api = readFileSync(new URL("frontend/src/realTradeApi.ts", root), "utf8");
const iface = api.slice(api.indexOf("export interface SwingStatusDoc"), api.indexOf("export interface SwingLedgerDoc"));
for (const k of ["mode", "live_real_enabled", "universe_size", "scan", "shadow", "pause", "open_positions", "planned", "wiring"]) {
  assert.match(iface, new RegExp(`\\b${k}\\b`), `SwingStatusDoc lost "${k}"`);
}
const eng = readFileSync(new URL("services/real-trade-service/swing/engine.py", root), "utf8");
const stt = readFileSync(new URL("services/real-trade-service/swing/stats.py", root), "utf8");
for (const k of ["by_status", "kind", "note", "since"]) assert.ok(eng.includes(`out["${k}"]`), `engine.report no longer sets ${k}`);
for (const k of ["verdict", "reasons", "stopped_pct", "gap_through"]) assert.ok(stt.includes(`out["${k}"]`), `stats.verdict no longer sets ${k}`);

// WIRING: the page shows the card, polls with the shared visible-interval helper, never posts, and the tab has the section.
const tab = readFileSync(new URL("frontend/src/components/CalibrationControlTab.tsx", root), "utf8");
assert.match(tab, /section === "swing" && <SwingPanel \/>/);
assert.match(tab, /\{ id: "swing", label: "Swing lane" \}/);
const panel = readFileSync(new URL("frontend/src/components/calibration/SwingPanel.tsx", root), "utf8");
assert.match(panel, /setVisibleInterval/);
assert.ok(!/method:\s*"(POST|PUT|DELETE)"/.test(panel) && !/swingResume|swingProbe|swingScan/.test(panel), "the swing card is read-only");
assert.ok(!/pct_rank|top \d+%/i.test(panel + readFileSync(new URL("frontend/src/swingView.ts", root), "utf8").replace(/\/\/.*$/gm, "")), "no 'top X%' label: what pct_rank means is not confirmed");
assert.ok(!/swingResume|swing\/resume|swing\/probe|swing\/scan/.test(api.replace(/\/\/.*$/gm, "")), "no write call to the swing lane in the client");

const pipe = readFileSync(new URL("frontend/src/components/realAuto/tabs/PipelineTab.tsx", root), "utf8");
assert.match(pipe, /The swing lane also needs this ON/, "the EOD square-off switch text must say the swing lane needs it");
const lv = readFileSync(new URL("services/real-trade-service/swing/live.py", root), "utf8");
assert.match(lv, /EOD square-off switch is off/, "the backend wiring check still reports a missing EOD switch");

console.log("swing.test.mjs: all assertions passed");
