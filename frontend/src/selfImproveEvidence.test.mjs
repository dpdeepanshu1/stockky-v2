// round 556 (E581): guards for Self Improve System > Evidence (run: node --experimental-strip-types src/selfImproveEvidence.test.mjs).
// Pure helpers are run; the rest are static checks on the source: read only, optional read, no rupee field, no switch is set, wired into npm test.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const e = await import("./selfImproveEvidence.ts");
const v = await import("./selfImproveView.ts");
let n = 0; const ok = (name) => { n++; console.log("ok -", name); };

/* ---------- verdict and lifecycle words and tones ---------- */
assert.equal(e.verdictTone("POSITIVE"), "good"); assert.equal(e.verdictTone("NEGATIVE"), "bad"); assert.equal(e.verdictTone("NOT_PROVEN"), "warn");
assert.equal(e.verdictTone("NOT_ENOUGH_DATA"), undefined, "no information is not a warning"); assert.equal(e.verdictTone("made_up"), undefined); assert.equal(e.verdictTone(null), undefined);
assert.equal(e.verdictLabel("NOT_ENOUGH_DATA"), "NOT ENOUGH DATA"); assert.equal(e.verdictLabel("made_up"), "UNKNOWN"); assert.equal(e.verdictLabel(undefined), "UNKNOWN");
for (const x of ["POSITIVE", "NEGATIVE", "NOT_PROVEN", "NOT_ENOUGH_DATA", "zzz"]) assert.ok(e.verdictMeaning(x).length > 10, x);
assert.match(e.verdictMeaning("POSITIVE"), /not a reason to raise size/); assert.match(e.verdictMeaning("NEGATIVE"), /You decide/); assert.match(e.verdictMeaning("NOT_ENOUGH_DATA"), /no information/);
assert.equal(e.lifecycleTone("proven"), "good"); assert.equal(e.lifecycleTone("demoted"), "bad"); assert.equal(e.lifecycleTone("watch"), "warn"); assert.equal(e.lifecycleTone("testing"), undefined); assert.equal(e.lifecycleTone("retired"), undefined);
assert.equal(e.driftTone("DECAY"), "bad"); assert.equal(e.driftTone("WARNING"), "warn"); assert.equal(e.driftTone("IMPROVING"), "good"); assert.equal(e.driftTone("STABLE"), undefined); assert.equal(e.driftTone(null), undefined);
assert.equal(e.driftLabel(null), "no drift reading"); assert.equal(e.driftLabel("NOT_ENOUGH_DATA"), "NOT ENOUGH DATA"); assert.equal(e.driftLabel("alien"), "UNKNOWN");
ok("verdict, lifecycle and drift: own word and tone each, no information stays neutral, unknown stays unknown");

/* ---------- numbers ---------- */
assert.equal(e.pct(0.21), "0.21%"); assert.equal(e.pct(-0.05), "-0.05%"); assert.equal(e.pct(-0.4), "-0.4%"); assert.equal(e.pct(18.24, 1), "18.2%"); assert.equal(e.pct(0), "0%");
assert.equal(e.pct(null), "-"); assert.equal(e.pct(undefined), "-"); assert.equal(e.pct(NaN), "-"); assert.equal(e.pct(Infinity), "-");
assert.equal(e.probText(0.62), "62 %"); assert.equal(e.probText(null), "-"); assert.equal(e.probText(NaN), "-");
ok("numbers: percentages trimmed, missing / NaN / Infinity show a dash");

/* ---------- missing, stale, unreadable ---------- */
assert.equal(e.partNotice("ok", "book"), ""); assert.equal(e.partNotice("ok", "registry"), "");
assert.match(e.partNotice("none", "book"), /export_records, then evidence_report/); assert.match(e.partNotice("none", "registry"), /strategy_registry job creates it/);
assert.match(e.partNotice("stale", "book"), /14 days/); assert.match(e.partNotice("stale", "registry"), /7 days/);
assert.match(e.partNotice("unreadable", "book"), /could not be read/); assert.match(e.partNotice("unreadable", "registry"), /could not be read/);
assert.match(e.partNotice(undefined, "book"), /No evidence report/);
ok("missing, stale and unreadable: each has its own plain notice");

/* ---------- registry counts and attention rows ---------- */
assert.deepEqual(e.registryChips({ testing: 1, proven: 2, watch: 0, demoted: 1, retired: 0 }), [{ label: "1 testing", tone: undefined }, { label: "2 proven", tone: "good" }, { label: "1 demoted", tone: "bad" }]);
assert.deepEqual(e.registryChips({}), []); assert.deepEqual(e.registryChips(undefined), []);
assert.equal(e.registryTotal({ testing: 1, proven: 2 }), 3); assert.equal(e.registryTotal(undefined), 0);
const row = { strategy: "volume_shock | WATCH", status: "demoted", drift: "DECAY", verdict: "NEGATIVE", n: 61 };
assert.equal(e.attentionText(row), "demoted · drift DECAY · last verdict NEGATIVE · 61 closed trades");
assert.equal(e.attentionText({ strategy: "a | b", status: "watch", drift: null, verdict: null, n: 1 }), "watch · no drift reading · no verdict yet · 1 closed trade");
assert.equal(e.attentionTone(row), "bad"); assert.equal(e.attentionTone({ ...row, status: "proven", drift: "WARNING" }), "warn"); assert.equal(e.attentionTone({ ...row, status: "watch", drift: "DECAY" }), "bad"); assert.equal(e.attentionTone({ ...row, status: "testing", drift: null }), undefined);
const wk = { group: "tier_name", key: "tier_c", n: 33, mean_net_ret_pct: -0.4, lower90_pct: -0.9, verdict: "NOT_PROVEN" };
assert.equal(e.weakText(wk), "tier_name: tier_c · 33 trades · mean -0.4% · lower bound -0.9% · NOT PROVEN");
assert.equal(e.weakText({ ...wk, n: 1, mean_net_ret_pct: null }), "tier_name: tier_c · 1 trade · mean - · lower bound -0.9% · NOT PROVEN");
assert.equal(e.verdictCountsText({ NOT_PROVEN: 5, NOT_ENOUGH_DATA: 7 }), "5 not proven · 7 not enough data"); assert.equal(e.verdictCountsText({}), ""); assert.equal(e.verdictCountsText(undefined), "");
ok("registry chips, attention rows, weak groups and verdict counts read plainly");

/* ---------- the Needs-a-person line ---------- */
const view = (verdict, attention, bs = "ok", rs = "ok") => ({ book_evidence: { status: bs, verdict }, strategy_status: { status: rs, attention } });
assert.equal(e.evidenceWarning(null), ""); assert.equal(e.evidenceWarning(undefined), "");
assert.equal(e.evidenceWarning(view("NOT_PROVEN", [])), "", "a not-proven book is no warning");
assert.equal(e.evidenceWarning(view("POSITIVE", [{ ...row, status: "watch", drift: "WARNING" }])), "", "watch / warning alone is shown on Evidence, not as a warning");
assert.match(e.evidenceWarning(view("NEGATIVE", [])), /real trades read NEGATIVE/);
assert.match(e.evidenceWarning(view("NOT_PROVEN", [row])), /1 strategy is demoted or decaying/);
assert.match(e.evidenceWarning(view("NOT_PROVEN", [row, { ...row, status: "proven" }])), /2 strategies are demoted or decaying/);
assert.equal(e.evidenceWarning(view("NEGATIVE", [row], "stale", "stale")), "", "a stale part is never turned into a warning");
assert.match(e.evidenceWarning(view("NEGATIVE", [row])), /changes no switch/);
ok("needs-a-person line: only NEGATIVE or demoted / DECAY, never from a stale part, always says it changes nothing");

/* ---------- source checks ---------- */
const panel = rd("./components/selfImprove/EvidencePanel.tsx"), tab = rd("./components/SelfImproveTab.tsx"), api = rd("./calibrationApi.ts"), health = rd("./components/selfImprove/HealthPanel.tsx"), pkg = rd("../package.json");
assert.ok(/agentEvidence: \(\) => R<EvidenceView>\("\/agent\/evidence"\)/.test(api), "a GET, no method / body");
assert.ok(/runnerApi\.agentEvidence\(\)\.then/.test(tab) && !/\[l, h, ev/.test(tab) && /const bad = \[l, h\]/.test(tab), "the evidence read is optional: a failure never lands in the page error banner");
assert.ok(/section === "evidence" && <EvidencePanel/.test(tab) && !/loop && section === "evidence"/.test(tab), "the Evidence section does not wait for the loop read");
assert.ok(/data-testid="si-evidence"/.test(panel) && /Read only/.test(panel) && /decision/.test(panel), "read-only note and the 'your decision' wording are on the page");
assert.ok(!/runnerApi\.|fetch\(|method:|onClick|<Btn/.test(panel), "the panel has no button, no call and no write");
assert.ok(!/\.net\b|₹|money\(/.test(panel), "no rupee amount is shown");
assert.ok(/evidenceWarning/.test(health), "the Health card shows the evidence warning");
assert.ok(pkg.includes("src/selfImproveEvidence.test.mjs"), "wired into npm test");
ok("source: read-only GET, optional read, no button or write, no rupee field, Health warning, wired into npm test");
console.log(`${n} groups passed`);
