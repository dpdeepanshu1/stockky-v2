// round 543 (E568, S3): guards for the Controls and Changes sections of the Self Improve System tab
// (run: node --experimental-strip-types src/selfImproveControls.test.mjs). Pure helpers are run; the rest are static checks on the source.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const c = await import("./selfImproveControls.ts");
const v = await import("./selfImproveView.ts");
let n = 0; const ok = (name) => { n++; console.log("ok -", name); };

const values = { mode: "report", run_after: "16:10", max_ideas: 5, max_queue: 3 };
const bounds = { modes: ["off", "report", "propose"], run_after: ["15:45", "23:00"], max_ideas: [1, 10], max_queue: [0, 6] };

/* ---------- form ---------- */
const d0 = c.draftFrom(values);
assert.deepEqual(d0, { mode: "report", run_after: "16:10", max_ideas: "5", max_queue: "3" });
assert.deepEqual(c.validateDraft(d0, bounds), {});
assert.ok(c.validateDraft({ ...d0, mode: "apply" }, bounds).mode, "an unknown mode (the agent has no apply mode) is refused");
for (const t of ["", "25:00", "16:60", "1610", "16:1", "ab:cd"]) assert.ok(c.validateDraft({ ...d0, run_after: t }, bounds).run_after, `run_after ${JSON.stringify(t)}`);
assert.ok(c.validateDraft({ ...d0, run_after: "15:44" }, bounds).run_after, "before 15:45 IST is refused (market hours)");
assert.ok(c.validateDraft({ ...d0, run_after: "23:01" }, bounds).run_after);
assert.deepEqual(c.validateDraft({ ...d0, run_after: "15:45" }, bounds), {}); assert.deepEqual(c.validateDraft({ ...d0, run_after: "23:00" }, bounds), {}); assert.deepEqual(c.validateDraft({ ...d0, run_after: "9:00" }, { ...bounds, run_after: ["0:00", "23:59"] }), {});
for (const t of ["", "0", "11", "-1", "2.5", "abc", "1e1"]) assert.ok(c.validateDraft({ ...d0, max_ideas: t }, bounds).max_ideas, `max_ideas ${JSON.stringify(t)}`);
assert.deepEqual(c.validateDraft({ ...d0, max_ideas: "10" }, bounds), {}); assert.deepEqual(c.validateDraft({ ...d0, max_queue: "0" }, bounds), {}); assert.ok(c.validateDraft({ ...d0, max_queue: "7" }, bounds).max_queue);
ok("form: every field checked against the runner's bounds, edges inclusive, junk refused");

assert.deepEqual(c.draftBody(values, d0, bounds), { body: {}, changed: [] }, "nothing changed -> empty body");
const r1 = c.draftBody(values, { ...d0, max_ideas: "3", run_after: "17:30" }, bounds);
assert.deepEqual(r1.body, { run_after: "17:30", max_ideas: 3 }); assert.deepEqual(r1.changed, ["run_after", "max_ideas"]);
assert.equal(typeof r1.body.max_ideas, "number", "numbers are sent as numbers");
assert.deepEqual(c.draftBody(values, { ...d0, max_ideas: "99" }, bounds), { body: {}, changed: [] }, "an invalid draft sends nothing");
assert.deepEqual(c.draftBody(values, { ...d0, run_after: " 16:10 " }, bounds).body, {}, "spaces around the same time are no change");
assert.equal(c.draftChangeText(values, r1.body), "run after 16:10 → 17:30 · max ideas 5 → 3");
ok("form body: only the fields that changed, numbers as numbers, an invalid form sends nothing");

/* ---------- second-click rule for the agent ---------- */
assert.equal(c.agentNeedsTwoClicks(values, { mode: "propose" }), true);
assert.equal(c.agentNeedsTwoClicks({ ...values, mode: "propose" }, { mode: "propose" }), false, "already propose: no widening");
assert.equal(c.agentNeedsTwoClicks(values, { mode: "off" }), false); assert.equal(c.agentNeedsTwoClicks({ ...values, mode: "propose" }, { mode: "report" }), false, "propose -> report narrows: one click");
assert.equal(c.agentNeedsTwoClicks(values, { max_ideas: 3 }), false);
const env = (mode) => ({ values, env_defaults: { mode } });
assert.equal(c.resetNeedsTwoClicks(env("propose")), true, "reset to an .env that says propose widens");
assert.equal(c.resetNeedsTwoClicks(env("report")), false); assert.equal(c.resetNeedsTwoClicks({ values: { ...values, mode: "propose" }, env_defaults: { mode: "propose" } }), false);
ok("agent: only the move into propose (or a reset into an .env propose) needs the second click");

/* ---------- auto-calibration clicks ---------- */
const S = (enabled, dry_run) => ({ enabled, dry_run });
assert.equal(c.acNeedsTwoClicks({ kind: "dry_run", to: false }, S(true, true)), true, "dry-run OFF: two clicks");
assert.equal(c.acNeedsTwoClicks({ kind: "dry_run", to: true }, S(true, false)), false, "dry-run ON: one click");
assert.equal(c.acNeedsTwoClicks({ kind: "pin", key: "K", to: false }, S(true, true)), true, "unpin: two clicks");
assert.equal(c.acNeedsTwoClicks({ kind: "pin", key: "K", to: true }, S(true, true)), false, "pin: one click");
assert.equal(c.acNeedsTwoClicks({ kind: "enabled", to: false }, S(true, false)), false, "switch OFF: one click");
assert.equal(c.acNeedsTwoClicks({ kind: "enabled", to: true }, S(false, true)), false, "switch ON while dry-run is on: it only reports");
assert.equal(c.acNeedsTwoClicks({ kind: "enabled", to: true }, S(false, false)), true, "switch ON while dry-run is off: it would start applying");
ok("auto-calibration: widening moves need two clicks, narrowing moves one");

assert.deepEqual(c.acBody({ kind: "enabled", to: false }, []), { enabled: false });
assert.deepEqual(c.acBody({ kind: "dry_run", to: true }, []), { dry_run: true });
assert.deepEqual(c.acBody({ kind: "pin", key: "B", to: true }, ["C", "A"]), { pinned: ["A", "B", "C"] }, "pin adds one switch to the whole list, sorted");
assert.deepEqual(c.acBody({ kind: "pin", key: "A", to: false }, ["C", "A", "A"]), { pinned: ["C"] }, "unpin removes only that switch");
assert.deepEqual(c.acBody({ kind: "pin", key: "A", to: true }, ["A"]), { pinned: ["A"] }, "pinning twice does not duplicate");
const ids = [{ kind: "enabled", to: true }, { kind: "enabled", to: false }, { kind: "dry_run", to: true }, { kind: "dry_run", to: false }, { kind: "pin", key: "K", to: true }, { kind: "pin", key: "K", to: false }, { kind: "pin", key: "J", to: false }].map(c.acArmId);
assert.equal(new Set(ids).size, ids.length, "every action has its own arm id, so a click on another action re-arms");
assert.equal(v.nextArm(c.acArmId({ kind: "pin", key: "K", to: false }), c.acArmId({ kind: "pin", key: "J", to: false })).fire, false);
assert.match(c.acConfirmText({ kind: "dry_run", to: false }), /changes real switches/); assert.match(c.acConfirmText({ kind: "pin", key: "KRONOS_GATE_MODE", to: false }), /KRONOS_GATE_MODE/);
assert.equal(c.acButtonText({ kind: "dry_run", to: false }, true), "Click again: turn dry-run OFF"); assert.equal(c.acButtonText({ kind: "pin", key: "K", to: false }, false), "Unpin"); assert.equal(c.acButtonText({ kind: "pin", key: "K", to: true }, false), "Pin");
ok("auto-calibration bodies: whole sorted pinned list, no duplicates, unique arm ids, plain confirm text");

/* ---------- pinned list ---------- */
const rules = [
  { key: "STRUCTURE_MIN_RR", title: "min RR", service: "real-trade", tests: ["calibration_pack"] },
  { key: "KRONOS_GATE_MODE", title: "kronos", service: "real-trade", tests: ["kronos_backtest"] },
  { key: "SETUP_S2_ENABLED", title: "setup 2", service: "real-trade", tests: ["x"] },
  { key: "OVERNIGHT_HOLD_ENABLED", title: "hold", service: "real-trade", tests: ["hold_bucket"] },
];
const rows = c.pinRows(rules, ["KRONOS_GATE_MODE", "STRUCTURE_MIN_RR", "GONE"]);
assert.deepEqual(rows.map((r) => r.key), ["OVERNIGHT_HOLD_ENABLED", "KRONOS_GATE_MODE", "STRUCTURE_MIN_RR", "SETUP_S2_ENABLED"], "group A, B, C, other");
assert.deepEqual(rows.map((r) => r.pinned), [false, true, true, false]);
assert.equal(c.pinGroup("SETUP_S2_ENABLED"), "other"); assert.equal(c.pinGroup("OVERNIGHT_HOLD_ENABLED"), "A"); assert.equal(c.pinGroup("PS_STRUCTURE_GATE_MODE"), "B");
assert.equal(c.pinSummary(rows), "2 pinned (never changed automatically) · 2 unpinned (auto-calibration may change them; the agent can name them)", "a pinned name that has no rule any more is not counted");
assert.equal(c.pinRows([], ["X"]).length, 0);
assert.deepEqual(c.protectedRows({ B: "b why", A: "a why" }), [{ key: "A", why: "a why" }, { key: "B", why: "b why" }]); assert.deepEqual(c.protectedRows(undefined), []);
ok("pinned list: group order A/B/C/other, counts only switches that have a rule, protected list sorted");

/* ---------- changes ---------- */
assert.equal(c.changeWord("LOOSENS"), "LOOSENED"); assert.equal(c.changeWord("TIGHTENS"), "TIGHTENED"); assert.equal(c.changeWord("NEUTRAL"), "CHANGED"); assert.equal(c.changeWord(undefined), "CHANGED");
assert.equal(c.changeTone("LOOSENS"), "warn"); assert.equal(c.changeTone("TIGHTENS"), undefined);
assert.equal(c.changeHeadline({ at: 1763200200, title: "min RR", key: "STRUCTURE_MIN_RR", from: "1.5", to: "1.8" }), "2025-11-15 15:20 IST · min RR: 1.5 → 1.8");
assert.equal(c.changeHeadline({ at: 1763200200, title: "", key: "K", from: null, to: 3 }), "2025-11-15 15:20 IST · K: - → 3");
assert.equal(c.changeApplied({ applied_live: true, restart_required: true }), "applied live"); assert.match(c.changeApplied({ applied_live: false, restart_required: true }), /needs a service restart/); assert.equal(c.changeApplied({ applied_live: false, restart_required: false }), "saved, not live yet");
assert.deepEqual(c.pendingRows({ B: { to: "off", votes: ["r1"] }, A: { to: "on", votes: [] } }, [{ key: "A", title: "Alpha" }]), [{ key: "A", title: "Alpha", to: "on", votes: 0 }, { key: "B", title: "B", to: "off", votes: 1 }]);
assert.deepEqual(c.pendingRows(null, []), []); assert.equal(c.pendingRows({ X: { to: "1", votes: "junk" } }, [])[0].votes, 0);
assert.equal(c.changesSummary([]), "No switch was changed by auto-calibration yet.");
assert.equal(c.changesSummary([{ change: "TIGHTENS" }, { change: "LOOSENS" }, { change: "TIGHTENS" }, { change: "NEUTRAL" }]), "4 changes in the history: 2 tightened · 1 loosened · 1 other");
assert.equal(c.changesSummary([{ change: "TIGHTENS" }]), "1 change in the history: 1 tightened · 0 loosened · 0 other");
assert.deepEqual(c.acAuditLines(undefined), []); assert.deepEqual(c.acAuditLines("junk"), []);
ok("changes: words, applied-live honesty, waiting votes, summary counts add up, old runner without audit is fine");

/* ---------- static checks ---------- */
const ctl = rd("./components/selfImprove/ControlsPanel.tsx");
const chg = rd("./components/selfImprove/ChangesPanel.tsx");
const arm = rd("./components/selfImprove/useArm.ts");
const tab = rd("./components/SelfImproveTab.tsx");
assert.ok(/runnerApi\.autoStatus\(false\)/.test(tab) && /Promise\.allSettled/.test(tab) && /section === "controls"/.test(tab) && /section === "changes"/.test(tab));
assert.ok(/runnerApi\.agentSetSettings\(/.test(ctl) && /runnerApi\.autoSettings\(acBody\(a, acs\.pinned, acNeedsTwoClicks\(a, acs\)\)\)/.test(ctl), "the two writes of the Controls panel");
assert.ok(/acNeedsTwoClicks\(a, acs\)/.test(ctl) && /agentNeedsTwoClicks\(st\.values, body\)/.test(ctl) && /resetNeedsTwoClicks\(st\)/.test(ctl), "every write goes through the second-click rule");
assert.ok(/confirm: true/.test(ctl) && /needs2 \? \{ confirm: true \}/.test(ctl), "propose is sent with the confirm the runner demands, only after the second click");
assert.ok(/setTimeout\(\(\) => setArmed\(null\), ARM_MS\)/.test(arm) && /ARM_MS = 8000/.test(arm) && /nextArm\(armed, id\)/.test(arm), "an armed click disarms itself after 8 seconds");
for (const [name, s] of [["ControlsPanel", ctl], ["ChangesPanel", chg], ["useArm", arm]]) {
  assert.ok(!/\/control\/|setSwitch|switchApi|autoRun\(|agentRun\(/.test(s), `${name}: sets no trading switch, applies nothing on the spot, runs no agent`);
  assert.ok(!/api[_-]?key|GEMINI_API_KEY|gsk_|AIza/i.test(s), `${name}: no key is shown`);
}
assert.ok(!/autoSettings\(|agentSetSettings\(/.test(chg), "Changes is read only");
assert.ok(!/localStorage|sessionStorage/.test(ctl + chg + arm), "no browser storage");
const api = rd("./calibrationApi.ts");
assert.match(api, /agentSetSettings: \(body: \{[^}]*\}\) => R<AgentSettingsView>\("\/agent\/settings", \{ method: "POST"/);
assert.match(api, /autoStatus: \(coverage = true\) => R<AutoStatus>\(coverage \? "\/autocalib\/status" : "\/autocalib\/status\?coverage=false"\)/);
assert.ok(rd("./components/calibration/AutoCalibPanel.tsx").includes("runnerApi.autoStatus()") , "the old panel still asks for the coverage too");
const old = rd("./components/calibration/AutoCalibPanel.tsx");
assert.ok(/const two = acNeedsTwoClicks\(a, s\);/.test(old) && /two && !window\.confirm\(/.test(old) && /acBody\(a, s\.pinned, two\)/.test(old) && /change\(\{ kind: "dry_run", to: !s\.dry_run \}\)/.test(old) && /change\(\{ kind: "pin", key, to: !pinned \}\)/.test(old), "the old Calibration page asks before turning dry-run off or unpinning");
assert.ok(!/autoSettings\(\{ (dry_run|enabled)/.test(old) && !/autoSettings\(\{ pinned/.test(old), "no one-click write is left on the old page");
const src = rd("./selfImproveControls.ts");
assert.ok(!/^import (?!type )/m.test(src) && !/fetch\(/.test(src), "selfImproveControls.ts: only erased type imports, no network");
ok("Controls and Changes: two writes behind the second-click rule, Changes read only, nothing applied on the spot, no key, no storage");

console.log(`selfImproveControls.test.mjs: ${n} ok`);

// round 547: the body carries confirm:true only when the caller says so (the second click), and never changes the rest of the body
assert.deepEqual(c.acBody({ kind: "dry_run", to: false }, []), { dry_run: false }, "no confirm by default");
assert.deepEqual(c.acBody({ kind: "dry_run", to: false }, [], true), { dry_run: false, confirm: true }, "dry-run OFF with the second click");
assert.deepEqual(c.acBody({ kind: "pin", key: "B", to: false }, ["A", "B"], true), { pinned: ["A"], confirm: true }, "unpin with the second click");
assert.deepEqual(c.acBody({ kind: "pin", key: "B", to: true }, ["A"]), { pinned: ["A", "B"] }, "pin: one click, no confirm");
