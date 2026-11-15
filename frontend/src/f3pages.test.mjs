// Run: node --experimental-strip-types src/f3pages.test.mjs  (Node >= 22.6). Round 537 (F3 part 1): the daily-use pages keep their handlers and use the kit Button.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
let n = 0; const test = (name, f) => { f(); n++; console.log("ok -", name); };
const FILES = { overview: "./components/realAuto/tabs/OverviewTab.tsx", history: "./components/realAuto/tabs/HistoryTab.tsx", rt: "./components/RealAutoTrade.tsx", ps: "./components/PositionStocksTab.tsx", parts: "./components/realAuto/parts.tsx" };

test("the five files import the kit Button and no hand-rolled money / action button is left", () => {
  for (const [k, p] of Object.entries(FILES)) assert.match(rd(p), /import \{ Button \} from "\.{1,2}(\/\.\.)?\/kit"|import \{ Button \} from "\.\/kit"/, k);
  for (const k of ["overview", "rt", "ps", "parts"]) assert.ok(!/<button[^>]*(bg-signal-|ra-btn)/.test(rd(FILES[k])), `${k} still has a hand-rolled coloured button`);
  assert.ok(!/hx-btn|ra-btn/.test(rd(FILES.history) + rd(FILES.overview)));
});
test("every money handler of the Overview tab is still wired, now on kit buttons", () => {
  const ov = rd(FILES.overview);
  for (const [h, v] of [["doArm()", "primary"], ["doDisarm()", "danger"], ["doEmergencyPause()", "danger"], ["setShowCloseAll(true)", "danger"], ["doConfirmRisk()", "soft"], ["doSaveRiskConfig()", "soft"], ["doConnectDhan()", "primary"]]) {
    assert.ok(new RegExp(`<Button variant="${v}"[^>]*onClick=\\{\\(\\) => (void )?${h.replace(/[()]/g, "\\$&")}\\}`).test(ov), `${h} as ${v}`);
  }
  assert.ok(ov.includes("<CloseAllDialog") && ov.includes("<ArmStepper"));
  assert.ok(/disabled=\{loading \|\| !gate1 \|\| !gate2 \|\| !gate3\}/.test(ov), "ARM stays disabled until the three gates are met");
});
test("Close position uses the kit loading state and the same handler; the segmented controls and row expanders are kept on purpose", () => {
  const p = rd(FILES.parts); assert.ok(/<Button variant="danger" size="sm" onClick=\{onClose\} loading=\{closing\}>/.test(p));
  assert.ok(rd(FILES.rt).includes('className="ra-seg-btn"') && rd(FILES.history).includes('className="hx-row-main"') && rd(FILES.history).includes('className="hx-seg"'));
});
test("the dead ra-btn / hx-btn rules are gone from the stylesheet", () => {
  assert.ok(!/\.(ra-btn|hx-btn)/.test(rd("./styles/base.css")));
});
console.log(`f3pages.test.mjs: ${n} ok`);
