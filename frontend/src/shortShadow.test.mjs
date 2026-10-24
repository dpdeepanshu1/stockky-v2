// Run: node --experimental-strip-types shortShadow.test.mjs  (Node >= 22.6). Short selling step 6: the "Short selling (shadow)" card helpers.
import assert from "node:assert/strict";
import { sellProbeView, shortShadowView, statusTone, rupees, S2_DAYS_NEEDED, shortSwitchRows, canSwitchShortOff } from "./shortShadowView.ts";
import { readFileSync } from "node:fs";

const row = (o = {}) => ({ id: 1, day: "2026-10-23", symbol: "AAA", window_source: "5m", pct_change: -1.2, entry_price: 100, stop_price: 102, target_price: 96, quantity: 10,
  status: "TARGET", exit_price: 96, gross_pnl: 40, net_pnl: 31, net_pnl_pct: 3.1, low_water: 96, high_water: 100.5, board_conviction: 62, market_state: "stressed", created_at: "2026-10-23T05:00:00+00:00", ...o });
const doc = (o = {}) => ({ mode: "shadow", total: 0, open: 0, closed: 0, target: 0, stop: 0, eod: 0, expired: 0, net_pnl: 0, gross_pnl: 0, win_rate_pct: null, days_with_results: 0,
  s2_progress: { would_be_shorts: 0, needed: 100, days: 0, weeks_needed: 4 }, gate_counters: { by_code: {} }, recent: [], ...o });

// unavailable
assert.equal(shortShadowView(null).kind, "unavailable");
assert.equal(shortShadowView(undefined).s2Ready, false);

// off: mode off and nothing recorded -> tells where to switch it on, S2 is not ready
const off = shortShadowView(doc({ mode: "off" }));
assert.equal(off.kind, "off");
assert.match(off.headline, /SHORT_MODE is off/);
assert.equal(off.s2Ready, false);

// waiting: shadow on, no row yet; refusals sorted biggest first and capped at 6
const waiting = shortShadowView(doc({ gate_counters: { by_code: { SHORT_VWAP: 4, SHORT_NEAR_LOW: 9, A: 1, B: 1, C: 1, D: 1, E: 1, ZERO: 0 } } }));
assert.equal(waiting.kind, "waiting");
assert.deepEqual(waiting.refusals.map((r) => r.code), ["SHORT_NEAR_LOW", "SHORT_VWAP", "A", "B", "C", "D"], "biggest first, ties by code, zero counts dropped, at most 6");
assert.match(waiting.headline, /fail closed/);

// active but short of the S2 gate: says exactly what is missing
const a = shortShadowView(doc({ total: 12, closed: 10, open: 2, net_pnl: -85.5, days_with_results: 3, s2_progress: { would_be_shorts: 10, needed: 100, days: 3, weeks_needed: 4 }, recent: [row(), row({ id: 2, status: "STOP", net_pnl: -40 })] }));
assert.equal(a.kind, "active");
assert.equal(a.s2Ready, false);
assert.equal(a.progress.pct, 10);
assert.equal(a.s2Missing.length, 3);
assert.match(a.s2Missing[0], /90 more would-be shorts \(10 of 100\)/);
assert.match(a.s2Missing[1], new RegExp(`${S2_DAYS_NEEDED - 3} more trading days`));
assert.match(a.s2Missing[2], /negative/);
assert.equal(a.rows.length, 2);
assert.match(a.headline, /^12 would-be shorts recorded, 10 closed, 2 open\.$/);
assert.match(shortShadowView(doc({ total: 1, closed: 1 })).headline, /^1 would-be short recorded/);

// the three numbers pass -> ready (a person still checks it is not from one or two days: the card says so)
const ready = shortShadowView(doc({ total: 130, closed: 120, net_pnl: 2100, days_with_results: 21, s2_progress: { would_be_shorts: 120, needed: 100, days: 21, weeks_needed: 4 } }));
assert.equal(ready.s2Ready, true);
assert.deepEqual(ready.s2Missing, []);
assert.equal(ready.progress.pct, 100);
// 100 shorts and positive but too few days is NOT ready; zero net is not positive
assert.equal(shortShadowView(doc({ total: 100, closed: 100, net_pnl: 500, days_with_results: 5, s2_progress: { would_be_shorts: 100, needed: 100, days: 5, weeks_needed: 4 } })).s2Ready, false);
assert.match(shortShadowView(doc({ total: 100, closed: 100, net_pnl: 0, days_with_results: 25, s2_progress: { would_be_shorts: 100, needed: 100, days: 25, weeks_needed: 4 } })).s2Missing[0], /zero/);

// SHORT_MODE switched back to off with rows left over: still active, and says so
assert.match(shortShadowView(doc({ mode: "off", total: 4, closed: 4 })).headline, /SHORT_MODE is off now/);

// malformed numbers never throw
assert.equal(shortShadowView(doc({ s2_progress: {}, closed: 7, days_with_results: 2, total: 7 })).progress.done, 7);
assert.equal(shortShadowView({ mode: "shadow", total: 0 }).kind, "waiting");

// tones and money
assert.equal(statusTone("TARGET"), "good");
assert.equal(statusTone("STOP"), "bad");
assert.equal(statusTone("OPEN"), "warn");
assert.equal(statusTone("EXPIRED"), undefined);
assert.equal(rupees(1234.6), "+₹1,235");
assert.equal(rupees(-40.2), "-₹40");
assert.equal(rupees(0), "₹0");
assert.equal(rupees(null), "-");
assert.equal(rupees(NaN), "-");

console.log("shortShadow.test.mjs: ok");

// item 7a: the Short selling panel on the calibration page
const fl = (key, group, eff, o = {}) => ({ key, title: key.toLowerCase(), group, effective: eff, running: eff, recommended: null, ...o });
const rows = shortSwitchRows([fl("SHORT_RISK_MULT", "Short selling", "0.5"), fl("MARKET_STATE_MODE", "Market Weather", "live"), fl("SHORT_MODE", "Short selling", "shadow", { recommended: "shadow", pending_restart: true }), fl("SHORT_MAX_POSITIONS", "Short selling", "2")]);
assert.deepEqual(rows.map((r) => r.key), ["SHORT_MODE", "SHORT_MAX_POSITIONS", "SHORT_RISK_MULT"]);      // only the group, SHORT_MODE first
assert.equal(rows[0].pending, true);
assert.equal(canSwitchShortOff(rows), true);
assert.equal(canSwitchShortOff(shortSwitchRows([fl("SHORT_MODE", "Short selling", "off")])), false);   // already off: nothing to switch
assert.deepEqual(shortSwitchRows(null), []);
const sp = readFileSync(new URL("./components/calibration/ShortSellingPanel.tsx", import.meta.url), "utf8");
const spCode = sp.replace(/^\s*\/\/.*$/gm, "");
assert.match(sp, /data-testid="short-selling"/);
assert.match(sp, /controlApi\.set\("ps", "SHORT_MODE", "off"/);          // the one-click action can only set OFF
assert.ok(!/"SHORT_MODE", "(live|shadow)"/.test(spCode), "the panel never turns shorting on");
assert.match(sp, /start\("job", "short_shadow_report"/);
const tab = readFileSync(new URL("./components/CalibrationControlTab.tsx", import.meta.url), "utf8");
assert.match(tab, /section === "short" && <ShortSellingPanel \/>/);
console.log("shortShadow panel: ok");

// E108 (T6): the Dhan SELL probe line on the panel
assert.equal(sellProbeView(null).tone, "warn");
assert.match(sellProbeView({ ran: false, verdict: "NOT_RUN", line: "x", passed: false, required_for_live: true }).text, /refused until it passes/);
assert.match(sellProbeView({ ran: false, verdict: "NOT_RUN", line: "x", passed: false, required_for_live: false }).text, /not required/);
const okp = sellProbeView({ ran: true, verdict: "PASS", line: "SELL PROBE: PASS - clean round trip on ITC", symbol: "ITC", passed: true, required_for_live: true });
assert.equal(okp.tone, "good"); assert.match(okp.text, /allowed by this check/);
const bad = sellProbeView({ ran: true, verdict: "FAIL", line: "SELL PROBE: FAIL - Dhan refused the SELL Super Order", passed: false, required_for_live: true });
assert.equal(bad.tone, "bad"); assert.match(bad.text, /refused until the probe passes/);
assert.match(sp, /data-testid="short-probe"/);
assert.match(sp, /start\("job", "dhan_sell_probe", \{ status_only: true \}\)/);
assert.ok(!/dhan_sell_probe[^)]*yes/.test(spCode), "the panel can only READ the probe, never start the real order");
console.log("sell probe panel: ok");
