// Run: node --experimental-strip-types src/sessionView.test.mjs  (Node >= 22.6). E277: trading-day rail, pre-market plan card, setup ladder, labels.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { sessionView, premarketPlanView, PS_SLOTS, RT_SLOTS, istDay } from "./sessionView.ts";
import { setupLadderView, SETUP_CATALOG } from "./setupView.ts";
import { ladderView } from "./v7View.ts";
import { swingView } from "./swingView.ts";
const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const at = (hhmm, day = "2026-11-02") => new Date(`${day}T${hhmm}:00+05:30`);   // 2 Nov 2026 is a Monday

// slots are in time order and match the plan
for (const l of [PS_SLOTS, RT_SLOTS]) for (let i = 1; i < l.length; i++) assert.ok(l[i].mins > l[i - 1].mins, "slots ascend");
assert.deepEqual(PS_SLOTS.map((s) => s.at), ["08:30", "09:05", "09:12", "09:15", "09:21", "09:30", "09:45", "14:00", "14:55"]);
assert.equal(RT_SLOTS.at(-1).at, "15:00");

// states through the day
let v = sessionView("ps", at("08:00"));
assert.equal(v.active, null); assert.equal(v.pct, 0); assert.match(v.words, /Before the day's plan/);
v = sessionView("ps", at("09:16"));
assert.equal(v.active, "observe"); assert.deepEqual(v.slots.map((s) => s.state).slice(0, 6), ["done", "done", "done", "now", "next", "later"]);
v = sessionView("ps", at("09:21")); assert.equal(v.active, "s1s4");
v = sessionView("rt", at("10:30")); assert.equal(v.active, "s3");
assert.ok(v.pct > 0 && v.pct < 100);
v = sessionView("ps", at("15:00")); assert.equal(v.active, "flat"); assert.equal(v.pct, 100);
v = sessionView("ps", at("16:00")); assert.equal(v.active, null); assert.ok(v.slots.every((s) => s.state === "done")); assert.match(v.words, /closed for the day/);
v = sessionView("ps", at("10:00", "2026-11-01"));   // Sunday
assert.equal(v.weekday, false); assert.equal(v.nowMins, null); assert.equal(v.pct, 0); assert.match(v.words, /Weekend/);
assert.doesNotThrow(() => sessionView("ps", new Date(NaN)));

// pre-market plan card
const now = at("09:00");
assert.equal(premarketPlanView(null, now).kind, "unavailable");
assert.equal(premarketPlanView({ ok: false, error: "x" }, now).kind, "unavailable");
assert.equal(premarketPlanView({ ok: true, enabled: false }, now).kind, "off");
assert.equal(premarketPlanView({ ok: true, enabled: true, day: null, builds: 0 }, now).kind, "waiting");
assert.equal(premarketPlanView({ ok: true, enabled: true, day: "2026-10-30", builds: 3 }, now).kind, "stale");
const ok = premarketPlanView({ ok: true, enabled: true, day: istDay(now), builds: 4, built_at: "2026-11-02T08:35:10+05:30", risk_off: true, vix: "calm", events: { positive: 3, negative: 1 }, notes: ["a", 5, "b"] }, now);
assert.equal(ok.kind, "ready"); assert.equal(ok.tone, "warn"); assert.equal(ok.tiles.length, 5); assert.deepEqual(ok.notes, ["a", "b"]);
assert.equal(ok.tiles.find((t) => t.label === "Last build").value, "08:35 IST");
assert.equal(premarketPlanView({ ok: true, day: istDay(now), builds: 1, risk_off: null }, now).tiles[0].value, "unknown");

// setup ladder
assert.equal(SETUP_CATALOG.length, 4);
let lad = setupLadderView(null); assert.equal(lad.cards.length, 4); assert.ok(lad.cards.every((c) => c.stage === "building"));
lad = setupLadderView({ ok: true, min_trades: 30, ladder_enabled: true, promoted: ["S1_CATALYST_UP|0920"], demoted: ["S2_RS_BREAKOUT|0930"],
  cells: { "S3_EXHAUSTION|ALL": { n: 31 }, "LEGACY_SHORTLIST|ALL": { n: 12 } } });
const by = Object.fromEntries(lad.cards.map((c) => [c.short, c.stage]));
assert.deepEqual(by, { S1: "promoted", S4: "building", S2: "demoted", S3: "starting" }); assert.equal(lad.legacyClosed, 12);
assert.match(setupLadderView({ ok: true, ladder_enabled: false }).words, /OFF/);

// labels: swing lane is live or off only; its ladder has two rungs
assert.equal(swingView({ mode: "shadow" }).kind, "off"); assert.equal(swingView({ mode: "live" }).kind, "live");
assert.deepEqual(ladderView("shadow", "x", true).steps.map((s) => s.id), ["off", "live"]);
assert.deepEqual(ladderView("live", "x").steps.map((s) => s.id), ["off", "shadow", "live"]);
for (const f of ["./components/calibration/SwingPanel.tsx", "./swingView.ts"]) assert.ok(!/Shadow mode|\(shadow:|Shadow\. The lane/.test(rd(f)), `${f} still says shadow mode`);
assert.ok(/twoStep/.test(rd("./components/calibration/SwingPanel.tsx")));

// mounts
assert.ok(/<SessionRail service="ps"/.test(rd("./components/PositionStocksTab.tsx")) && /<PremarketPlanCard/.test(rd("./components/PositionStocksTab.tsx")));
assert.ok(/<SessionRail service="rt"/.test(rd("./components/realAuto/tabs/OverviewTab.tsx")));
assert.ok(/<Card title="Setup ladder"/.test(rd("./components/calibration/SetupScorecardPanel.tsx")));
for (const f of ["./components/session/SessionRail.tsx", "./components/session/PremarketPlanCard.tsx"]) assert.ok(!/\.post\(|method:\s*"POST"|onClick/.test(rd(f)), `${f} must place nothing`);
console.log("sessionView.test.mjs: ok");
