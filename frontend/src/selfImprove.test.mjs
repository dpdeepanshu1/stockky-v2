// round 542 (E567, S2): guards for the Self Improve System tab (run: node --experimental-strip-types src/selfImprove.test.mjs).
// Pure helpers are run; the rest are static checks on the source: read-only views, the second click for propose, no key, no switch is set.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const v = await import("./selfImproveView.ts");
const nav = await import("./components/app/navModel.ts");
let n = 0; const ok = (name) => { n++; console.log("ok -", name); };

/* ---------- side list ---------- */
assert.deepEqual(v.siIds(), ["today", "ideas", "changes", "controls", "health"]);
assert.equal(new Set(v.siIds()).size, v.siIds().length);
ok("side list: five sections in plan order, each exactly once (Changes and Controls added in round 543)");

/* ---------- chain steps ---------- */
for (const s of ["done", "waiting", "skipped", "failed", "not_due", "unknown"]) { assert.ok(v.stepLabel(s) && v.stepMark(s), s); }
assert.equal(v.stepTone("done"), "good"); assert.equal(v.stepTone("failed"), "bad"); assert.equal(v.stepTone("waiting"), "warn"); assert.equal(v.stepTone("unknown"), "warn"); assert.equal(v.stepTone("not_due"), undefined);
assert.equal(v.stepLabel("made_up"), "UNKNOWN"); assert.equal(v.stepMark("made_up"), "?");
const marks = ["done", "waiting", "skipped", "failed", "not_due", "unknown"].map(v.stepMark); assert.equal(new Set(marks).size, 6, "every state has its own mark (not colour only)");
assert.equal(v.chainSummary([{ state: "done" }, { state: "done" }, { state: "waiting" }, { state: "not_due" }]), "2 done · 1 waiting · 1 not due");
assert.equal(v.chainSummary([]), "");
ok("chain steps: own label and mark per state, unknown stays unknown, summary counts");

/* ---------- banner, next run, timetable ---------- */
assert.equal(v.bannerLabel({ state: "dry_run" }), "DRY-RUN"); assert.equal(v.bannerLabel({ state: "live" }), "LIVE"); assert.equal(v.bannerLabel({ state: "off" }), "OFF");
assert.equal(v.bannerTone({ state: "dry_run" }), "warn"); assert.equal(v.bannerTone({ state: "off" }), undefined);
assert.equal(v.nextRunText({ state: "scheduled", at_ist: "2026-11-16 16:10", reason: "" }), "Next automatic run: 2026-11-16 16:10 IST");
assert.match(v.nextRunText({ state: "due_now", at_ist: "x", reason: "" }), /within a minute/);
assert.match(v.nextRunText({ state: "off", at_ist: null, reason: "the agent is off" }), /agent is off/);
assert.equal(v.nextRunText(null), "Next automatic run: unknown");
assert.equal(v.daysText(["Mon", "Tue", "Wed", "Thu", "Fri"]), "Mon-Fri"); assert.equal(v.daysText(["Sat"]), "Sat"); assert.equal(v.daysText(["Mon", "Wed", "Fri"]), "Mon, Wed, Fri"); assert.equal(v.daysText([]), "no days");
assert.equal(v.slotLine({ id: "a", pack: "daily_close", time: "16:05", days: ["Mon", "Tue", "Wed", "Thu", "Fri"], enabled: true, today: "waiting", next_ist: null }), "daily_close · 16:05 IST · Mon-Fri");
assert.equal(v.slotTodayTone("not_fired"), "bad"); assert.match(v.slotTodayText("not_fired"), /did NOT fire/);
ok("banner, next run and timetable text");

/* ---------- why no ideas ---------- */
const bd = { allowed: 4, pinned: 2, seen: 2, no_evidence: 1, hold: 1, would_change: 0, unreachable: 0, unreadable: 0, asked: true, ideas: 0, dropped: 0, summary: "4 switches the agent may name: 2 pinned" };
assert.equal(v.whyLine(bd), bd.summary); assert.match(v.whyLine(null), /before round 541/); assert.match(v.whyLine({ ...bd, summary: "" }), /No summary/);
const chips = v.breakdownChips(bd).map((c) => c.label);
assert.deepEqual(chips, ["4 switches it may name", "2 pinned (hidden from it)", "1 no test result", "1 on hold"]);
assert.equal(v.breakdownChips(null).length, 0);
assert.equal(v.breakdownChips({ ...bd, pinned: 4 })[1].tone, "warn", "everything pinned is flagged");
assert.ok(v.breakdownChips({ ...bd, unreachable: 2 }).some((c) => c.tone === "bad" && /unreachable/.test(c.label)));
ok("why-no-ideas: sentence as saved, counts in reading order, old files say so");

/* ---------- ideas ---------- */
assert.equal(v.supportText("rule_holds"), "its rule says: no change (unproven)"); assert.equal(v.supportText("rule_agrees"), "its rule agrees"); assert.equal(v.supportText(undefined), ""); assert.equal(v.supportTone("rule_holds"), "warn");
assert.equal(v.outcomeText("pending"), "waiting for its test"); assert.equal(v.outcomeText(undefined), "waiting for its test"); assert.equal(v.outcomeText("contradicted"), "contradicted");
assert.equal(v.outcomeTone("survived"), "good"); assert.equal(v.outcomeTone("contradicted"), "bad"); assert.equal(v.outcomeTone("not_confirmed"), "warn"); assert.equal(v.outcomeTone("pending"), undefined);
assert.equal(v.directionText("up"), "▲ UP"); assert.equal(v.directionText("down"), "▼ DOWN");
const days = [
  { date: "2026-11-15", mode: "report", queued_n: 0, dropped: [], ideas: [{ switch: "A", direction: "down", outcome: "pending", support: "rule_holds" }, { switch: "B", direction: "down", outcome: "survived" }] },
  { date: "2026-11-14", mode: "propose", queued_n: 2, dropped: [{ switch: "C", reason: "x" }], ideas: [{ switch: "A", direction: "up", outcome: "contradicted" }] },
  { date: "2026-11-13", unreadable: true },
];
assert.equal(v.dayLine(days[0]), "2026-11-15 · report · 2 ideas · 0 dropped · 0 queued");
assert.equal(v.dayLine(days[1]), "2026-11-14 · propose · 1 idea · 1 dropped · 2 queued");
assert.equal(v.dayLine(days[2]), "2026-11-13 · file could not be read");
const t = v.historyTotals(days); assert.equal(t.ideas, 3); assert.deepEqual(t.by, { pending: 1, survived: 1, contradicted: 1 });
assert.equal(Object.values(t.by).reduce((a, b) => a + b, 0), t.ideas, "the outcome counts add up to the number of ideas");
assert.equal(v.totalsLine(t), "3 ideas in the saved days: 1 waiting for its test · 1 survived · 1 contradicted");
assert.equal(v.totalsLine({ ideas: 0, by: {} }), "No ideas in the saved days yet.");
ok("ideas: support, outcome and day lines; totals add up");

/* ---------- health ---------- */
const base = () => ({
  now_ist: "2026-11-16 18:00", weekday: "Mon",
  agent: { mode: "report", mode_effective: "report", stopped: null, run_after_ist: "16:10", next_run: { state: "scheduled", at_ist: "2026-11-17 16:10", reason: "" }, last: null, last_auto_date: null, run_now_allowed: true, market_open: false,
    keys: { gemini: true, groq: true }, usage_today: { date: "2026-11-16", gemini: 0, groq: 0, runs: 0, used: 0, cap: 40, left: 40, gemini_blocked: false }, scoreboard: {}, settings: {} },
  schedule: { enabled: true, slots: [] }, autocalib: {}, chain: [], recent_runs: [],
});
assert.equal(v.agentMissedToday(base()), true, "weekday, 1 h 50 min after 16:10, no run today");
assert.equal(v.agentMissedToday({ ...base(), now_ist: "2026-11-16 17:00" }), false, "inside the 60 minute grace");
assert.equal(v.agentMissedToday({ ...base(), now_ist: "2026-11-16 17:10" }), true, "exactly 60 minutes after");
assert.equal(v.agentMissedToday({ ...base(), weekday: "Sun" }), false); assert.equal(v.agentMissedToday({ ...base(), weekday: "Sat" }), false);
const ran = base(); ran.agent.last_auto_date = "2026-11-16"; assert.equal(v.agentMissedToday(ran), false);
const manual = base(); manual.agent.last = { date: "2026-11-16" }; assert.equal(v.agentMissedToday(manual), false, "a manual run today counts as ran");
const off = base(); off.agent.mode_effective = "off"; assert.equal(v.agentMissedToday(off), false);
assert.equal(v.agentMissedToday({ ...base(), now_ist: "junk" }), false, "junk time is never a warning");
assert.deepEqual(v.healthWarnings({ ...base(), now_ist: "2026-11-16 16:30" }), []);
const bad = base(); bad.agent.keys = { gemini: false, groq: false }; bad.agent.usage_today.left = 0; bad.agent.stopped = { date: "2026-11-10" }; bad.schedule = { enabled: false, slots: [{ id: "x", pack: "daily_close", time: "16:05", today: "not_fired" }] }; bad.chain = [{ id: "agent", title: "AI agent", state: "failed", detail: "no LLM answered" }];
const w = v.healthWarnings(bad).join("\n");
for (const piece of ["switched itself off on 2026-11-10", "No LLM key", "call cap is used up", "timetable is switched off", "daily_close slot (16:05 IST) did not fire", "AI agent: no LLM answered"]) assert.ok(w.includes(piece), piece);
ok("health: missed run needs a weekday and 60 minutes; every warning has plain words");

/* ---------- the second click ---------- */
assert.deepEqual(v.nextArm(null, "propose"), { armed: "propose", fire: false });
assert.deepEqual(v.nextArm("propose", "propose"), { armed: null, fire: true });
assert.deepEqual(v.nextArm("propose", "report"), { armed: "report", fire: false }, "a click on another action re-arms, it never fires");
ok("two-click rule: first click arms, the same second click fires, another click re-arms");

/* ---------- settings lines ---------- */
const sv = { values: { mode: "report", run_after: "16:10", max_ideas: 5, max_queue: 3 }, env_defaults: {}, saved: { mode: "report" }, source: { mode: "saved", run_after: "env", max_ideas: "env", max_queue: "env" }, bounds: { modes: ["off", "report", "propose"], run_after: ["15:45", "23:00"], max_ideas: [1, 10], max_queue: [0, 6] }, audit: [] };
assert.deepEqual(v.settingLines(sv), ["Mode: report (saved on this page)", "Run after: 16:10 IST (from .env)", "Max ideas a night: 5 (from .env)", "Max queued tests a night: 3 (from .env)"]);
assert.equal(v.auditText({ at: 1, at_ist: "2026-11-15 12:01:00", by: "admin", reset: false, changes: { mode: { from: "report", to: "propose" } } }), "2026-11-15 12:01:00 by admin: mode report -> propose");
assert.equal(v.auditText({ at: 1, at_ist: "T", by: "admin", reset: true, changes: {} }), "T by admin: reset to .env");
ok("settings in force and audit lines read as plain text");

/* ---------- the view module is pure ---------- */
const src = rd("./selfImproveView.ts");
assert.ok(!/^import (?!type )/m.test(src) && !/fetch\(/.test(src), "only erased type imports, no network");
ok("selfImproveView.ts is pure");

/* ---------- navigation: the 15th tab ---------- */
assert.ok(nav.TRADING_TABS.includes("selfimprove") && nav.sectionOf("selfimprove").id === "trading");
assert.equal(nav.itemOf("selfimprove").label, "Self Improve System"); assert.equal(nav.itemOf("selfimprove").group, "Trading");
assert.equal(nav.isNavTab("selfimprove"), true); assert.equal(nav.isNavTab("nope"), false); assert.equal(nav.isNavTab(undefined), false); assert.equal(nav.isNavTab({ x: 1 }), false);
assert.equal(nav.activeSlot("selfimprove", null), "trading");
const tc = rd("./components/app/TabContent.tsx");
assert.ok(tc.includes('<AdminGate title="Self Improve System"') && tc.includes("<SelfImproveTab />") && /const SelfImproveTab = lazy\(\(\) => import\("\.\.\/SelfImproveTab"\)\)/.test(tc));
const app = rd("./App.tsx");
assert.ok(/stockky:goto-tab/.test(app) && /isNavTab\(id\)/.test(app) && app.includes('id: "selfimprove"'), "the shell opens the tab from the event and the command palette");
ok("navigation: Trading section, AdminGate, lazy chunk, goto event ignores a bad id");

/* ---------- the tab and its panels ---------- */
const tab = rd("./components/SelfImproveTab.tsx");
assert.ok(/runnerApi\.agentLoop\(\)/.test(tab) && /runnerApi\.agentHistory\(30\)/.test(tab) && /Promise\.allSettled/.test(tab), "two independent reads, one failing does not blank the other");
assert.ok(/data-testid="self-improve"/.test(tab));
const today = rd("./components/selfImprove/TodayPanel.tsx");
assert.ok(/nextArm\(armed, id\)/.test(today) && /click\("propose", .*, true\)/.test(today) && /click\("report", .*, false\)/.test(today), "propose needs the second click, report does not");
assert.ok(/runnerApi\.autoRun\(true\)/.test(today) && !/runnerApi\.autoRun\(\)/.test(today) && !/autoRun\(false\)/.test(today), "the only auto-calibration call is the dry-run preview");
assert.ok(/setTimeout\(\(\) => setArmed\(null\)/.test(today), "an armed click disarms itself");
const api = rd("./calibrationApi.ts");
assert.match(api, /agentLoop: \(\) => R<LoopView>\("\/agent\/loop"\)/); assert.match(api, /agentHistory: .*"\/agent\/history\?limit=\$\{limit\}"|agentHistory: .*`\/agent\/history\?limit=\$\{limit\}`/); assert.match(api, /agentSettings: \(\) => R<AgentSettingsView>\("\/agent\/settings"\)/);
for (const f of ["SelfImproveTab.tsx", "selfImprove/TodayPanel.tsx", "selfImprove/IdeasPanel.tsx", "selfImprove/HealthPanel.tsx", "selfImprove/SiNav.tsx"]) {
  const s = rd(`./components/${f}`);
  assert.ok(!/api[_-]?key|GEMINI_API_KEY|gsk_|AIza/i.test(s.replace(/\(GEMINI_API_KEY or GROQ_API_KEY\)/g, "")), `${f}: no key is shown`);
  assert.ok(!/\/control\/|setSwitch|switchApi|PUT.*\/autocalib\/settings|autoSettings\(/.test(s), `${f}: sets no trading switch and changes no auto-calibration setting`);
}
ok("the tab reads loop and history, propose needs a second click, the only auto-calibration call is the preview, no key, no switch");

/* ---------- the old card is a link ---------- */
const card = rd("./components/calibration/AgentCard.tsx");
assert.ok(/stockky:goto-tab/.test(card) && /tab: "selfimprove"/.test(card) && /Open Self Improve System/.test(card));
ok("the Calibration page's AI agent card is a short link to the new tab");

console.log(`selfImprove.test.mjs: ${n} ok`);
