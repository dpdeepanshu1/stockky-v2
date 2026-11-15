// round 545 (E570, S5): guards for the proof, run log, weekly review and CSV download on the Self Improve System tab
// (run: node --experimental-strip-types src/selfImproveReview.test.mjs). Pure helpers are run; the rest are static checks on the source.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const v = await import("./selfImproveView.ts");
let n = 0; const ok = (name) => { n++; console.log("ok -", name); };

/* ---------- proof ---------- */
for (const s of ["ok", "waiting", "missed", "off", "weekend"]) assert.ok(v.proofLabel({ state: s }) && v.proofLabel({ state: s }) !== "UNKNOWN", s);
assert.equal(v.proofLabel(undefined), "UNKNOWN"); assert.equal(v.proofLabel({ state: "made_up" }), "UNKNOWN");
assert.equal(v.proofTone({ state: "ok" }), "good"); assert.equal(v.proofTone({ state: "missed" }), "bad"); assert.equal(v.proofTone({ state: "waiting" }), "warn"); assert.equal(v.proofTone({ state: "weekend" }), undefined); assert.equal(v.proofTone(null), undefined);
assert.equal(new Set(["ok", "waiting", "missed", "off", "weekend"].map((s) => v.proofLabel({ state: s }))).size, 5, "every state has its own words (not colour only)");
ok("proof: own words and tone per state, unknown stays unknown");

/* ---------- the missed warning follows the runner's proof, with the old rule as a fallback ---------- */
const base = (proof) => ({ now_ist: "2026-11-16 17:40", weekday: "Mon", banner: { state: "dry_run", text: "", unpinned: [] }, chain: [], schedule: { enabled: true, slots: [] },
  agent: { mode: "report", mode_effective: "report", stopped: null, run_after_ist: "16:10", next_run: { state: "scheduled", at_ist: null, reason: "" }, last: null, last_auto_date: null, run_now_allowed: true, market_open: false,
    keys: { gemini: true, groq: true }, usage_today: { left: 10, cap: 40, gemini_blocked: false }, scoreboard: {}, settings: {}, ...(proof ? { proof } : {}) } });
const P = (state, extra = {}) => ({ state, detail: "no agent run was saved today: no run started after 16:10 IST (the runner was down then, or the loop is stuck)", day: "2026-11-16", warn_after_ist: "17:30", runs_today: 0, scheduled_today: 0, ran_today: false, warned_today: false, ...extra });
assert.match(v.missedWarning(base(P("missed"))), /no run started after 16:10 IST.*warns by Telegram after 17:30 IST\)\./);
assert.match(v.missedWarning(base(P("missed", { warned_today: true }))), /already sent today/);
assert.equal(v.missedWarning(base(P("ok", { ran_today: true, runs_today: 1 }))), null);
assert.equal(v.missedWarning(base(P("waiting"))), null, "with a proof present the 60 minute rule does not fire early");
assert.match(v.missedWarning(base(null)), /passed over an hour ago/, "an older runner has no proof: the old rule stays");
assert.equal(v.healthWarnings(base(P("missed"))).filter((x) => /no run started/.test(x)).length, 1, "one warning, not two");
assert.equal(v.healthWarnings(base(P("ok", { ran_today: true }))).length, 0);
ok("health: the runner's proof drives the missed-run warning once; an older runner keeps the old rule");

/* ---------- run log ---------- */
assert.equal(v.runLogText({ at_ist: "2026-11-16 16:12:03", ok: true, trigger: "schedule", source: "gemini", ideas: 2, dropped: 1, queued: 0 }), "2026-11-16 16:12 · scheduled · gemini · 2 ideas, 1 dropped, 0 queued");
assert.equal(v.runLogText({ at_ist: "2026-11-16 18:40:00", ok: true, trigger: "manual", by: "admin", source: "", ideas: 1, dropped: 0, queued: 0, errors: 2 }), "2026-11-16 18:40 · manual by admin · no LLM answer · 1 idea, 0 dropped, 0 queued, 2 error(s)");
assert.match(v.runLogText({ at_ist: "2026-11-16 16:10:00", ok: false, trigger: "schedule", error: "crashed: boom" }), /scheduled · CRASHED: crashed: boom$/);
assert.match(v.runLogText({ at_ist: "2026-11-16 16:10:00", ok: false }), /CRASHED: unknown error/);
assert.doesNotMatch(v.runLogText({ at_ist: "x", ok: true }), /undefined|NaN/);
ok("run log: a scheduled, a manual and a crashed line read plainly, no undefined");

/* ---------- weekly ---------- */
for (const s of ["ran", "missed", "pending", "weekend", "off"]) assert.ok(v.weekMark(s) !== "?" && v.weekText(s), s);
assert.equal(new Set(["ran", "missed", "pending", "weekend", "off"].map(v.weekMark)).size, 5, "every state has its own mark");
assert.equal(v.weekTone("ran"), "good"); assert.equal(v.weekTone("missed"), "bad"); assert.equal(v.weekTone("pending"), "warn"); assert.equal(v.weekTone("weekend"), undefined);
assert.equal(v.weekDayLine({ date: "2026-11-16", weekday: "Mon", state: "ran", ideas: 2, dropped: 0, queued_n: 0 }), "Mon 11-16: ✓ ran · 2 ideas");
assert.equal(v.weekDayLine({ date: "2026-11-18", weekday: "Wed", state: "missed", ideas: 0, dropped: 0, queued_n: 0 }), "Wed 11-18: ✕ MISSED");
assert.equal(v.weekDayLine({ date: "2026-11-14", weekday: "Sat", state: "weekend", ideas: 0, dropped: 0, queued_n: 0 }), "Sat 11-14: – weekend");
assert.equal(v.weekMark("junk"), "?");
ok("weekly: a mark, words and tone per state; a day line reads plainly");

/* ---------- source checks ---------- */
const api = rd("./calibrationApi.ts");
assert.match(api, /agentWeekly: \(\) => R<WeeklyView>\("\/agent\/weekly"\)/);
assert.match(api, /downloadAgentCsv[\s\S]{0,400}\/agent\/history\.csv[\s\S]{0,200}Authorization: `Bearer \$\{anyToken\(\) \|\| ""\}`/, "the CSV is fetched with the bearer header and saved from a blob");
const tab = rd("./components/SelfImproveTab.tsx");
assert.ok(/runnerApi\.agentWeekly\(\)/.test(tab) && /Promise\.allSettled/.test(tab) && /if \(wk\.status === "fulfilled"\) setWeekly/.test(tab), "the weekly read is optional: a 404 from an older runner blanks nothing");
assert.ok(!/const bad = \[l, h, wk\]/.test(tab) && /const bad = \[l, h\]/.test(tab), "a failed weekly read is not an error banner");
const ideas = rd("./components/selfImprove/IdeasPanel.tsx"), today = rd("./components/selfImprove/TodayPanel.tsx");
assert.ok(/data-testid="si-weekly"/.test(ideas) && /Download CSV/.test(ideas) && /downloadAgentCsv\(\)\.catch/.test(ideas), "weekly card and CSV button on Ideas, a failed download shows its message");
assert.ok(/data-testid="si-proof"/.test(today) && /runLogText\(r\)/.test(today) && /aria-label="Run log"/.test(today));
for (const f of ["SelfImproveTab.tsx", "selfImprove/IdeasPanel.tsx", "selfImprove/TodayPanel.tsx"]) {
  const s = rd(`./components/${f}`);
  assert.ok(!/\/control\/|setSwitch|switchApi|PUT.*\/autocalib\/settings|autoSettings\(/.test(s), `${f}: still sets no trading switch`);
}
assert.ok(/"test": ".*selfImproveReview\.test\.mjs/.test(rd("../package.json")), "the new test is part of npm test");
ok("source: optional weekly read, bearer CSV download, weekly card, proof line, run log, no switch is set, wired into npm test");

console.log(`selfImproveReview.test.mjs: ${n} ok`);
