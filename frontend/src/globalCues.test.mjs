// round 553 (P4): the global cues strip's words. A missing number is a dash, an old one is never shown as current, a number with no date is not shown.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { ageDays, clockText, CUE_LIST, cueTile, dayText, globalCueTiles, pctText, priceText, stripSub, STALE_AFTER_DAYS } from "./globalCuesView.ts";

const ok = (m) => console.log("ok - " + m);
const MINUS = "−";

assert.equal(dayText("2026-11-13"), "13 Nov");
for (const bad of [null, undefined, "", "13 Nov", "2026-13-01", "2026-00-10", 5]) assert.equal(dayText(bad), "");
assert.equal(ageDays("2026-11-13", "2026-11-15"), 2);
assert.equal(ageDays("2026-11-15", "2026-11-15"), 0);
assert.equal(ageDays("2026-11-16", "2026-11-15"), 0, "a bar dated after today is not negative");
assert.equal(ageDays("2026-11-13", "bad"), null);
ok("dates and ages");

assert.equal(pctText(0.4249), "+0.42%");
assert.equal(pctText(-0.4249), `${MINUS}0.42%`);
assert.equal(pctText(0), "0.00%");
assert.equal(pctText(0.001), "0.00%", "a move that rounds to zero carries no sign");
assert.equal(pctText(-0.001), "0.00%");
for (const bad of [null, undefined, NaN, Infinity, "x"]) assert.equal(pctText(bad), "—");
assert.notEqual(pctText(-1)[0], "-", "a negative number never starts with a dash");
assert.equal(priceText(5921.5), "5,921.50");
for (const bad of [null, undefined, NaN, 0, -3, "x"]) assert.equal(priceText(bad), "—", "zero or negative price is a dash, never 0.00");
assert.equal(clockText("2026-11-15T14:32:10+05:30"), "14:32");
assert.equal(clockText(null), "");
ok("numbers: real minus sign, a missing or zero price is a dash");

const TODAY = "2026-11-15";
const sp = CUE_LIST[0], brent = CUE_LIST[2];
assert.deepEqual(cueTile(sp, { price: 6000, day_change_pct: 0.5, bar_date: "2026-11-15" }, TODAY), { key: "^GSPC", label: "S&P 500", value: "6,000.00", delta: "+0.50%", tone: "up", sub: "15 Nov, today", state: "ok" });
assert.equal(cueTile(sp, { price: 6000, day_change_pct: -0.5, bar_date: "2026-11-13" }, TODAY).sub, "last close 13 Nov");
assert.equal(cueTile(sp, { price: 6000, day_change_pct: -0.5, bar_date: "2026-11-13" }, TODAY).tone, "down");
assert.equal(cueTile(brent, { price: 80, day_change_pct: 3, bar_date: "2026-11-15" }, TODAY).tone, "flat", "crude up is not green: no good / bad colour on a neutral cue");
assert.equal(cueTile(brent, { price: 80, day_change_pct: 3, bar_date: "2026-11-15" }, TODAY).delta, "+3.00%");
ok("a live tile always says which bar it is from");

const old = cueTile(sp, { price: 6000, day_change_pct: 1, bar_date: "2026-11-10" }, TODAY);
assert.equal(ageDays("2026-11-10", TODAY), STALE_AFTER_DAYS);
assert.deepEqual([old.value, old.delta, old.state, old.sub], ["—", undefined, "stale", "stale, last 10 Nov"]);
assert.equal(cueTile(sp, { price: 6000, day_change_pct: 1, bar_date: "2026-11-11" }, TODAY).state, "ok", "4 days (a long weekend) still shows, labelled as the last close");
const nodate = cueTile(sp, { price: 6000, day_change_pct: 1, bar_date: "" }, TODAY);
assert.deepEqual([nodate.value, nodate.state], ["—", "missing"]);
assert.equal(cueTile(sp, { price: 6000, day_change_pct: 1, bar_date: "2026-11-15" }, "").state, "missing", "without a today date nothing can be called current");
for (const row of [undefined, { price: 0, day_change_pct: 1, bar_date: TODAY }, { price: NaN, day_change_pct: 1, bar_date: TODAY }, { price: 5, day_change_pct: null, bar_date: TODAY }]) {
  const t = cueTile(sp, row, TODAY);
  assert.deepEqual([t.value, t.state, t.delta], ["—", "missing", undefined]);
}
ok("old, undated, zero or non-numeric rows are dashes, never a number");

const tiles = globalCueTiles({ today_ist: TODAY, rows: { "^GSPC": { price: 6000, day_change_pct: 0.5, bar_date: "2026-11-14" } } });
assert.equal(tiles.length, 6);
assert.deepEqual(tiles.map((t) => t.key), ["^GSPC", "^IXIC", "BZ=F", "DX-Y.NYB", "INR=X", "GC=F"]);
assert.deepEqual(tiles.map((t) => t.state), ["ok", "missing", "missing", "missing", "missing", "missing"]);
assert.equal(globalCueTiles(null).every((t) => t.value === "—"), true);
assert.equal(globalCueTiles({ rows: null }).length, 6);
ok("six tiles in fixed order; no body means six dashes");

assert.equal(stripSub({ fetched_at: "2026-11-15T14:32:10+05:30" }, false), "Read 14:32 IST from Yahoo daily bars");
assert.match(stripSub({ fetched_at: "2026-11-15T14:32:10+05:30" }, true), /Refresh failed, showing the last read/);
assert.match(stripSub(null, true), /Could not load/);
assert.match(stripSub({ enabled: false }, false), /Switched off/);
assert.match(stripSub({ fetched_at: "2026-11-15T14:32:10+05:30", error: "market-data unreachable" }, false), /market-data unreachable/);
ok("the line under the title says when it was read, or why not");

// display only: the strip never reaches a trading service, a switch or a Market Weather dial
const strip = readFileSync(new URL("./components/GlobalCuesStrip.tsx", import.meta.url), "utf8").replace(/^\s*\/\/.*$/gm, "");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");
assert.match(api, /globalCues:[^\n]*\n?[^\n]*\/market\/global-cues/);
assert.ok(!/\/control\/|method:\s*"(POST|PUT|DELETE|PATCH)"|runnerApi|overlay|weatherSettings/.test(strip), "the strip only reads");
const home = readFileSync(new URL("./components/app/DashboardView.tsx", import.meta.url), "utf8");
assert.match(home, /<MarketSentimentHeader \/>\s*<GlobalCuesStrip \/>/, "on Home, right under Market today");
const weather = readFileSync(new URL("./components/calibration/WeatherPanel.tsx", import.meta.url), "utf8");
assert.match(weather, /<GlobalCuesStrip \/>/, "on the Market Weather page");
assert.ok(!/GlobalCues|globalCue/.test(readFileSync(new URL("./weatherView.ts", import.meta.url), "utf8")), "the weather dials are still set only by the runner's own overlay");
ok("display only, placed on Home and the Market Weather page");
