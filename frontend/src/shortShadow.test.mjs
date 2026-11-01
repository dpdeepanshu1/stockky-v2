// Run: node --experimental-strip-types shortShadow.test.mjs  (Node >= 22.6). The "Short selling" panel helpers.
import assert from "node:assert/strict";
import { sellProbeView, shortShadowView, shortSwitchRows, canSwitchShortOff } from "./shortShadowView.ts";
import { readFileSync, existsSync } from "node:fs";

const doc = (o = {}) => ({ mode: "off", gate_counters: { by_code: {} }, ...o });

// unavailable
assert.equal(shortShadowView(null).kind, "unavailable");
assert.equal(shortShadowView(undefined).refusals.length, 0);

// off: tells where to switch it on; a leftover "shadow" mode reads as off (the service cannot run it any more)
const off = shortShadowView(doc({ mode: "off" }));
assert.equal(off.kind, "off");
assert.match(off.headline, /SHORT_MODE is off/);
assert.equal(shortShadowView(doc({ mode: "shadow" })).kind, "off");

// live: says real orders are allowed and where they show
const live = shortShadowView(doc({ mode: "live" }));
assert.equal(live.kind, "live");
assert.match(live.headline, /LIVE/);
assert.match(live.headline, /Positions/);

// refusals sorted biggest first (ties by code), zero counts dropped, at most 6
const r = shortShadowView(doc({ mode: "live", gate_counters: { by_code: { SHORT_VWAP: 4, SHORT_NEAR_LOW: 9, A: 1, B: 1, C: 1, D: 1, E: 1, ZERO: 0 } } }));
assert.deepEqual(r.refusals.map((x) => x.code), ["SHORT_NEAR_LOW", "SHORT_VWAP", "A", "B", "C", "D"]);

// malformed input never throws
assert.equal(shortShadowView({ mode: "live" }).refusals.length, 0);
assert.equal(shortShadowView({ mode: "off", gate_counters: { by_code: { X: "bad" } } }).refusals.length, 0);

// the shadow history is gone from the view and the helpers module
const viewSrc = readFileSync(new URL("./shortShadowView.ts", import.meta.url), "utf8");
for (const gone of ["s2Ready", "s2Missing", "ShortShadowRow", "statusTone", "S2_DAYS_NEEDED", "would-be"]) assert.ok(!viewSrc.includes(gone), `${gone} is removed`);
assert.ok(!existsSync(new URL("./components/board/ShortShadowCard.tsx", import.meta.url)), "the shadow card file is deleted");
const tabSrc = readFileSync(new URL("./components/PositionStocksTab.tsx", import.meta.url), "utf8");
assert.ok(!tabSrc.includes("ShortShadowCard"), "the Position Stocks board no longer shows the shadow card");

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
assert.ok(!sp.includes("short_shadow_report"), "the shadow report button is removed");
assert.ok(!sp.includes("short-s2") && !sp.includes("short-counters"), "the S2 line and the shadow counters are removed");
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
