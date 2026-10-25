// Run: node --experimental-strip-types src/tradesView.test.mjs
import assert from "node:assert/strict";
import { tradesKpis, signedRupee } from "./components/tradesView.ts";

assert.deepEqual(tradesKpis(null), []);
assert.deepEqual(tradesKpis(undefined), []);

// nothing reported: dashes, never invented zeros
const e = tradesKpis({});
assert.equal(e.length, 6);
for (const k of e) assert.equal(k.value, "—");
assert.equal(e[1].sub, "open count not reported");
assert.equal(e[3].sub, "closed count not reported");

// a normal portfolio
const s = tradesKpis({ cash_balance: 125000.4, open_positions_value: 48250, open_positions_pnl: 1234.6, open_positions: 3, closed_positions: 1, win_rate: 62.5, avg_r_multiple: 0.456, max_drawdown_pct: 4.26 });
assert.equal(s[0].value, "₹1,25,000");
assert.equal(s[1].value, "₹48,250"); assert.equal(s[1].sub, "3 open positions");
assert.equal(s[2].value, "+₹1,235"); assert.equal(s[2].tone, "up"); assert.equal(s[2].sub, "up on open positions");
assert.equal(s[3].value, "62.5%"); assert.equal(s[3].sub, "1 closed trade");
assert.equal(s[4].label, "Avg R"); assert.equal(s[4].value, "0.46R");
assert.equal(s[5].value, "4.3%"); assert.equal(s[5].tone, "flat");

// loss is signed and spelled
const l = tradesKpis({ open_positions_pnl: -300 })[2];
assert.equal(l.value, "-₹300"); assert.equal(l.tone, "down"); assert.equal(l.sub, "down on open positions");
// a real zero is a reading; a sub-rupee wiggle rounds to flat, not "+₹0"
const z = tradesKpis({ open_positions_pnl: 0, cash_balance: 0, win_rate: 0, open_positions: 1 });
assert.equal(z[2].value, "₹0"); assert.equal(z[2].tone, "flat"); assert.equal(z[2].sub, "unchanged on open positions");
assert.equal(z[0].value, "₹0"); assert.equal(z[3].value, "0%"); assert.equal(z[1].sub, "1 open position");
assert.equal(tradesKpis({ open_positions_pnl: 0.4 })[2].value, "₹0");
assert.equal(tradesKpis({ open_positions_pnl: -0.4 })[2].tone, "flat");
assert.equal(signedRupee(-0.2), "₹0");
// NaN / non-number is missing
assert.equal(tradesKpis({ cash_balance: NaN, win_rate: null })[0].value, "—");
assert.equal(tradesKpis({ win_rate: null })[3].value, "—");
// Avg R falls back to average P&L %, then to a dash; a real 0R stays 0.00R
const f = tradesKpis({ avg_pnl_pct: 2.5 })[4];
assert.equal(f.label, "Avg trade"); assert.equal(f.value, "+2.5%");
assert.equal(tradesKpis({ avg_pnl_pct: -1.5 })[4].value, "-1.5%");
assert.equal(tradesKpis({ avg_r_multiple: 0 })[4].value, "0.00R");
assert.equal(tradesKpis({ max_drawdown_pct: 0 })[5].value, "0.0%");
for (const k of s) assert.equal(typeof k.value, "string");
console.log("tradesView ok");
