// Run: node --experimental-strip-types paperNet.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { paperNet, paperNetTotals } from "./paperNet.ts";
import { legCharges, DP_CHARGE_INCL_GST } from "./chargesRates.ts";

const closed = { quantity: 100, entry_price: 100, exit_price: 110, pnl_amount: 1000, status: "CLOSED", entry_date: "2026-10-01", exit_date: "2026-10-08" };
const r = paperNet(closed);
const expected = legCharges(10000, "BUY", true).total + legCharges(11000, "SELL", true).total + DP_CHARGE_INCL_GST;
assert.ok(Math.abs(r.charges - expected) < 1e-9); assert.equal(r.gross, 1000); assert.ok(Math.abs(r.net - (1000 - expected)) < 1e-9); assert.equal(r.estimated, false);
assert.ok(r.charges > 20 && r.charges < 60);                                         // sanity: ~Rs 36 on a Rs 10,000 swing, mostly STT both legs + DP

// same-day round trip pays no DP
const sd = paperNet({ ...closed, exit_date: "2026-10-01 14:10:00", entry_date: "2026-10-01 09:20:00" });
assert.ok(Math.abs(sd.charges - (r.charges - DP_CHARGE_INCL_GST)) < 0.2);

// a loser nets worse than gross; gross falls back to price difference when pnl_amount is missing
const l = paperNet({ ...closed, exit_price: 95, pnl_amount: null });
assert.equal(l.gross, -500); assert.ok(l.net < -500);

// open trade: estimated, uses the last price
const o = paperNet({ quantity: 10, entry_price: 100, current_price: 105, pnl_amount: 50, status: "OPEN", entry_date: "2026-10-20" });
assert.equal(o.estimated, true); assert.equal(o.gross, 50); assert.ok(o.net < 50);

// junk is null, never a throw
for (const bad of [null, undefined, {}, { ...closed, quantity: 0 }, { ...closed, exit_price: null }, { ...closed, entry_price: "x" }, { status: "OPEN", quantity: 1, entry_price: 1 }]) assert.equal(paperNet(bad), null);

// totals: closed only, skips unusable rows
const t = paperNetTotals([closed, { ...closed, exit_price: 95, pnl_amount: -500 }, { ...closed, exit_price: null }, { status: "OPEN", quantity: 1, entry_price: 1, current_price: 2 }, null]);
assert.equal(t.n, 2); assert.equal(t.gross, 500); assert.ok(t.net < 500 && Math.abs(t.net - (500 - t.charges)) < 1e-9);
assert.deepEqual(paperNetTotals(null), { gross: 0, charges: 0, net: 0, n: 0 });
console.log("paperNet: all assertions passed");
