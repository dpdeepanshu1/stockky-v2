// round 554 (P5): the end-of-day F&O pack's words. A missing number is a dash, an old pack says it is old, every screen says which close it is.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { BUILDUP_LIST, basisList, buildupLabel, countLine, expiryText, flowList, intText, packHead, pct1Text, plainPct, priceText, rolloverList, signedIntText } from "./foEodView.ts";

const ok = (m) => console.log("ok - " + m);
const MINUS = "−";

assert.equal(intText(1234567), "12,34,567", "Indian grouping");
assert.equal(signedIntText(1500), "+1,500");
assert.equal(signedIntText(-1500), `${MINUS}1,500`);
assert.equal(signedIntText(0), "0", "a real zero is shown as 0");
assert.equal(pct1Text(11.24), "+11.2%");
assert.equal(pct1Text(-3.06), `${MINUS}3.1%`);
assert.equal(pct1Text(0.01), "0.0%");
assert.equal(plainPct(23.14), "23.1%");
assert.equal(priceText(2512.5), "2,512.50");
for (const bad of [null, undefined, NaN, Infinity, "x"]) for (const f of [intText, signedIntText, pct1Text, plainPct]) assert.equal(f(bad), "—", `${f.name}(${bad}) is a dash, never 0`);
for (const bad of [null, undefined, NaN, 0, -2]) assert.equal(priceText(bad), "—");
assert.notEqual(signedIntText(-5)[0], "-", "a negative number never starts with a hyphen that could read as the dash");
ok("numbers: real minus sign, missing is a dash, zero price is a dash");

assert.equal(expiryText("2026-11-24", 11), "24 Nov (11 d)");
assert.equal(expiryText("2026-11-13", 0), "13 Nov (today)");
assert.equal(expiryText("2026-11-24", null), "24 Nov");
for (const bad of [null, "", "24 Nov", undefined]) assert.equal(expiryText(bad, 3), "—");
assert.equal(buildupLabel("short_covering"), "Short covering");
assert.equal(buildupLabel(null), "No clear read");
assert.equal(buildupLabel("nonsense"), "No clear read");
ok("expiry and labels");

const base = { today_ist: "2026-11-15", enabled: true, trade_date: "2026-11-13", expected_date: "2026-11-13", stale: false };
let h = packHead(base, false);
assert.deepEqual([h.tone, h.showDate], ["ok", "13 Nov"]);
assert.match(h.sub, /Close of 13 Nov\. Read from the exchange's F&O file, not live\./);
h = packHead({ ...base, trade_date: "2026-11-12", stale: true }, false);
assert.equal(h.tone, "stale");
assert.match(h.sub, /Latest file \(close of 13 Nov\) is not out yet\. Showing the close of 12 Nov/);
h = packHead({ ...base, trade_date: "2026-11-12", stale: false }, false);          // the page works it out too, in case an old gateway forgot the flag
assert.equal(h.tone, "stale");
h = packHead({ ...base, trade_date: "2026-11-09", expected_date: "2026-11-13", stale: true }, true);
assert.match(h.sub, /\(6 days ago\)/);
assert.match(h.sub, /Refresh failed/);
h = packHead({ ...base, trade_date: null, error: "no file found for the recent sessions" }, false);
assert.deepEqual([h.tone, h.showDate], ["none", ""]);
assert.match(h.sub, /No file yet\. no file found/);
assert.match(packHead({ enabled: false }, false).sub, /Switched off/);
assert.match(packHead(null, true).sub, /Could not load/);
assert.equal(packHead(null, false).sub, "Loading");
assert.equal(packHead({ ...base, trade_date: "not a date" }, false).tone, "none", "a pack without a real date shows nothing as current");
ok("the header always names the close, and says when it is not the latest");

const R = (symbol, o) => ({ symbol, ...o });
const rows = [
  R("A", { buildup: "long_buildup", oi_chg_pct: 5, basis_pct: 0.8, roll_pct: 20, rolling: false }),
  R("B", { buildup: "long_buildup", oi_chg_pct: 12, basis_pct: -0.4, roll_pct: 55, rolling: true }),
  R("C", { buildup: "long_buildup", oi_chg_pct: 9, basis_pct: 1.5, roll_pct: 70, rolling: false }),
  R("D", { buildup: "short_buildup", oi_chg_pct: -2 }),                       // a short build-up with a negative OI % is not possible: still sorted by size, not shown wrongly
  R("E", { buildup: "long_unwinding", oi_chg_pct: -7, basis_pct: -1.1 }),
  R("F", { buildup: null, oi_chg_pct: 30, basis_pct: 0 }),
  R("G", { buildup: "long_buildup", oi_chg_pct: null }),
  R("H", { buildup: "short_covering", oi_chg_pct: -3, basis_pct: null, roll_pct: null }),
];
let f = flowList(rows, "long_buildup");
assert.deepEqual(f.rows.map((r) => r.symbol), ["B", "C", "A"], "biggest OI change first; a row with no OI % is left out, not sorted as 0");
assert.equal(f.total, 3);
assert.deepEqual(flowList(rows, "long_buildup", 2).rows.map((r) => r.symbol), ["B", "C"]);
assert.equal(flowList(rows, "long_buildup", 2).total, 3, "total is the full count, not the cut");
assert.deepEqual(flowList(rows, "long_unwinding").rows.map((r) => r.symbol), ["E"]);
assert.deepEqual(flowList(undefined, "long_buildup"), { rows: [], total: 0 });
assert.deepEqual(flowList(rows, "long_buildup", 0).rows, []);
assert.ok(!flowList(rows, "long_buildup").rows.some((r) => r.symbol === "F"), "a flat-price stock with no label is in none of the four lists");
ok("flow lists");

assert.deepEqual(basisList(rows, "premium").rows.map((r) => r.symbol), ["C", "A"]);
assert.deepEqual(basisList(rows, "discount").rows.map((r) => r.symbol), ["E", "B"], "most negative first");
assert.equal(basisList(rows, "premium").rows.some((r) => r.symbol === "F" || r.symbol === "H"), false, "zero basis is in neither list; a missing basis is in neither list");
assert.deepEqual(basisList(null, "premium"), { rows: [], total: 0 });
ok("basis lists");

assert.deepEqual(rolloverList(rows).rows.map((r) => r.symbol), ["B", "C", "A"], "rolling stocks first, then by share");
assert.equal(rolloverList(rows).rows.some((r) => r.symbol === "H"), false, "no share = not listed");
ok("rollover list");

assert.equal(countLine({ stocks: 180, long_buildup: 40, short_covering: 20, short_buildup: 30, long_unwinding: 25 }), "Long build-up 40 · Short covering 20 · Short build-up 30 · Long unwinding 25 · of 180 stocks");
assert.equal(countLine({ stocks: 10 }), "Long build-up — · Short covering — · Short build-up — · Long unwinding — · of 10 stocks");
assert.equal(countLine(undefined), "—");
assert.equal(BUILDUP_LIST.length, 4);
ok("count line");

// display only: the page reads, never writes; it lives under the F&O page as its own section and touches no engine file
const page = readFileSync(new URL("./components/fno/FnoEod.tsx", import.meta.url), "utf8").replace(/^\s*\/\/.*$/gm, "");
assert.ok(!/\/control\/|method:\s*"(POST|PUT|DELETE|PATCH)"|fnoApi|runnerApi|setSwitch|arm/i.test(page), "the page only reads");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");
assert.match(api, /foEod:[^\n]*\n?[^\n]*\/market\/fo-eod/);
const tab = readFileSync(new URL("./components/fno/FnoTab.tsx", import.meta.url), "utf8");
assert.match(tab, /\{ id: "eod", label: "End of day" \}/);
assert.match(tab, /\{s === "eod" && <FnoEod \/>\}/);
ok("display only, wired into the F&O page");
