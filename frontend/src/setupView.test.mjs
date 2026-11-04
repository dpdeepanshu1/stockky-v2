// Run: node --experimental-strip-types setupView.test.mjs  (Node >= 22.6). E272: entry-reason helper, setup scorecard helper and their wiring.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { entryReason, scorecardView, setupName, slotLabel, LEGACY_ID } from "./setupView.ts";

const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

// names and slots
assert.equal(setupName(LEGACY_ID), "Shortlist (old rule)");
assert.equal(setupName("S2_PULLBACK"), "S2 PULLBACK");
assert.equal(setupName(""), "-");
assert.equal(setupName(null), "-");
assert.equal(slotLabel("0920"), "09:20");
assert.equal(slotLabel("1000"), "10:00");
assert.equal(slotLabel("nonsense"), "later");
assert.equal(slotLabel(undefined), "later");

// entry reason
const none = entryReason(null);
assert.equal(none.known, false);
assert.equal(none.label, "-");
assert.match(none.title, /before entries were stamped/);
assert.equal(entryReason({}).known, false, "a stamp without an id is not a stamp");
assert.equal(entryReason("x").known, false);
const legacy = entryReason({ id: LEGACY_ID, trigger: "", confirmations: [], penalties: {}, size_mult: null, slot: "0930", side: "LONG" });
assert.equal(legacy.known, true);
assert.equal(legacy.label, "No setup matched");
assert.equal(legacy.slot, "09:30");
assert.equal(entryReason({ id: LEGACY_ID, trigger: "BUY NOW", slot: "later" }).label, "BUY NOW (no setup)");
assert.equal(entryReason({ id: LEGACY_ID, trigger: "BUY NOW", slot: "later" }).slot, "after 10:30");
assert.match(entryReason({ id: LEGACY_ID }).title, /No new setup/);
assert.match(legacy.title, /baseline/);
assert.ok(!/Size x/.test(legacy.title), "no size line when the multiplier is unknown");
const s2 = entryReason({ id: "S2_PULLBACK", trigger: "VWAP reclaim", confirmations: ["volume", "nifty"], penalties: { gap: -0.1 }, size_mult: 0.5, slot: "0920", side: "SHORT" });
assert.match(s2.title, /Trigger: VWAP reclaim\./);
assert.match(s2.title, /Confirmations: volume, nifty\./);
assert.match(s2.title, /Penalties: gap -0\.1\./);
assert.match(s2.title, /Size x0\.5\./);
assert.match(s2.title, /slot 09:20, short/);
// hostile shapes never throw
assert.doesNotThrow(() => entryReason({ id: "X", confirmations: "no", penalties: 5, size_mult: "a" }));

// scorecard
assert.equal(scorecardView(null).kind, "unavailable");
assert.equal(scorecardView({ ok: false, error: "DatabaseError" }).headline.includes("DatabaseError"), true);
assert.equal(scorecardView({}).kind === "unavailable" || scorecardView({}).kind === "empty", true);
const empty = scorecardView({ ok: true, rows_read: 12, min_trades: 30, cells: {} });
assert.equal(empty.kind, "empty");
assert.match(empty.headline, /12 settled trades read, none carries a setup stamp/);
assert.match(scorecardView({ ok: true, rows_read: 0, cells: {} }).headline, /nothing to score/);

const cell = (o = {}) => ({ n: 12, wins: 7, win_rate: 0.5833, r_n: 12, avg_r: 0.31, lower_bound_r: null, verdict: "NOT_ENOUGH_DATA", ...o });
const doc = { ok: true, rows_read: 40, min_trades: 30, cells: {
  "S2_PULLBACK|0930": cell({ n: 35, verdict: "POSITIVE", lower_bound_r: 0.12 }),
  "S2_PULLBACK|ALL": cell({ n: 35, wins: 21, win_rate: 0.6, avg_r: 0.4, lower_bound_r: 0.12, verdict: "POSITIVE" }),
  "LEGACY_SHORTLIST|0920": cell(),
  "LEGACY_SHORTLIST|ALL": cell({ n: 40, wins: 16, win_rate: 0.4, avg_r: -0.05, lower_bound_r: -0.2, verdict: "NOT_PROVEN" }),
  "LEGACY_SHORTLIST|later": cell({ n: 3 }),
} };
const v = scorecardView(doc);
assert.equal(v.kind, "ok");
assert.deepEqual(v.rows.map((r) => r.key), ["LEGACY_SHORTLIST|ALL", "LEGACY_SHORTLIST|0920", "LEGACY_SHORTLIST|later", "S2_PULLBACK|ALL", "S2_PULLBACK|0930"], "the baseline first, then each setup: all-day row, then slots in clock order");
assert.equal(v.rows[0].winPct, 40);
assert.equal(v.rows[0].tone, "warn");
assert.equal(v.rows[0].verdictText, "not proven");
assert.equal(v.rows[3].tone, "good");
assert.equal(v.rows[3].lowerR, 0.12);
assert.equal(v.rows[1].progressPct, 40, "12 of 30 trades");
assert.equal(v.rows[2].progressPct, 10);
assert.equal(v.rows[1].tone, undefined, "not enough data has no tone: it can never look like a pass");
assert.match(v.headline, /1 of 2 judged setups positive/);
assert.equal(v.tone, "good");
assert.ok(v.warnings.some((w) => /deleted after a few days/.test(w)));
// nothing judged -> a plain warning and no positive tone
const none30 = scorecardView({ ok: true, rows_read: 5, min_trades: 30, cells: { "LEGACY_SHORTLIST|ALL": cell({ n: 5 }) } });
assert.match(none30.headline, /1 setup seen, none with 30 trades yet/);
assert.equal(none30.tone, "warn");
assert.ok(none30.warnings.some((w) => /none can be promoted/.test(w)));
// a bad cell is skipped, a bad number is null
const bad = scorecardView({ ok: true, rows_read: 1, cells: { "A|ALL": null, "B|ALL": { n: "x", verdict: 5, win_rate: NaN } } });
assert.equal(bad.rows.length, 1);
assert.equal(bad.rows[0].winPct, null);

// wiring
const api = rd("./positionStocksApi.ts");
assert.match(api, /setupStats: \(\) => psRequest<SetupStatsDoc>\("\/setup\/stats", undefined, true\)/);
assert.match(api, /setup\?: SetupStamp \| null/);
const tab = rd("./components/PositionStocksTab.tsx");
assert.match(tab, /<th className="py-1 pr-3">Entry reason<\/th>/, "history table has the column");
assert.match(tab, /entryReason\(t\.setup\)/);
assert.match(tab, /entryReason\(p\.setup\)/, "open position card shows it too");
const cal = rd("./components/CalibrationControlTab.tsx");
assert.match(cal, /section === "setups" && <SetupScorecardPanel svc=\{svc\} \/>/);
assert.match(cal, /section !== "setups"/, "no Run-all bar on the setups section");
assert.match(cal, /\{ id: "setups", label: "Setup scorecard" \}/);
const panel = rd("./components/calibration/SetupScorecardPanel.tsx");
assert.match(panel, /scorecardView\(doc\)/);
assert.match(panel, /positionStocksApi\.setupStats\(\)/);
assert.ok(!/realTradeApi/.test(panel), "Real Trade has no setup stamp yet, so the card never calls it");
console.log("setupView ok");
