// Run: node --experimental-strip-types weather.test.mjs  (Node >= 22.6). Part 1 step 5: the Market Weather card.
// weather.fixtures.json holds REAL GET /weather/status payloads produced by the runner's weather.py with a fake market-data feed (2026-10-23).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { weatherView, applyLines, gateLine, lastWiringLine, hm, DIAL_LABEL, DIAL_ORDER, STATE_TEXT, STATE_TONE } from "./weatherView.ts";
import { shortExposureLine } from "./shortShadowView.ts";

const fx = JSON.parse(readFileSync(new URL("./weather.fixtures.json", import.meta.url), "utf8"));
const TODAY = "2026-10-23";

// state 1: OFF -> nothing computed, no state, no dials
const off = weatherView(fx.off, TODAY);
assert.equal(off.kind, "off");
assert.equal(off.state, null);
assert.deepEqual(off.dials, []);
assert.match(off.headline, /^Off\./);

// state 2: switched on but nothing has run (or the first stage failed) -> waiting, with the failure flagged
const failed = weatherView(fx.failed, TODAY);
assert.equal(failed.kind, "waiting");
assert.equal(failed.failed, true);
assert.match(failed.headline, /could not get its data/);
assert.equal(weatherView({ ...fx.off, enabled: true }, TODAY).headline, "Switched on. Waiting for today's 08:00 morning stage.");

// state 3: ACTIVE, calm -> label, no dial would change
const calm = weatherView(fx.active, TODAY);
assert.equal(calm.kind, "active");
assert.equal(calm.state, "calm");
assert.equal(calm.stale, false);
assert.deepEqual(calm.dials.map((d) => d.key), ["MARKET_STATE_SHORT_MULT"], "calm changes only the short dial (shorts off); every long dial stays");
assert.equal(calm.dials[0].value, "0");
assert.deepEqual(weatherView({ ...fx.active, current: { ...fx.active.current, state: "normal" } }, TODAY).dials, [], "normal only stamps: no dial is listed");
assert.equal(calm.today.length, 1);
assert.equal(hm(calm.today[0].at), "08:00");

// ACTIVE, panic -> every dial the profile would tighten, in display order, with real values from the payload
const panic = weatherView(fx.stressed, TODAY);
assert.equal(panic.state, "panic");
assert.equal(panic.dials.length, DIAL_ORDER.length);
assert.deepEqual(panic.dials.map((d) => d.key), DIAL_ORDER);
assert.equal(panic.dials.find((d) => d.key === "MARKET_STATE_RISK_MULT").value, "0.4");
assert.equal(panic.dials.find((d) => d.key === "ENTRY_LAST_TIME_IST").value, "12:00");
assert.deepEqual(panic.today.map((r) => r.stage), ["morning", "lock"], "today's rows are oldest first");
assert.equal(STATE_TONE.panic, "bad");
assert.equal(STATE_TONE.calm, "good");

// a state from an earlier day is flagged stale, not shown as today's
const stale = weatherView(fx.stressed, "2026-10-26"); // the state is from Fri 23 Oct, the page is opened on Monday before 08:00
assert.equal(stale.kind, "active");
assert.equal(stale.stale, true);
assert.equal(stale.today.length, 0);
assert.match(stale.headline, /Today has not started yet/);

// every dial in every profile row has a label (a new dial without one would show a blank row)
for (const row of fx.stressed.profile) for (const k of Object.keys(row.dials)) assert.ok(DIAL_LABEL[k], `label for ${k}`);
for (const row of fx.stressed.profile) assert.ok(STATE_TEXT[row.state]);

// missing payload never throws
assert.equal(weatherView(null, TODAY).kind, "off");

// last apply attempt: what was sent, or why nothing was
const lines = applyLines({ at: "2026-10-23T09:14:00+05:30", stage: "lock", applied: true, why: "applied", services: { "real-trade": { ok: true, entries: { MARKET_STATE_RISK_MULT: "0.4" } }, "position-stocks": { ok: false, why: "service MARKET_STATE_MODE is shadow, not live: nothing sent" } } });
assert.equal(lines[0], "09:14 lock: dials applied");
assert.match(lines[1], /^real-trade: Risk per trade \(× saved\) 0\.4$/);
assert.match(lines[2], /not live: nothing sent/);
assert.deepEqual(applyLines(null), []);
assert.equal(applyLines({ at: "2026-10-23T09:14:00+05:30", stage: "lock", applied: false, why: "not applied: no regime_shadow_report result in the last 14 days" })[0], "09:14 lock: not applied: no regime_shadow_report result in the last 14 days");

// the gate line: mode and what opened or closed it
assert.equal(gateLine(null), "");
assert.equal(gateLine({ mode: "replay_only", open: true, why: "replay USEFUL (shadow report not required: WEATHER_GATE_MODE=replay_only)" }), "Gate (replay_only): open - replay USEFUL (shadow report not required: WEATHER_GATE_MODE=replay_only)");
assert.match(gateLine({ mode: "strict", open: false, why: "the latest regime_history_replay verdict is not USEFUL" }), /^Gate \(strict\): closed - /);

// UI wiring and shadow honesty
const panel = readFileSync(new URL("./components/calibration/WeatherPanel.tsx", import.meta.url), "utf8");
const tab = readFileSync(new URL("./components/CalibrationControlTab.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./calibrationApi.ts", import.meta.url), "utf8");
assert.match(tab, /section === "weather" && <WeatherPanel \/>/);
assert.match(tab, /\{ id: "weather", label: "Market Weather" \}/);
assert.match(api, /"\/weather\/status"/);
assert.match(api, /"\/weather\/run"/);
assert.match(panel, /SHADOW · changes nothing/);
assert.match(panel, /LIVE · tighten-only/);
assert.match(panel, /data-testid="weather-gate"/);
assert.match(panel, /Send dials: \{st\.apply \? "ON" : "off"\}/);
const code = (panel + api).replace(/^\s*\/\/.*$/gm, "");
assert.ok(!/revert/i.test(code) && !/overlay\(/.test(code), "shadow card has no revert button and calls no overlay endpoint");
assert.ok(!/\/control\//.test(panel.replace(/^\s*\/\/.*$/gm, "") + api.slice(api.indexOf("weatherStatus"), api.indexOf("calibrationSummary"))), "the card never calls a trading service /control endpoint");
console.log("weather.test.mjs: ok");

// short selling step 6: the short exposure line of the card, from the profile's short dial (position-stocks only)
const dialsOf = (st) => fx.active.profile.find((p) => p.state === st).dials;
assert.match(shortExposureLine("calm", dialsOf("calm")), /OFF today/);
assert.match(shortExposureLine("choppy", dialsOf("choppy")), /OFF today/);
assert.match(shortExposureLine("panic", dialsOf("panic")), /50% of the saved short size/);
assert.match(shortExposureLine("normal", dialsOf("normal")), /left at the saved short size/);
assert.match(shortExposureLine("stressed", dialsOf("stressed")), /left at the saved short size/);
assert.equal(shortExposureLine(null, {}), "");
assert.ok(DIAL_LABEL.MARKET_STATE_SHORT_MULT && DIAL_ORDER.includes("MARKET_STATE_SHORT_MULT"));

// item 7c: the last wiring_check result on the card (from the runner's own run list)
const run = (o) => ({ target: "wiring_check", status: "ok", finished_at: "2026-10-26T09:05:12+05:30", headline: ["WIRING: PASS"], exit_summary: "", ...o });
assert.match(lastWiringLine(null), /never run/);
assert.match(lastWiringLine([run({ target: "services_health" })]), /never run/);
assert.equal(lastWiringLine([run()]), "Wiring check 2026-10-26 09:05 IST: PASS");
assert.match(lastWiringLine([run({ headline: ["x", "WIRING: FAIL - position-stocks: dial MARKET_STATE_SHORT_MULT is not live"] })]), /FAIL - position-stocks: dial MARKET_STATE_SHORT_MULT/);
assert.match(lastWiringLine([run({ status: "running" }), run({ finished_at: "2026-10-25T09:00:00+05:30" })]), /2026-10-25 09:00 IST: PASS/);   // a run in progress is skipped
assert.match(lastWiringLine([run({ status: "failed", headline: [], exit_summary: "timed out" })]), /failed - timed out/);
assert.match(panel, /data-testid="weather-wiring"/);
