// Run: node --experimental-strip-types agentView.test.mjs  (Node >= 22.6). Round 536: the agent card's pure helpers, plus a source check that the card, API and panel are wired.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { headline, runBlock, keyLine, usageLine, scoreLine, scoreTone, whenIst, runResultLines } from "./agentView.ts";

const usage = { date: "2026-11-16", gemini: 2, groq: 1, runs: 1, used: 3, cap: 40, left: 37, gemini_blocked: false };
const base = { stopped: null, mode: "propose", mode_effective: "propose", market_open: false, keys: { gemini: true, groq: true }, usage_today: usage };
const sb = { ideas: 12, survived: 1, not_confirmed: 3, contradicted: 1, unclear: 2, no_result: 0, tested: 5, pending: 4, survival_pct: 20, dropped: 1, since_resume: false, rule: "r" };
let n = 0; const ok = (m) => { n++; console.log("ok -", m); };

assert.equal(headline(base).kind, "propose"); assert.equal(headline(base).tone, "warn");
assert.equal(headline({ ...base, mode: "report", mode_effective: "report" }).tone, "good");
assert.match(headline({ ...base, stopped: { date: "2026-12-01" } }).text, /switched itself off on 2026-12-01/);
assert.equal(headline({ ...base, mode: "off", mode_effective: "off" }).kind, "off");
ok("headline: report is calm, propose is warn, off and self-off are spelled out");

assert.equal(runBlock(base), "");
assert.match(runBlock({ ...base, stopped: { date: "x" } }), /Resume/);
assert.match(runBlock({ ...base, market_open: true }), /15:45/);
assert.match(runBlock({ ...base, keys: { gemini: false, groq: false } }), /No LLM key/);
assert.match(runBlock({ ...base, usage_today: { ...usage, left: 0 } }), /cap/);
assert.match(runBlock({ ...base, mode_effective: "off" }), /AGENT_MODE/);
ok("run buttons: blocked for self-off, market open, no key, cap used up; otherwise free");

assert.equal(keyLine({ gemini: true, groq: false }), "Gemini key set · Groq key NOT set");
assert.equal(usageLine(usage), "LLM calls today: 3 of 40 (Gemini 2, Groq 1)");
assert.match(usageLine({ ...usage, gemini_blocked: true }), /Groq answers/);
assert.match(scoreLine(sb), /12 ideas · 5 tested · 1 survived \(20%\)/);
assert.match(scoreLine({ ...sb, ideas: 0, since_resume: true }), /since Resume/);
assert.equal(scoreTone(sb), "good"); assert.equal(scoreTone({ ...sb, survival_pct: 4 }), "bad"); assert.equal(scoreTone({ ...sb, survival_pct: null }), undefined);
ok("usage, score and idea lines");

assert.equal(whenIst(1763291400), "2025-11-16 16:40 IST");
assert.equal(whenIst(NaN), "-"); assert.equal(whenIst(undefined), "-");
ok("whenIst formats in IST and survives junk");

assert.deepEqual(runResultLines({ ran: false, reason: "refused: the market is open" }), ["refused: the market is open"]);
const lines = runResultLines({ ran: true, ideas: [{ switch: "A", direction: "down", why: "w" }], queued: [{ test: "t1", switch: "A" }, { test: "t1", switch: "B" }], skipped: [{ test: "t2", reason: "old" }], health: ["model x retired"], stopped: "r" });
assert.deepEqual(lines, ["A down: w", "Queued tests: t1", "Not queued: 1 (old)", "Warning: model x retired", "The agent switched itself off: r"]);
assert.deepEqual(runResultLines({ ran: true }), ["No ideas this time."]);
ok("run result lines: refusal reason, ideas, queued tests and warnings and self-off");

const view = readFileSync(new URL("./agentView.ts", import.meta.url), "utf8");
assert.ok(!/^import /m.test(view) && !/fetch\(/.test(view), "pure: no imports, no requests");
const card = readFileSync(new URL("./components/calibration/AgentCard.tsx", import.meta.url), "utf8");
assert.ok(!/api[_-]?key|GEMINI_API_KEY|gsk_|AIza/i.test(card), "the card never shows a key");
ok("the helpers are pure and the card never shows a key");

const api = readFileSync(new URL("./calibrationApi.ts", import.meta.url), "utf8");
assert.match(api, /agentStatus: \(\) => R<AgentStatus>\("\/agent\/status"\)/);
assert.match(api, /agentRun: .*"\/agent\/run"/); assert.match(api, /agentResume: .*"\/agent\/resume"/);
const panel = readFileSync(new URL("./components/calibration/AutoCalibPanel.tsx", import.meta.url), "utf8");
assert.match(panel, /import AgentCard/); assert.match(panel, /<AgentCard \/>/);
assert.match(card, /runnerApi\.agentRun\(/); assert.match(card, /runnerApi\.agentResume\(/); assert.match(card, /data-testid="agent-card"/);
ok("the API client and the panel mount are wired");

console.log(`agentView.test.mjs: ${n} ok`);
