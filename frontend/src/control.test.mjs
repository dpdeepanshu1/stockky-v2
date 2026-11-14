// E539 (redesign round 6): guards for the F&O option-chain table, the engine status row and the Calibration side list. Run: node --experimental-strip-types src/control.test.mjs
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import * as ch from "./fnoChainView.ts";
import * as en from "./fnoEngineView.ts";
import * as cal from "./calibrationNav.ts";

const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const stripCss = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "");
const code = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
let n = 0; const test = (name, f) => { f(); n++; console.log("ok -", name); };

const side = (oi, ltp = 10) => ({ oi, volume: 1, iv: 12.5, delta: 0.5, ltp });
const strikes = [24800, 24600, 24500, 24400, 24300, 23800, 25000, 25100, 25200, 25300].map((k) => ({ strike: k, ce: side(k === 24500 ? 1000 : 250), pe: side(k === 24400 ? 500 : 100) }));

/* ---------- option chain rows ---------- */
test("atmStrike picks the strike nearest the spot (and null for no strikes)", () => {
  assert.equal(ch.atmStrike(strikes, 24512), 24500);
  assert.equal(ch.atmStrike(strikes, 24580), 24600);
  assert.equal(ch.atmStrike([], 24500), null); assert.equal(ch.atmStrike(null, 24500), null);
});
test("chainRows: sorted by strike, only strikes within the window of the ATM, exactly one ATM row", () => {
  const rows = ch.chainRows(strikes, 24512, "NIFTY");
  assert.deepEqual(rows.map((r) => r.strike), [24300, 24400, 24500, 24600, 24800, 25000, 25100]);   // 600 points either side of 24500, edges included; 23800, 25200 and 25300 are out
  assert.equal(rows.filter((r) => r.atm).length, 1);
  assert.equal(rows.find((r) => r.atm).strike, 24500);
  assert.equal(ch.atmIndex(rows), 2);
  assert.equal(ch.chainWindow("SENSEX"), 2000); assert.equal(ch.chainWindow("NIFTY"), 600);
  assert.equal(ch.chainRows(strikes, 24512, "SENSEX").length, 10);
  assert.deepEqual(ch.chainRows([], 1, "NIFTY"), []);
});
test("in the money: calls below the spot, puts above it, never the ATM row", () => {
  const rows = ch.chainRows(strikes, 24512, "NIFTY");
  const at = (k) => rows.find((r) => r.strike === k);
  assert.ok(at(24400).ceItm && !at(24400).peItm);
  assert.ok(at(24600).peItm && !at(24600).ceItm);
  assert.ok(!at(24500).ceItm && !at(24500).peItm);
});
test("OI bars: CE and PE share one scale (the biggest OI shown is 100), whole percents, 0 for missing or zero", () => {
  const rows = ch.chainRows(strikes, 24512, "NIFTY");
  const atm = rows.find((r) => r.atm);
  assert.equal(atm.ceOiPct, 100);
  assert.equal(atm.peOiPct, 10);
  assert.equal(rows.find((r) => r.strike === 24400).peOiPct, 50);
  const odd = ch.chainRows([{ strike: 100, ce: null, pe: { oi: 0 } }, { strike: 200, ce: { oi: null }, pe: { oi: 40 } }], 100, "NIFTY");
  assert.deepEqual(odd.map((r) => [r.ceOiPct, r.peOiPct]), [[0, 0], [0, 100]]);
});
test("number formats: Indian grouping, 2 decimals, a dash for a missing value", () => {
  assert.equal(ch.chainN0(1234567), "12,34,567"); assert.equal(ch.chainN0(null), "-"); assert.equal(ch.chainN0(undefined), "-");
  assert.equal(ch.chainN2(12.5), "12.50"); assert.equal(ch.chainN2(null), "-"); assert.equal(ch.chainN2(NaN), "-");
});

/* ---------- engine status pills ---------- */
const O = (over = {}) => ({ locks: { engine_enabled: true, mode: "DEMO", real_enabled: false, armed_today: false }, market_open: true, runner: { alive: true, error: null }, ...over });
const byId = (ps, id) => ps.find((p) => p.id === id);
test("engine pills: nothing without an overview; every pill has words", () => {
  assert.deepEqual(en.enginePills(null), []); assert.deepEqual(en.enginePills({}), []);
  const ps = en.enginePills(O(), { problems: [], ok: 8, idle: 0 }, { all_green: true, counts: { green: 9, red: 0, unknown: 0, hard_open: 0 } });
  assert.deepEqual(ps.map((p) => p.id), ["engine", "mode", "armed", "market", "runner", "pipeline", "readiness"]);
  for (const p of ps) assert.ok(p.text.length > 3, p.id);
});
test("engine pills: OFF and stopped are offline, DEMO is neutral info, not armed is grey (closed), paused adds a pill", () => {
  const ps = en.enginePills(O({ locks: { engine_enabled: false, mode: "DEMO", armed_today: false }, market_open: false, runner: { alive: false, error: "boom" }, paused: true }));
  assert.equal(byId(ps, "engine").state, "offline"); assert.equal(byId(ps, "engine").text, "Engine off");
  assert.equal(byId(ps, "mode").text, "Mode: DEMO"); assert.equal(byId(ps, "mode").state, "info");
  assert.equal(byId(ps, "armed").state, "closed"); assert.equal(byId(ps, "armed").text, "Not armed");
  assert.equal(byId(ps, "market").state, "closed");
  assert.equal(byId(ps, "runner").state, "offline"); assert.equal(byId(ps, "runner").title, "boom");
  assert.equal(byId(ps, "paused").state, "paused");
});
test("engine pills: REAL says whether orders are allowed or locked; armed is its own state", () => {
  assert.equal(byId(en.enginePills(O({ locks: { engine_enabled: true, mode: "REAL", real_enabled: true, armed_today: true } })), "mode").text, "Mode: REAL, orders allowed");
  assert.equal(byId(en.enginePills(O({ locks: { engine_enabled: true, mode: "REAL", real_enabled: false, armed_today: true } })), "mode").text, "Mode: REAL, orders locked");
  assert.equal(byId(en.enginePills(O({ locks: { engine_enabled: true, mode: "DEMO", armed_today: true } })), "armed").state, "armed");
});
test("pipeline and readiness pills: problems are offline, idle is grey (never green), unmet readiness is a caution, unknown is not green", () => {
  assert.equal(byId(en.enginePills(O(), { problems: ["a", "b"], ok: 3, idle: 3 }), "pipeline").text, "Pipeline: 2 problems");
  assert.equal(byId(en.enginePills(O(), { problems: ["a"], ok: 3, idle: 3 }), "pipeline").text, "Pipeline: 1 problem");
  assert.equal(byId(en.enginePills(O(), { problems: [], ok: 0, idle: 8 }), "pipeline").state, "closed");
  assert.equal(byId(en.enginePills(O(), { problems: [], ok: 1, idle: 7 }), "pipeline").text, "Pipeline: 1 stage OK");
  const r = (c, all = false) => byId(en.enginePills(O(), null, { all_green: all, counts: c }), "readiness");
  assert.equal(r({ red: 2, unknown: 0, hard_open: 1 }).state, "paused");
  assert.equal(r({ red: 0, unknown: 3, hard_open: 0 }).state, "info");
  assert.equal(r({ red: 0, unknown: 0, hard_open: 0 }, true).state, "live");
  assert.equal(r({ red: 0, unknown: 0, hard_open: 0 }, false), undefined);
  assert.equal(byId(en.enginePills(O(), null, null), "pipeline"), undefined);
});

/* ---------- calibration side list ---------- */
const OLD_SECTIONS = ["switches", "alerts", "auto", "weather", "research", "short", "tape", "setups", "swing", "reconcile", "probes", "trades", "plan", "tests", "runs"];
test("calibration side list: the 15 old sections each appear exactly once, in four groups; the plan's seven names are all there", () => {
  assert.ok(cal.calCovers(cal.CAL_GROUPS, OLD_SECTIONS));
  assert.deepEqual(cal.CAL_GROUPS.map((g) => g.label), ["Control", "Market", "Checks", "Runs"]);
  const labels = cal.CAL_GROUPS.flatMap((g) => g.items.map((i) => i.label));
  for (const want of ["Switches", "Runs & downloads", "Tests & training", "Order-flow tape", "Swing lane", "Short selling", "Market research"]) assert.ok(labels.includes(want), want);
  assert.equal(cal.calGroupOf("tape").id, "market"); assert.equal(cal.calGroupOf("nope"), null);
});
test("calCovers fails when a section is missing or repeated", () => {
  assert.ok(!cal.calCovers([{ id: "a", label: "A", items: [{ id: "switches", label: "S" }] }], ["switches", "alerts"]));
  assert.ok(!cal.calCovers([{ id: "a", label: "A", items: [{ id: "switches", label: "S" }, { id: "switches", label: "S" }] }], ["switches", "alerts"]));
});
test("the Run-all bar shows on exactly the sections it showed on before", () => {
  const hidden = ["runs", "alerts", "weather", "research", "swing", "reconcile", "tape", "setups", "probes"];
  for (const s of OLD_SECTIONS) assert.equal(cal.showsRunAll(s), !hidden.includes(s), s);
});
test("run progress: time used of the limit, clamped to 0..100, null when it cannot be known", () => {
  const t0 = "2026-11-14T10:00:00Z", at = (s) => new Date(t0).getTime() + s * 1000;
  assert.deepEqual(cal.runProgress(t0, 600, at(150)), { pct: 25, usedS: 150 });
  assert.equal(cal.runProgress(t0, 600, at(9999)).pct, 100);
  assert.equal(cal.runProgress(t0, 600, at(-50)).pct, 0);
  assert.equal(cal.runProgress(null, 600), null); assert.equal(cal.runProgress("bad", 600), null); assert.equal(cal.runProgress(t0, 0), null);
});
test("switch row lines: last changed in words, and a one-line description (first sentence, cut at 140)", () => {
  assert.equal(cal.lastChangedText(null), "Last changed: not changed from this page");
  assert.equal(cal.lastChangedText({ by: "admin", at: "2026-11-14T10:32:11+00:00" }), "Last changed: 2026-11-14 10:32 by admin");
  assert.equal(cal.oneLine("Stops new entries after the cut-off. Longer text follows."), "Stops new entries after the cut-off.");
  assert.equal(cal.oneLine(""), ""); assert.equal(cal.oneLine(null), "");
  const long = cal.oneLine("x".repeat(300)); assert.equal(long.length, 140); assert.ok(long.endsWith("…"));
  assert.equal(cal.oneLine("No full stop here"), "No full stop here");
});

/* ---------- source guards ---------- */
test("FnoChain draws a kit DataTable (no hand-made table), keeps the 8 s poll and the same chain call, and nothing places an order", () => {
  const s = code(rd("./components/fno/FnoChain.tsx"));
  assert.ok(/<DataTable/.test(s) && !/<table/.test(s));
  assert.ok(/fnoApi\.chain\(und, expiry\)/.test(s) && /8000/.test(s));
  assert.ok(!/fnoApi\.(?!chain)\w+\(/.test(s), "only the chain call");
  assert.ok(/oc-row-atm/.test(s) && /ATM/.test(s) && /pcrReading/.test(s) && /d\.bias\.reasons/.test(s), "ATM row, PCR reading and the bias reasons are kept");
});
test("DataTable keeps working for older callers: rowClass, cellClass and wrapRef are optional", () => {
  const s = code(rd("./components/kit/DataTable.tsx"));
  assert.ok(/rowClass\?:/.test(s) && /cellClass\?:/.test(s) && /wrapRef\?:/.test(s));
});
test("Engine status row: read-only (no call, no handler), fed by what the Overview already polls; readiness is polled once and shared", () => {
  const row = code(rd("./components/fno/EngineStatusRow.tsx"));
  assert.ok(!/fnoApi\.|fetch\(|onClick|useFno/.test(row));
  const ov = code(rd("./components/fno/FnoOverview.tsx")); const rc = code(rd("./components/fno/FnoReadinessCard.tsx"));
  assert.ok(/<EngineStatusRow o=\{d\}/.test(ov) && /fnoApi\.readiness, 30000/.test(ov) && /<FnoReadinessCard r=\{ready\}/.test(ov));
  assert.ok(!/useFno\(/.test(rc) && /r: FnoPoll<FnoReadiness>/.test(rc), "the card no longer polls on its own");
  assert.ok(!/REAL orders allowed:/.test(ov), "the four-line lock list is gone");
  assert.ok(/<FnoStagesRow/.test(ov) && /<FnoControlCard/.test(ov), "stages row and control card are kept");
});
test("Calibration page: side list replaces the section tab bar, every section still renders, handlers and runner provider untouched", () => {
  const s = code(rd("./components/CalibrationControlTab.tsx"));
  assert.ok(/<CalNav value=\{section\}/.test(s) && /cx-layout/.test(s) && !/SECTIONS/.test(s));
  for (const id of OLD_SECTIONS) assert.ok(new RegExp(`section === "${id}"`).test(s), `section ${id} is not rendered`);
  assert.ok(/<RunnerProvider authKey/.test(s) && /<SessionNotice/.test(s) && /<RunnerBanner/.test(s) && /<ConnectionCard/.test(s) && /<RunWindow/.test(s));
  const nav = code(rd("./components/calibration/CalNav.tsx")); assert.ok(!/Api\.|fetch\(/.test(nav));
});
test("JobCard shows an inline progress bar while a job runs; the Run, Watch and Cancel handlers are unchanged", () => {
  const s = code(rd("./components/calibration/JobCard.tsx"));
  assert.ok(/role="progressbar"/.test(s) && /runProgress\(/.test(s) && /time limit used/.test(s));
  assert.ok(/start\("job", job\.id, params\(\)\)/.test(s) && /cancel\(activeRec\.id\)/.test(s) && /openRun\(activeRec\.id\)/.test(s));
});
test("Switch rows show a one-line description and the last-changed line; apply, clear and the confirm step are unchanged", () => {
  const s = code(rd("./components/calibration/SwitchesPanel.tsx"));
  assert.ok(/oneLine\(f\.help\)/.test(s) && /lastChangedText\(f\.override\)/.test(s));
  assert.ok(/controlApi\.set\(svc, row\.key, value, confirm\)/.test(s) && /controlApi\.clear\(svc, row\.key\)/.test(s) && /needsConfirm/.test(s));
});

/* ---------- CSS ---------- */
test("control.css: loads last, tokens only, no animation, no !important, no blur or gradient, no hex colour; every oc- / cx- class used is defined", () => {
  const css = stripCss(rd("./styles/control.css")); const main = rd("./main.tsx");
  const imports = [...main.matchAll(/import "\.\/([\w/-]+\.css)"/g)].map((m) => m[1]);
  assert.equal(imports[imports.length - 2], "styles/control.css");   // markets.css (E540) is the only later file
  assert.ok(!/@keyframes|animation\s*:|transition\s*:/.test(css) && !/!important/.test(css) && !/backdrop-filter|linear-gradient/.test(css));
  assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(css), "no hard-coded hex colours");
  const used = new Set();
  for (const f of ["components/fno/FnoChain.tsx", "components/fno/EngineStatusRow.tsx", "components/calibration/CalNav.tsx", "components/calibration/JobCard.tsx", "components/CalibrationControlTab.tsx"])
    for (const m of rd(`./${f}`).matchAll(/["'` ]((?:oc|cx)-[a-z0-9-]+)/g)) used.add(m[1]);
  assert.ok(used.size >= 14, `found ${used.size} classes`);
  for (const c of used) assert.ok(css.includes(`.${c}`), `.${c} missing from control.css`);
});
test("the chain never uses the profit / loss colours for calls and puts; the ATM row and the nav have a focus and selected state", () => {
  const css = stripCss(rd("./styles/control.css"));
  const chain = css.slice(css.indexOf("option chain"), css.indexOf("F&O engine status row"));
  assert.ok(!/--buy-rgb|--sell-rgb/.test(chain), "calls and puts are not green and red");
  assert.ok(/\.cx-nav-i:focus-visible/.test(css) && /\.cx-nav-i\[aria-current="page"\]/.test(css));
  assert.ok(/\.cx-prog-bar i \{[^}]*width/.test(css) === false, "the bar width comes from the page, not a fixed CSS value");
});
console.log(`${n} control tests passed`);
test("calibration/ui.tsx sits on the kit (round 8b, E547): Card, Btn and the run pill wrap kit parts, no old-layer class is left, signatures are unchanged", () => {
  const s = rd("./components/calibration/ui.tsx");
  assert.ok(/from "\.\.\/kit"/.test(s) && /<KitCard/.test(s) && /<CardHeader/.test(s) && /<Button variant=/.test(s) && /<KitPill state=/.test(s));
  assert.ok(!/btn-terminal|btn-primary-term|bg-graphite|animate-pulse/.test(s), "an old-layer class is back");
  for (const name of ["StatusPill", "Card", "Chip", "Btn", "Msg", "money", "pnlClass"]) assert.ok(new RegExp(`export (function|const) ${name}\\b`).test(s), `${name} export lost`);
  assert.ok(/role=\{kind === "err" \? "alert" : "status"\}/.test(s), "Msg keeps its alert / status role");
  const css = stripCss(rd("./styles/control.css"));
  for (const c of ["cx-chip", "cx-msg", "cx-good"]) assert.ok(css.includes(`.${c}`), `.${c} missing`);
});
test("round 8c (E548): no component uses btn-terminal any more; the nine former users render the kit Button", () => {
  for (const f of ["DecisionCard", "ui/ErrorBoundary", "Trades", "WatchlistManager", "app/OvernightAutomationPanel", "ServiceManager", "StockChart", "StockCandleBlock", "ScanPanel"]) {
    const s = rd(`./components/${f}.tsx`);
    assert.ok(!/btn-terminal/.test(s) && /<Button\b/.test(s) && /import \{[^}]*\bButton\b[^}]*\} from "\.{1,2}\/kit/.test(s), `${f} still has an old button or no kit import`);
  }
});
test("round 8d (E549): scan-action buttons are kit Buttons, SubTabs is the kit Tabs, the old subtabs and v6-kpi rules are gone", () => {
  for (const f of ["DataHealthAudit", "ScanPanel"]) { const s = rd(`./components/${f}.tsx`); assert.ok(!/scan-action-btn/.test(s) && /<Button variant=/.test(s), `${f} still has an old action button`); }
  const st = rd("./components/ui/SubTabs.tsx");
  assert.ok(/from "\.\.\/kit\/Tabs"/.test(st) && /SubTabs = Tabs/.test(st) && !/useLayoutEffect|className=/.test(st));
  const old = ["styles/base.css", "styles/legacy.css"].map((f) => rd(`./${f}`)).join("\n");
  assert.ok(!/\.subtab\b|\.subtabs\b|\.scan-action-btn|\.v6-kpi\b/.test(old), "an old rule is back");
});
