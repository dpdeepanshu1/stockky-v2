// Run: node --experimental-strip-types chartContext.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { normalizeCtx, windowStart, clipLine, niceTicks, priceRange, groupBadges, summaryLine, barChangePct, shortDate, toneOf, toneText } from "./chartContext.ts";

const bars = Array.from({ length: 40 }, (_, i) => [`2026-01-${String(i + 1).padStart(2, "0")}`, 100 + i, 102 + i, 99 + i, 101 + i, 1000]);
const ok = { ok: true, bars, markers: [], shapes: [], levels: [], badges: [
  { key: "regime_trend", kind: "regime", label: "Uptrend", tone: "good" }, { key: "bull_flag", kind: "chart", label: "Bull flag", tone: "good", status: "forming" },
  { key: "hammer", kind: "candle", label: "Hammer", tone: "good", bars_ago: 0 }, { key: "squeeze", kind: "signal", label: "Squeeze", tone: "warn" }], regime: { trend: "up" } };

// normalizeCtx: never throws, drops junk rows, keeps ok only with >= 5 usable bars
assert.equal(normalizeCtx(null).ok, false); assert.equal(normalizeCtx("x").ok, false); assert.equal(normalizeCtx({ ok: false }).ok, false);
assert.equal(normalizeCtx({ ok: true, bars: bars.slice(0, 3) }).ok, false);
const n = normalizeCtx({ ...ok, bars: [...bars, ["bad", NaN, 1, 1, 1, 1], "junk", null] });
assert.equal(n.ok, true); assert.equal(n.bars.length, 40); assert.equal(n.regime.volatility, null); assert.deepEqual(n.markers, []);
assert.equal(normalizeCtx({ ...ok, shapes: [{ label: "x" }, { label: "y", lines: [] }] }).shapes.length, 1);

// window + clipping
assert.equal(windowStart(100, 63), 37); assert.equal(windowStart(10, 63), 0); assert.equal(windowStart(100, 1), 95); assert.equal(windowStart(NaN, 5), 0);
const dates = bars.map((b) => b[0]);
const l = clipLine({ role: "neckline", x1: dates[2], y1: 100, x2: dates[30], y2: 128 }, dates, 16);
assert.equal(l.i1, 16); assert.ok(Math.abs(l.y1 - 114) < 1e-9); assert.equal(l.i2, 30);
assert.equal(clipLine({ role: "x", x1: dates[2], y1: 1, x2: dates[10], y2: 2 }, dates, 16), null);        // ends before the window
assert.equal(clipLine({ role: "x", x1: "nope", y1: 1, x2: dates[10], y2: 2 }, dates, 0), null);           // unknown date
assert.equal(clipLine({ role: "x", x1: dates[0], y1: NaN, x2: dates[10], y2: 2 }, dates, 0), null);

// ticks + range
assert.deepEqual(niceTicks(0, 100, 4), [0, 20, 40, 60, 80, 100]); assert.deepEqual(niceTicks(5, 5, 4), []); assert.deepEqual(niceTicks(NaN, 1, 4), []);
const r = priceRange(bars.slice(-10), [95, 5000, 143]);       // 95 and 5000 are far off-scale and ignored; 143 is near
assert.ok(r.min < 129 && r.min > 120 && r.max > 143 && r.max < 150);
assert.equal(priceRange([], []), null);

// badges + summary
const g = groupBadges([...ok.badges, { key: "z", kind: "weird", label: "Odd", tone: "mute" }]);
assert.deepEqual(g.map((x) => x.kind), ["regime", "chart", "candle", "signal"]); assert.equal(g[3].items.length, 2);
assert.deepEqual(groupBadges(null), []);
assert.equal(summaryLine(normalizeCtx(ok)), "Uptrend · Bull flag (forming) · Hammer today");
assert.equal(summaryLine(null), ""); assert.equal(summaryLine({ ok: false }), "");

// small helpers
assert.equal(barChangePct(bars, 0), null); assert.ok(Math.abs(barChangePct(bars, 1) - (101 / 101 * 100 + 0) + 100) < 5);
assert.equal(shortDate("2026-10-22"), "22 Oct"); assert.equal(shortDate("x"), "x"); assert.equal(shortDate(""), "");
assert.equal(toneOf("weird"), "mute"); assert.equal(toneText("good"), "text-signal-buy");
console.log("chartContext: all assertions passed");

// ---- E27: timeframes (1m / 5m / 15m / 30m / 1h / 1d)
import { TIMEFRAMES, isTf, tfInfo, windowsFor, defaultWindow, windowKeyFor, agoText, isIntradayLabel, dateOf, timeOf, barLabel, axisLabel, dayBreaks, sessionChangePct, marketHoursIST, missingText } from "./chartContext.ts";

assert.deepEqual(TIMEFRAMES.map((t) => t.key), ["1m", "5m", "15m", "30m", "1h", "1d"]);
assert.equal(isTf("5m"), true); assert.equal(isTf("7m"), false); assert.equal(isTf(null), false);
assert.equal(tfInfo("nope").key, "1d"); assert.equal(tfInfo("15m").intraday, true); assert.equal(tfInfo("1d").refreshMs, 0);
// every intraday cache entry expires before the next scheduled re-read, so a refresh really fetches
for (const t of TIMEFRAMES.filter((x) => x.intraday)) assert.ok(t.ttlMs < t.refreshMs, t.key);

// zoom windows: daily keeps its four, every intraday timeframe has its own, defaults always exist
assert.deepEqual(windowsFor("1d").map((w) => w.key), ["1m", "3m", "6m", "1y"]);
for (const t of TIMEFRAMES) { assert.ok(windowsFor(t.key).some((w) => w.key === defaultWindow(t.key)), t.key); assert.equal(windowKeyFor(t.key, "zzz"), defaultWindow(t.key)); }
assert.equal(windowKeyFor("5m", "5d"), "5d"); assert.equal(windowKeyFor("5m", "3m"), "2d");
assert.equal(windowsFor("1m").find((w) => w.key === "1d").bars, 375);          // one NSE session of 1-minute candles
assert.equal(windowsFor("5m").find((w) => w.key === "1d").bars, 75);

// normalizeCtx keeps the forming bar and the timeframe, and a reason for a miss
const intra = Array.from({ length: 40 }, (_, i) => [`2026-10-22 ${String(9 + Math.floor((15 + i * 5) / 60)).padStart(2, "0")}:${String((15 + i * 5) % 60).padStart(2, "0")}`, 100 + i, 102 + i, 99 + i, 101 + i, 1000]);
const ni = normalizeCtx({ ok: true, tf: "5m", bars: intra, live: ["2026-10-22 12:35", 1, 2, 1, 2, 5], data_lag_s: 95.6, note: "noisy", markers: [], shapes: [], levels: [], badges: [] });
assert.equal(ni.ok, true); assert.equal(ni.tf, "5m"); assert.equal(ni.unit, "candle"); assert.deepEqual(ni.live, ["2026-10-22 12:35", 1, 2, 1, 2, 5]); assert.equal(ni.data_lag_s, 96); assert.equal(ni.note, "noisy");
assert.equal(normalizeCtx({ ok: true, tf: "5m", bars: intra, live: ["x", NaN, 1, 1, 1, 1] }).live, null);
const nd = normalizeCtx({ ...ok, live: ["2026-01-41", 1, 2, 1, 2, 5] });
assert.equal(nd.tf, "1d"); assert.equal(nd.unit, "day"); assert.equal(nd.live, null);       // daily never has a forming bar
const miss = normalizeCtx({ ok: false, tf: "1m", reason: "thin" });
assert.equal(miss.ok, false); assert.equal(miss.reason, "thin"); assert.equal(miss.tf, "1m"); assert.equal(normalizeCtx({ ok: false }).reason, undefined);

// wording
assert.equal(agoText(0, "1d"), "today"); assert.equal(agoText(2, "1d"), "2d ago"); assert.equal(agoText(0, "5m"), "latest candle"); assert.equal(agoText(1, "5m"), "1 candle ago"); assert.equal(agoText(4, "1h"), "4 candles ago");
assert.equal(summaryLine({ ...ni, badges: [{ key: "hammer", kind: "candle", label: "Hammer", tone: "good", bars_ago: 0 }] }), "Hammer latest candle");
assert.match(missingText("INFY", "5m", "thin"), /5-minute candles are not available for INFY.*too few/); assert.match(missingText("INFY", "1d"), /daily history too short/);
assert.match(missingText("INFY", "1m", "http_503"), /busy or slow/);

// labels, axis and session breaks
assert.equal(isIntradayLabel("2026-10-22 09:15"), true); assert.equal(isIntradayLabel("2026-10-22"), false);
assert.equal(dateOf("2026-10-22 09:15"), "2026-10-22"); assert.equal(timeOf("2026-10-22 09:15"), "09:15"); assert.equal(timeOf("2026-10-22"), "");
assert.equal(barLabel("2026-10-22 14:35"), "22 Oct 14:35"); assert.equal(barLabel("2026-10-22"), "22 Oct");
assert.equal(axisLabel("2026-10-22 09:15"), "22 Oct 09:15"); assert.equal(axisLabel("2026-10-22 10:30", "2026-10-22 09:15"), "10:30"); assert.equal(axisLabel("2026-10-23 09:15", "2026-10-22 15:15"), "23 Oct"); assert.equal(axisLabel("2026-10-22", "2026-10-21"), "22 Oct");
const lab = ["2026-10-21 15:25", "2026-10-21 15:29", "2026-10-22 09:15", "2026-10-22 09:16", "2026-10-23 09:15"];
assert.deepEqual(dayBreaks(lab), [2, 4]); assert.deepEqual(dayBreaks(lab, 3), [4]); assert.deepEqual(dayBreaks(["2026-10-21", "2026-10-22"]), []); assert.deepEqual(dayBreaks([]), []);
const sb = [["2026-10-21 15:29", 1, 1, 1, 100, 1], ["2026-10-22 09:15", 1, 1, 1, 102, 1], ["2026-10-22 09:16", 1, 1, 1, 99, 1]];
assert.equal(sessionChangePct(sb, 0), null); assert.ok(Math.abs(sessionChangePct(sb, 1) - 2) < 1e-9); assert.ok(Math.abs(sessionChangePct(sb, 2) + 1) < 1e-9); assert.equal(sessionChangePct([["2026-10-22", 1, 1, 1, 1, 1]], 0), null);

// market hours (IST): Thu 22 Oct 2026 10:00 IST = 04:30 UTC; Sat is closed
const ist = (iso) => Date.parse(iso + "+05:30");
assert.equal(marketHoursIST(ist("2026-10-22T10:00:00")), true); assert.equal(marketHoursIST(ist("2026-10-22T09:14:00")), false); assert.equal(marketHoursIST(ist("2026-10-22T15:35:00")), true);
assert.equal(marketHoursIST(ist("2026-10-22T15:36:00")), false); assert.equal(marketHoursIST(ist("2026-10-24T11:00:00")), false); assert.equal(marketHoursIST(NaN), false);
console.log("chartContext E27 ok");
