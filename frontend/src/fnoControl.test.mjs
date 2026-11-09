// Run: node --experimental-strip-types fnoControl.test.mjs  (Node >= 22.6). Round 262: pure helpers of the F&O Control card.
import assert from "node:assert/strict";
import { pickControl, currentValue, isOn, toggledValue, needsTypedConfirm, typedConfirmOk, valueText, resultText, offRecommended, LOCK_KEYS, EXPIRY_STRATS } from "./fnoControl.ts";

const R = (key, o = {}) => ({ key, title: key, group: "", kind: "bool", help: "", choices: [], lo: null, hi: null, live: true, shipped: "false", recommended: "false", looser: "on", evidence: "", running: null, desired: null, effective: null,
  source: "default", override: null, pending_restart: false, matches_recommended: null, restart_needed_on_change: false, ...o });
const STRATS = ["CORE", "TREND_RIDER", "MOMENTUM_SURGE", "BEAR_SWEEP", "BEAR_PIVOT", "DRIFT_EDGE", "WEEKLY_OPENER", "EXPIRY_MOMENTUM", "EXPIRY_ALPHA"].map((s) => R("FNO_STRAT_" + s));
const flags = [R("FNO_RISK_PCT", { kind: "float" }), ...STRATS, R("FNO_UNDERLYINGS", { kind: "enum", shipped: "both", recommended: null }), R("FNO_REAL_ENABLED"), R("FNO_MODE", { kind: "enum", shipped: "demo", recommended: "demo" }), R("FNO_ENGINE_ENABLED")];

// locks come in the fixed order; all nine strategies, and nothing else
const v = pickControl(flags);
assert.deepEqual(v.locks.map((r) => r.key), [...LOCK_KEYS]);
assert.equal(v.strategies.length, 9);
assert.ok(v.strategies.every((r) => r.key.startsWith("FNO_STRAT_")));
assert.ok(EXPIRY_STRATS.every((k) => v.strategies.some((r) => r.key === k)));
// a service that lacks a lock, or gives junk, never crashes the card
assert.equal(pickControl([R("FNO_MODE")]).locks.length, 1);
assert.deepEqual(pickControl(null), { locks: [], strategies: [] });
assert.deepEqual(pickControl(undefined), { locks: [], strategies: [] });
// a non-bool FNO_STRAT_ row (e.g. the per-day cap) is not a switch
assert.equal(pickControl([R("FNO_STRAT_X", { kind: "int" })]).strategies.length, 0);

// current value: running wins over effective / desired / shipped
assert.equal(currentValue(R("A", { running: "true", shipped: "false" })), "true");
assert.equal(currentValue(R("A", { running: null, effective: "TRUE" })), "true");
assert.equal(currentValue(R("A", { running: null, effective: null, desired: null, shipped: "false" })), "false");
assert.equal(isOn(R("A", { running: "true" })), true);
assert.equal(toggledValue(R("A", { running: "true" })), "false");
assert.equal(toggledValue(R("A", { running: "false" })), "true");

// REAL-capable changes need the typed word; turning things OFF / DEMO never does
assert.equal(needsTypedConfirm("FNO_REAL_ENABLED", "true"), true);
assert.equal(needsTypedConfirm("FNO_REAL_ENABLED", "false"), false);
assert.equal(needsTypedConfirm("FNO_MODE", "real"), true);
assert.equal(needsTypedConfirm("FNO_MODE", "demo"), false);
assert.equal(needsTypedConfirm("FNO_ENGINE_ENABLED", "true"), false);
assert.equal(typedConfirmOk(" real "), true);
assert.equal(typedConfirmOk("yes"), false);
assert.equal(typedConfirmOk(null), false);

// texts
assert.equal(valueText(R("A"), "true"), "ON");
assert.equal(valueText(R("M", { kind: "enum" }), "demo"), "DEMO");
assert.match(resultText(R("A", { title: "Engine" }), { change: "LOOSENS", value: "true" }), /Engine: now ON, effective immediately \(loosens\)/);
assert.match(resultText(R("A", { title: "Engine" }), { change: "TIGHTENS", value: "false", restart_required: true }), /restart/);
assert.match(resultText(R("A", { title: "Engine" }), { change: "SAME", value: "true" }), /already ON/);

// recommended count: all at recommended -> 0; one strategy ON against a recommended OFF -> 1
assert.equal(offRecommended(v), 0);
assert.equal(offRecommended(pickControl([R("FNO_STRAT_TREND_RIDER", { running: "true" })])), 1);
console.log("fnoControl.test.mjs OK");
