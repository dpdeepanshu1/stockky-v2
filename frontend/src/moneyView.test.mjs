// Run: node --experimental-strip-types moneyView.test.mjs  (Node >= 22.6). Round 270: the Dhan money helpers.
import assert from "node:assert/strict";
import { boardView, openCharges, afterCharges } from "./moneyView.ts";

const doc = { ts: 1000, day: "2026-11-09", mode: "live", active: ["real_trade", "position_stocks"], pcts: { real_trade: 37.5, position_stocks: 37.5, fno: 0 },
  amounts: { real_trade: 37500, position_stocks: 37500, fno: 0 }, total_equity: 100000, free_cash: 80000, exposure: { real_trade: 15000, position_stocks: 5000, fno: 0 }, scores: { real_trade: 0.4, position_stocks: null, fno: null }, trades: { real_trade: 25, position_stocks: 3, fno: 0 } };

let v = boardView({ mode: "live", this_service: "position_stocks", board: doc }, 1000 + 600);
assert.equal(v.state, "live"); assert.equal(v.total, 100000); assert.equal(v.freeCash, 80000); assert.equal(v.ageMin, 10);
assert.equal(v.rows.length, 3); assert.equal(v.thisRow.key, "position_stocks"); assert.equal(v.thisRow.room, 32500); assert.equal(v.rows[0].room, 22500);
assert.equal(v.rows[2].active, false); assert.equal(v.sizing, null);
v = boardView({ mode: "shadow", this_service: "real_trade", fixed_share_pct: 50, why: "fixed (mode not live)", board: doc }, 1000);
assert.equal(v.state, "shadow"); assert.ok(v.notes[0].includes("50%"));
assert.equal(boardView({ mode: "live", this_service: "real_trade", board: { ...doc, mode: "shadow" } }, 1000).state, "shadow");
assert.equal(boardView({ mode: "off", board: null }).state, "off");
assert.equal(boardView({ mode: "shadow", board: null }).state, "missing");
assert.equal(boardView(null).state, "missing");
assert.equal(boardView({ mode: "live", this_service: "fno", board: { ...doc, exposure: { fno: 999 }, amounts: { ...doc.amounts, fno: 100 } } }, 1000).rows[2].room, 0);
assert.equal(boardView({ mode: "shadow", this_service: "fno", active_for_real: false, real_sizing_capital: 0, demo_capital: 500000, board: null }).sizing.amount, 500000);
assert.equal(boardView({ mode: "live", this_service: "fno", active_for_real: true, real_sizing_capital: 25000, demo_capital: 500000, board: doc }, 1000).sizing.amount, 25000);

const c = openCharges({ side: "LONG", qty: 100, entry: 100, price: 110 });
assert.ok(c > 0 && c < 30, String(c));
assert.equal(openCharges({ side: "LONG", qty: 100, entry: 100, price: null }), null);
assert.equal(openCharges({ side: "LONG", qty: 0, entry: 100, price: 110 }), null);
assert.ok(openCharges({ side: "SHORT", qty: 100, entry: 110, price: 100 }) > 0);
assert.ok(openCharges({ side: "LONG", qty: 10, entry: 100, price: 110, delivery: true, entryAlreadyPaid: true }) < openCharges({ side: "LONG", qty: 10, entry: 100, price: 110, delivery: true }));
assert.equal(afterCharges(100, 12.345), 87.66);
assert.equal(afterCharges(null, 5), null);
assert.equal(afterCharges(100, null), null);
console.log("moneyView OK");
