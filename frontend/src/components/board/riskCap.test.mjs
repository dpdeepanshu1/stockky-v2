// Run: node --experimental-strip-types components/board/riskCap.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { riskCap, zoneScoreLabel } from "./riskCap.ts";

const acct = { equity: 100000, cashAvailable: 50000, riskPerTradePct: 1 };       // cap = 1000
let r = riskCap({ ltp: 200, zoneRiskPerShare: 4 }, acct);
assert.equal(r.status, "ok"); assert.equal(r.maxQty, 250); assert.equal(r.source, "zone-stop");
r = riskCap({ ltp: 200, zoneRiskPerShare: 1500 }, acct);
assert.equal(r.status, "over"); assert.equal(r.maxQty, 0); assert.match(r.reason, /Even 1 share risks ₹1500\.00/);
r = riskCap({ ltp: 200, zoneRiskPerShare: 1 }, { ...acct, cashAvailable: 500 });
assert.equal(r.status, "ok"); assert.equal(r.maxQty, 2); assert.match(r.reason, /cash-limited/);
r = riskCap({ ltp: 200, zoneRiskPerShare: 1 }, { ...acct, cashAvailable: 150 });
assert.equal(r.status, "over"); assert.match(r.reason, /Cash ₹150/);
r = riskCap({ ltp: 200, zoneRiskPerShare: 1500, ownEntry: 200, ownStop: 198 }, acct);
assert.equal(r.status, "ok"); assert.equal(r.source, "own-stop"); assert.equal(r.riskPerShare, 2); assert.equal(r.maxQty, 250);
r = riskCap({ ltp: 200, zoneRiskPerShare: 4, ownEntry: 200, ownStop: 201 }, acct);
assert.equal(r.source, "zone-stop");
r = riskCap({ ltp: 200, engineVerdict: "Even 1 share risks ₹9 which exceeds the 1.00% per-trade cap" }, acct);
assert.equal(r.status, "over"); assert.equal(r.source, "engine");
assert.equal(riskCap({ ltp: 200, engineVerdict: "per_trade_risk_cap" }, acct).status, "over");
for (const bad of [{ ...acct, equity: null }, { ...acct, riskPerTradePct: 0 }, { ...acct, equity: NaN }]) {
  assert.equal(riskCap({ ltp: 200, zoneRiskPerShare: 4 }, bad).status, "unknown");
}
assert.equal(riskCap({ ltp: 200 }, acct).status, "unknown");
assert.equal(riskCap({ ltp: 200, zoneRiskPerShare: -3 }, acct).status, "unknown");
r = riskCap({ ltp: 200, zoneRiskPerShare: 4 }, { ...acct, cashAvailable: null });
assert.equal(r.status, "ok"); assert.equal(r.maxQty, 250);
assert.deepEqual(zoneScoreLabel(80), { text: "80", tone: "good" });
assert.deepEqual(zoneScoreLabel(50), { text: "50", tone: "mid" });
assert.deepEqual(zoneScoreLabel(10), { text: "10", tone: "weak" });
assert.deepEqual(zoneScoreLabel(null), { text: "-", tone: "none" });
console.log("riskCap tests passed");
