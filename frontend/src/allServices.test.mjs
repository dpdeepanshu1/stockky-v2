// Run: node --experimental-strip-types allServices.test.mjs  (Node >= 22.6). Round 266: pure helpers of Calibration > All services.
import assert from "node:assert/strict";
import { netToday, basisText, fromRealTrade, fromPositionStocks, fromFno, failed, liveState, totals, headline, rupees, STATE_TEXT, STATE_TONE, SVC_WHERE } from "./allServices.ts";

const gate = (o = {}) => ({ mode: "REAL", auto_pilot_enabled: true, armed: true, disarmed_reason: null, account: { realized_pnl_today: -120.5 }, ...o });
const ps = (o = {}) => ({ auto_pilot_enabled: true, armed: true, service_enabled: true, daily_loss_kill_switch: false, ...o });
const led = (o = {}) => ({ realized_pnl_today: 300, daily_loss_kill_switch_tripped: false, ...o });
const fno = (o = {}) => ({ locks: { engine_enabled: true, mode: "demo", real_enabled: false, armed_today: false, real_orders_possible: false, closed_locks: ["real_enabled"], ...(o.locks || {}) }, day: { realized_net: 55 }, open_positions: [{}, {}], ...(o.rest || {}) });

// Real Trade
let rt = fromRealTrade(gate());
assert.equal(rt.mode, "REAL"); assert.equal(rt.pnlToday, -120.5); assert.equal(liveState(rt), "live");
assert.equal(liveState(fromRealTrade(gate({ armed: false, disarmed_reason: "risk not confirmed" }))), "not_armed");
assert.equal(fromRealTrade(gate({ armed: false, disarmed_reason: "risk not confirmed" })).note, "risk not confirmed");
assert.equal(liveState(fromRealTrade(gate({ auto_pilot_enabled: false }))), "engine_off");
assert.equal(liveState(fromRealTrade(gate({ mode: "DEMO" }))), "demo");
assert.equal(fromRealTrade(gate({ account: { realized_pnl_today: null } })).pnlToday, null);   // null stays null, never 0
assert.equal(fromRealTrade(null).ok, false);

// Position Stocks: the ledger's kill switch wins; a missing ledger leaves P&L unknown but the status still counts
let p = fromPositionStocks(ps(), led());
assert.equal(p.pnlToday, 300); assert.equal(liveState(p), "live");
assert.equal(liveState(fromPositionStocks(ps(), led({ daily_loss_kill_switch_tripped: true }))), "kill_switch");
assert.equal(liveState(fromPositionStocks(ps({ daily_loss_kill_switch: true }), null)), "kill_switch");
assert.equal(fromPositionStocks(ps(), null).pnlToday, null);
assert.equal(fromPositionStocks(ps({ service_enabled: false }), led()).note !== null, true);
assert.equal(fromPositionStocks(undefined, led()).ok, false);

// F&O: DEMO is simulated; REAL needs every lock; flags missing never read as LIVE
let f = fromFno(fno());
assert.equal(f.mode, "DEMO"); assert.equal(liveState(f), "demo"); assert.equal(f.openCount, 2); assert.match(f.note, /real_enabled/);
const fr = fromFno(fno({ locks: { mode: "real", real_enabled: true, armed_today: true, real_orders_possible: true, closed_locks: [] } }));
assert.equal(liveState(fr), "live"); assert.equal(fr.note, null);
assert.equal(liveState(fromFno(fno({ locks: { mode: "real", armed_today: true, real_orders_possible: false } }))), "armed_off");
assert.equal(liveState(fromFno(fno({ locks: { mode: "real", engine_enabled: false } }))), "engine_off");
assert.equal(liveState(fromFno({ locks: { mode: "real" } })), "unreachable");
assert.equal(fromFno({}).ok, false);

// gross vs net (round 267): the status gives GROSS, the charges answer gives NET; net wins, gross is labelled, junk is ignored
const ch = (n) => ({ pnl: { net_realized_today: n, realized_gross_today: 0, charges_today: 0 } });
assert.equal(netToday(ch(-150.25)), -150.25); assert.equal(netToday({}), null); assert.equal(netToday(null), null); assert.equal(netToday({ pnl: { net_realized_today: "x" } }), null);
const rtn = fromRealTrade(gate(), ch(-150.25));
assert.equal(rtn.pnlToday, -150.25); assert.equal(rtn.pnlBasis, "net"); assert.equal(basisText(rtn), "net realized today, after charges");
assert.equal(rt.pnlBasis, "gross"); assert.match(basisText(rt), /^GROSS realized today, before charges/);   // no charges answer: gross, said so
const psn = fromPositionStocks(ps(), led(), ch(280));
assert.equal(psn.pnlToday, 280); assert.equal(psn.pnlBasis, "net");
assert.equal(fromPositionStocks(ps(), null, ch(280)).pnlToday, 280);    // charges alone are enough for the P&L
assert.equal(fromPositionStocks(ps(), null, null).pnlBasis, null); assert.equal(basisText(fromPositionStocks(ps(), null, null)), "not reported");
assert.equal(f.pnlBasis, "net"); assert.match(basisText(f), /simulated/);
assert.deepEqual(totals([rt, p, f]).gross, ["rt", "ps"]); assert.deepEqual(totals([rtn, psn, f]).gross, []);
assert.equal(totals([rtn, psn]).real, 129.75);
assert.match(headline([rt, f]), /Real Trade: figure is GROSS \(before charges\)/); assert.doesNotMatch(headline([rtn, psn, f]), /GROSS/);

// failed(): the message is kept, no P&L, state unreachable
const bad = failed("ps", new Error("Cannot reach http://x"));
assert.equal(bad.ok, false); assert.equal(bad.error, "Cannot reach http://x"); assert.equal(bad.pnlToday, null); assert.equal(liveState(bad), "unreachable");
assert.equal(failed("rt", undefined).error, "no answer");

// totals: REAL sums rt + ps (+ F&O only in REAL); DEMO F&O is shown apart; a failed service is listed, not counted as 0
let t = totals([rt, p, f]);
assert.equal(t.real, 179.5); assert.equal(t.realCount, 2); assert.equal(t.simulated, 55); assert.deepEqual(t.missing, []); assert.deepEqual(t.live, ["rt", "ps"]);
t = totals([rt, bad, fr]);
assert.equal(t.real, -120.5 + 55); assert.deepEqual(t.missing, ["ps"]);
t = totals([failed("rt", "x"), failed("ps", "y")]);
assert.equal(t.real, null); assert.equal(t.simulated, null); assert.deepEqual(t.missing, ["rt", "ps"]);
assert.equal(totals([fromRealTrade(gate({ account: { realized_pnl_today: 0.1 } })), fromPositionStocks(ps(), led({ realized_pnl_today: 0.2 }))]).real, 0.3);   // no float tail

// headline
assert.match(headline([rt, p, f]), /2 of 3 services can send real orders now \(Real Trade, Position Stocks\)/);
assert.match(headline([fromRealTrade(gate({ armed: false })), f]), /No service can send real orders right now/);
assert.match(headline([bad, rt]), /No P&L from: Position Stocks .*not counted as 0/);
assert.match(headline([fromPositionStocks(ps(), led({ daily_loss_kill_switch_tripped: true }))]), /Kill switch tripped: Position Stocks/);

// words and colours: LIVE and kill switch are red, every state has words, every service says where to change it
for (const k of Object.keys(STATE_TEXT)) assert.ok(STATE_TEXT[k].length >= 3);
assert.equal(STATE_TONE.live, "bad"); assert.equal(STATE_TONE.kill_switch, "bad"); assert.equal(STATE_TONE.engine_off, undefined);
assert.ok(["rt", "ps", "fno"].every((k) => SVC_WHERE[k].length > 20));

// rupees
assert.equal(rupees(-120.5), "-Rs 120.50"); assert.equal(rupees(1234567.891), "+Rs 12,34,567.89"); assert.equal(rupees(0), "Rs 0.00");
assert.equal(rupees(null), "-"); assert.equal(rupees(NaN), "-");
console.log("allServices.test.mjs OK");
