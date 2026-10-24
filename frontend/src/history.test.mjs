// E122 Trade History helpers + wiring (run with: node --experimental-strip-types src/history.test.mjs)
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { summarize, pnlOf, equityCurve, inRange, istDay, insights, toCsv, signedInr, heldLabel, reasonLabel, newestFirst, pctLabel } from "./components/realAuto/history.ts";

const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
let passed = 0;
const test = (name, fn) => { fn(); passed += 1; console.log("ok -", name); };
const rec = (id, net, extra = {}) => ({ position_id: id, mode: "REAL", symbol: `S${id}`, entry_price: 100, exit_price: 101, quantity: 1, net_pnl: net, gross_pnl: null, charges: 1, closed_at: `2026-10-2${id % 9}T06:00:00Z`, ...extra });

test("net P&L is used when known, gross otherwise, null when neither", () => {
  assert.equal(pnlOf(rec(1, 50, { gross_pnl: 60 })), 50);
  assert.equal(pnlOf(rec(1, null, { gross_pnl: 60 })), 60);
  assert.equal(pnlOf(rec(1, null, { gross_pnl: null })), null);
});
test("summary: wins, losses, breakeven, win rate, best / worst, charges, payoff", () => {
  const s = summarize([rec(1, 100), rec(2, 300), rec(3, -100), rec(4, 0)]);
  assert.deepEqual([s.wins, s.losses, s.breakeven, s.counted], [2, 1, 1, 4]);
  assert.equal(s.winRate, 50); assert.equal(s.net, 300); assert.equal(s.best, 300); assert.equal(s.worst, -100);
  assert.equal(s.avgWin, 200); assert.equal(s.avgLoss, -100); assert.equal(s.payoff, 2); assert.equal(s.charges, 4);
});
test("summary of nothing is empty, not NaN", () => {
  const s = summarize([]);
  assert.equal(s.trades, 0); assert.equal(s.winRate, null); assert.equal(s.best, null); assert.equal(s.payoff, null); assert.equal(s.net, 0);
});
test("a record with no known P&L is listed but never counted as a win or loss", () => {
  const s = summarize([rec(1, null), rec(2, 10)]);
  assert.equal(s.trades, 2); assert.equal(s.counted, 1); assert.equal(s.wins, 1); assert.equal(s.losses, 0);
});
test("equity curve is a running total in close order", () => {
  const c = equityCurve([rec(3, -50), rec(1, 100), rec(2, 20), rec(4, null)]);
  assert.deepEqual(c, [100, 120, 70]);
});
test("the IST day rolls over at 18:30 UTC, not midnight UTC", () => {
  assert.equal(istDay("2026-10-24T18:29:00Z"), "2026-10-24");
  assert.equal(istDay("2026-10-24T18:31:00Z"), "2026-10-25");
  assert.equal(istDay(null), null);
  assert.equal(istDay("nonsense"), null);
});
test("today range keeps only today's IST day; other ranges are already cut by the server", () => {
  const now = new Date("2026-10-24T10:00:00Z");
  const rows = [rec(1, 1, { closed_at: "2026-10-24T05:00:00Z", exit_day_ist: "2026-10-24" }), rec(2, 1, { closed_at: "2026-10-23T05:00:00Z", exit_day_ist: "2026-10-23" })];
  assert.deepEqual(inRange(rows, "today", now).map((r) => r.position_id), [1]);
  assert.equal(inRange(rows, "7d", now).length, 2);
});
test("newest closed trade first; records without a close time go last", () => {
  const out = newestFirst([rec(1, 1, { closed_at: "2026-10-20T00:00:00Z" }), rec(2, 1, { closed_at: null }), rec(3, 1, { closed_at: "2026-10-22T00:00:00Z" })]);
  assert.deepEqual(out.map((r) => r.position_id), [3, 1, 2]);
});
test("pattern read needs 3 trades, names groups of 3+, and never calls anything a forecast", () => {
  assert.deepEqual(insights([rec(1, 10), rec(2, -5)]), []);
  const rows = [1, 2, 3].map((i) => rec(i, -100, { exit_reason: "STOP_LOSS" })).concat([4, 5, 6].map((i) => rec(i, 200, { exit_reason: "TARGET" })));
  const out = insights(rows);
  const text = out.map((x) => `${x.title} ${x.body}`).join(" ");
  assert.ok(/Costliest exit/.test(text) && /Stop hit/.test(text) && /Best exit/.test(text) && /Target hit/.test(text));
  assert.ok(!/will|forecast|predict|should|expect/i.test(text), "insights describe the past only");
  const two = [rec(1, -100, { exit_reason: "X" }), rec(2, -100, { exit_reason: "X" }), rec(3, 5, { exit_reason: "Y" })];
  assert.ok(!insights(two).some((x) => x.title === "Costliest exit"), "a group of 2 is not named");
});
test("CSV escapes commas, quotes and new lines and has one header line", () => {
  const csv = toCsv([rec(1, 5, { symbol: 'A,"B"\nC' })]);
  const lines = csv.split("\n");
  assert.ok(lines[0].startsWith("symbol,closed_at"));
  assert.ok(csv.includes('"A,""B""\nC"'));
});
test("labels: signed rupees always carry a sign, hold time and reasons read as words", () => {
  assert.equal(signedInr(1234), "+₹1,234"); assert.equal(signedInr(-1234), "-₹1,234"); assert.equal(signedInr(0), "₹0"); assert.equal(signedInr(null), "—");
  assert.equal(heldLabel({ held_minutes: 42 }), "42 min"); assert.equal(heldLabel({ held_minutes: 185 }), "3h 05m");
  assert.equal(heldLabel({ held_minutes: null, held_days: 0 }), "same day"); assert.equal(heldLabel({ held_days: 2 }), "2 days");
  assert.equal(reasonLabel("STOP_LOSS"), "Stop hit"); assert.equal(reasonLabel("WEIRD_CODE"), "Weird code"); assert.equal(reasonLabel(null), "Reason not recorded");
  assert.equal(pctLabel(1.234), "+1.23%"); assert.equal(pctLabel(-0.5), "-0.50%");
});
test("wiring: the tab is in the Real Trade tab bar, calls the mode-scoped records route, and can change nothing", () => {
  const rat = rd("./components/RealAutoTrade.tsx");
  assert.ok(/\{ id: "history", label: "Trade History" \}/.test(rat), "tab missing");
  assert.ok(/activeTab === "history" && \(\s*<HistoryTab mode=\{mode\} loggedIn=\{loggedIn\}/.test(rat), "tab not mounted");
  assert.ok(/\/positions\/\$\{mode\}\/records\?days=/.test(rd("./realTradeApi.ts")), "records route");
  assert.ok(/export type Tab =[^;]*"history"/.test(rd("./components/realAuto/types.ts")), "Tab type");
  const tab = rd("./components/realAuto/tabs/HistoryTab.tsx");
  assert.ok(!/method:\s*"(POST|PUT|DELETE|PATCH)"|closePosition|cancelOrder|placeOrder/.test(tab), "History tab must be read-only");
  assert.ok(/mine !== seq\.current/.test(tab), "stale answers must be dropped");
});
console.log(`${passed} passed`);
