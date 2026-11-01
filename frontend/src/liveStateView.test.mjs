// Run: node --experimental-strip-types liveStateView.test.mjs  (Node >= 22.6). E285 round 87: the readable card for GET /live/state.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { liveStateBlocks, rupees, plain } from "./liveStateView.ts";

const find = (blocks, id) => blocks.find((b) => b.id === id);
const val = (b, label) => b.rows.find((r) => r.label === label)?.value;

assert.equal(rupees(7288), "₹7,288"); assert.equal(rupees(1234567.4), "₹12,34,567"); assert.equal(rupees(null), "-"); assert.equal(rupees("x"), "-"); assert.equal(rupees(NaN), "-");
assert.equal(plain(3), "3"); assert.equal(plain(0.12345), "0.12"); assert.equal(plain(undefined), "-");

// Real Trade
const rt = {
  pool: { ok: true, accounts: [{ mode: "REAL", starting_capital: 7288, current_equity: 7400.4, cash_available: 5000, broker_cash_available: null }] },
  heat: { ok: true, enabled: true, limit_pct: 5, own_open_risk: 100.46, own_gap_allowance: 20, peer_open_risk: 50, peer_capital_base: 7000 },
  setup_stamp: { ok: true, n: 10, stamped: 8, unstamped: 2, by_setup: { S1_CATALYST_UP: 3, S2_ORB_UP: 5 }, by_slot: { open: 6, mid: 2 } },
  order_rate: { ok: true, enabled: true, working: true, calls_last_s: 3, by_service_last_s: { ps: 1, rt: 2 } },
};
const a = liveStateBlocks("rt", rt);
assert.deepEqual(a.map((b) => b.id), ["pool", "heat", "stamps", "order_rate"]);
assert.equal(val(find(a, "pool"), "REAL: starting capital"), "₹7,288");
assert.equal(val(find(a, "pool"), "REAL: broker cash"), "-");
assert.equal(val(find(a, "heat"), "shared heat limit"), "5 % of the pool");
assert.equal(val(find(a, "heat"), "this service: open risk + gap allowance"), "₹100 + ₹20");
assert.equal(val(find(a, "stamps"), "no stamp"), "2");
assert.equal(find(a, "stamps").rows.find((r) => r.label === "no stamp").tone, "warn");
assert.equal(find(a, "stamps").rows.filter((r) => r.label.startsWith("  "))[0].label, "  S2_ORB_UP");   // biggest first
assert.equal(val(find(a, "order_rate"), "file usable"), "on");
assert.equal(val(find(a, "order_rate"), "  Position Stocks"), "1");
// an older Real Trade answer without order_rate still renders
assert.deepEqual(liveStateBlocks("rt", { pool: rt.pool, heat: rt.heat, setup_stamp: rt.setup_stamp }).map((b) => b.id), ["pool", "heat", "stamps"]);
// a failed part is one red row and does not hide the others
const b = liveStateBlocks("rt", { ...rt, heat: { ok: false, error: "RuntimeError: db down" } });
assert.equal(find(b, "heat").ok, false); assert.equal(find(b, "heat").rows[0].tone, "bad"); assert.equal(find(b, "pool").ok, true);
// a shared window that cannot be used is a red row
assert.equal(find(liveStateBlocks("rt", { ...rt, order_rate: { ok: true, enabled: true, working: false, calls_last_s: null, by_service_last_s: {}, last_error: "OSError: read-only" } }), "order_rate").rows.find((r) => r.label === "file usable").tone, "bad");
assert.equal(find(liveStateBlocks("rt", { ...rt, order_rate: { ok: true, enabled: false } }), "order_rate").rows.length, 1);

// Position Stocks
const ps = {
  order_rate: { ok: true, calls: 12, waits: 2, max_wait_s: 0.4, forced: 0, peak_in_window: 4, budget_per_s: 4, enabled: true, shared_enabled: true, account_per_s: 8, account_waits: 1, shared_recorded: 12, shared_unusable: 0,
                shared: { file: "/data/shared/order_window.json", working: true, calls_last_s: 2, by_service_last_s: { ps: 1, rt: 1 } } },
  momentum: { ok: true, mode: "live", checked: 40, exits: 3 },
  shared_heat: { ok: true, enabled: true, limit_pct: 5, peer_open_risk: 12.5, peer_capital_base: 7288, peer_age_s: 30, peer_max_age_s: 900 },
  ladder: { ok: true, enabled: true, short_cap: 3, stages: { "S1_CATALYST_UP|LONG": 0, "S4_CATALYST_DOWN|SHORT": 2 } },
};
const c = liveStateBlocks("ps", ps);
assert.deepEqual(c.map((x) => x.id), ["order_rate", "shared_window", "momentum", "shared_heat", "ladder"]);
assert.equal(val(find(c, "order_rate"), "modifies that waited"), "2");
assert.equal(val(find(c, "order_rate"), "waits caused by the other service"), "1");
assert.equal(val(find(c, "momentum"), "mode"), "live"); assert.equal(val(find(c, "momentum"), "checked"), "40");
assert.equal(val(find(c, "shared_heat"), "other service: open risk"), "₹13");
assert.equal(find(c, "ladder").rows.filter((r) => r.label.startsWith("  "))[0].label, "  S4_CATALYST_DOWN SHORT");   // stage 2 first
assert.equal(val(find(c, "ladder"), "  S4_CATALYST_DOWN SHORT"), "stage 2");
// forced calls are red; a failed part does not hide the others
assert.equal(find(liveStateBlocks("ps", { ...ps, order_rate: { ...ps.order_rate, forced: 2 } }), "order_rate").rows.find((r) => r.label.startsWith("let through")).tone, "bad");
assert.equal(find(liveStateBlocks("ps", { ...ps, momentum: { ok: false, error: "boom" } }), "momentum").ok, false);

// never throws on junk
for (const junk of [null, undefined, 5, "x", [], {}, { pool: 3, heat: [], order_rate: null }]) {
  for (const svc of ["rt", "ps"]) { assert.doesNotThrow(() => liveStateBlocks(svc, junk)); assert.ok(Array.isArray(liveStateBlocks(svc, junk))); }
}

// CONTRACT: the field names read above exist in the services' live_state.py (so the card cannot drift from the route)
const root = new URL("../../", import.meta.url);
const rtSrc = readFileSync(new URL("services/real-trade-service/execution/live_state.py", root), "utf8");
const psSrc = readFileSync(new URL("services/position-stocks-service/orders/live_state.py", root), "utf8");
for (const k of ["starting_capital", "current_equity", "cash_available", "broker_cash_available", "own_open_risk", "own_gap_allowance", "peer_open_risk", "peer_capital_base", "limit_pct", "by_setup", "by_slot", "unstamped", "order_rate"]) assert.ok(rtSrc.includes(`"${k}"`) || rtSrc.includes(k), `rt live_state has no ${k}`);
for (const k of ["order_rate", "momentum", "shared_heat", "ladder", "peer_age_s", "peer_max_age_s", "short_cap", "stages", "limit_pct", "\"shared\""]) assert.ok(psSrc.includes(k), `ps live_state has no ${k}`);
const orSrc = readFileSync(new URL("services/position-stocks-service/execution/order_rate.py", root), "utf8");
for (const k of ["calls", "waits", "max_wait_s", "forced", "peak_in_window", "budget_per_s", "shared_enabled", "account_per_s", "account_waits", "shared_recorded", "shared_unusable"]) assert.ok(orSrc.includes(`"${k}"`), `order_rate has no ${k}`);

console.log("liveStateView.test.mjs: all checks passed");
