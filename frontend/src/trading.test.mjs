// E537 (redesign round 5): guards for the shared trading header, grouped tabs and arming stepper. Run: node --experimental-strip-types src/trading.test.mjs
import { readFileSync } from "node:fs";
import { cascadeOrder } from "./cssText.mjs";
import assert from "node:assert/strict";
import * as t from "./components/trading/tradingGroups.ts";
import { psSource } from "./psSource.mjs";

const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const stripCss = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "");
const code = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
let n = 0; const test = (name, f) => { f(); n++; console.log("ok -", name); };

const RT = ["overview", "board", "live", "positions", "orders", "history", "watchlist", "pipeline", "charges", "log"];
const PS = ["overview", "board", "timemachine", "pipeline", "screener", "positions", "history", "dhanorders", "charges", "settings"];

test("Auto Trade: five groups (Now, Orders, History, Pipeline, Log) that hold every one of the ten old tab ids exactly once", () => {
  assert.deepEqual(t.RT_GROUPS.map((g) => g.label), ["Now", "Orders", "History", "Pipeline", "Log"]);
  assert.ok(t.groupsCover(t.RT_GROUPS, RT));
  assert.deepEqual(t.RT_GROUPS.find((g) => g.id === "now").tabs, ["overview", "live", "positions"]);
  assert.deepEqual(t.RT_GROUPS.find((g) => g.id === "history").tabs, ["history", "charges"]);
  assert.deepEqual(t.RT_GROUPS.find((g) => g.id === "pipeline").tabs, ["pipeline", "board", "watchlist"]);
});
test("Position Stocks: five groups (Now, Board, Screener, History, Tools) that hold every one of the ten old sub-tab ids exactly once", () => {
  assert.deepEqual(t.PS_GROUPS.map((g) => g.label), ["Now", "Board", "Screener", "History", "Tools"]);
  assert.ok(t.groupsCover(t.PS_GROUPS, PS));
  assert.deepEqual(t.PS_GROUPS.find((g) => g.id === "history").tabs, ["history", "charges", "dhanorders"]);
  assert.deepEqual(t.PS_GROUPS.find((g) => g.id === "tools").tabs, ["timemachine", "pipeline", "settings"]);
});
test("groupsCover fails when an id is missing or repeated", () => {
  assert.ok(!t.groupsCover([{ id: "a", label: "A", tabs: ["x"] }], ["x", "y"]));
  assert.ok(!t.groupsCover([{ id: "a", label: "A", tabs: ["x"] }, { id: "b", label: "B", tabs: ["x", "y"] }], ["x", "y"]));
});
test("a group click keeps the page you are on inside that group, otherwise opens its first page", () => {
  assert.equal(t.tabForGroup(t.RT_GROUPS, "now", "live"), "live");
  assert.equal(t.tabForGroup(t.RT_GROUPS, "history", "live"), "history");
  assert.equal(t.tabForGroup(t.PS_GROUPS, "tools", "overview"), "timemachine");
  assert.equal(t.tabForGroup(t.PS_GROUPS, "nope", "overview"), "overview");
  assert.equal(t.groupOf(t.RT_GROUPS, "charges").id, "history");
});
test("header: REAL / DEMO in words, armed state, signed P&L with arrow, capital in use = total - idle, dash for what is missing", () => {
  const v = t.headerView({ mode: "REAL", armed: true, pnl: 1250, total: 100000, idle: 60000 });
  assert.equal(v.modeText, "REAL money"); assert.ok(v.real); assert.equal(v.armedText, "Armed"); assert.equal(v.armedState, "armed");
  assert.equal(v.pnlText, "▲ +₹1,250"); assert.equal(v.pnlTone, "up"); assert.equal(v.inUseText, "₹40,000"); assert.equal(v.idleText, "₹60,000"); assert.ok(v.canPause);
  const d = t.headerView({ mode: "DEMO", armed: false, pnl: -340.4, total: null, idle: 5000 });
  assert.equal(d.modeText, "DEMO (practice)"); assert.equal(d.armedText, "Not armed"); assert.equal(d.pnlText, "▼ -₹340"); assert.equal(d.inUseText, null); assert.equal(d.idleText, "₹5,000"); assert.ok(!d.canPause);
  const z = t.headerView({ mode: "REAL", armed: null, pnl: 0, total: undefined, idle: undefined });
  assert.equal(z.armedState, null); assert.equal(z.pnlText, "₹0"); assert.equal(z.pnlTone, "flat"); assert.equal(z.inUseText, null);
  assert.equal(t.headerView({ mode: "REAL", armed: true, pnl: null, total: 1, idle: 5 }).inUseText, "₹0", "idle above total never shows negative capital in use");
});
test("header: a tripped loss limit shows as halted and the pause button is off", () => {
  const v = t.headerView({ mode: "REAL", armed: true, killed: true, pnl: -5000, total: 1, idle: 1 });
  assert.equal(v.armedText, "Halted by loss limit"); assert.equal(v.armedState, "offline"); assert.ok(!v.canPause);
});
test("arming steps: four old gates become three steps; the first open step is current; the hint still names what is missing", () => {
  const s = (g) => t.armSteps(g);
  let x = s({ gate1: false, gate2: false, gate3: false, armed: false });
  assert.deepEqual(x.map((a) => a.state), ["current", "todo", "todo"]); assert.equal(x[0].hint, "Admin login needed");
  x = s({ gate1: true, gate2: false, gate3: false, armed: false }); assert.equal(x[0].hint, "Dhan is not connected"); assert.equal(x[0].state, "current");
  x = s({ gate1: true, gate2: true, gate3: false, armed: false }); assert.deepEqual(x.map((a) => a.state), ["done", "current", "todo"]);
  x = s({ gate1: true, gate2: true, gate3: true, armed: false }); assert.deepEqual(x.map((a) => a.state), ["done", "done", "current"]); assert.equal(x[2].hint, "Ready to arm");
  x = s({ gate1: true, gate2: true, gate3: true, armed: true }); assert.deepEqual(x.map((a) => a.state), ["done", "done", "done"]); assert.equal(x.length, 3);
  assert.equal(s({ gate1: true, gate2: true, gate3: false, armed: false, dhanRequired: false })[0].label, "Login");
});
test("Auto Trade page: header is fed from account fields, uses its own emergency pause, tabs are grouped, no tab id or body changed", () => {
  const rt = rd("./components/RealAutoTrade.tsx");
  assert.ok(rt.includes("<TradingHeader") && rt.includes("onPause={doEmergencyPause}") && rt.includes("status.account?.realized_pnl_today"));
  assert.ok(/<GroupTabs groups=\{RT_GROUPS\} items=\{tabs\}/.test(rt) && !rt.includes("<SubTabs"));
  for (const id of RT) assert.ok(rt.includes(`{ id: "${id}"`) && rt.includes(`activeTab === "${id}"`), id);
  assert.ok(rt.includes("window.confirm(\"Pause ALL trading"), "the pause keeps its confirm");
});
test("Position Stocks page: header uses the ledger, pause is the existing disarm action behind a second click, tabs are grouped", () => {
  const ps = psSource();
  assert.ok(ps.includes("<TradingHeader") && ps.includes('onPause={() => doAction("disarm")}') && ps.includes('pauseConfirm="Click again to disarm"'));
  assert.ok(ps.includes("ledger?.realized_pnl_today") && ps.includes("ledger?.total_allocated_capital") && ps.includes("ledger?.available_capital"));
  assert.ok(/<GroupTabs<SubTab>\s+groups=\{PS_GROUPS\}/.test(ps) && !ps.includes("<SubTabs"));
  for (const id of PS) assert.ok(ps.includes(`{ id: "${id}"`) && ps.includes(`subTab === "${id}"`), id);
  assert.ok(!/doAction\("kill"\)[\s\S]{0,40}TradingHeader/.test(ps), "the header never offers the kill action");
});
test("both pages use the stepper; the old GateStep boxes are gone; ARM / DISARM / PAUSE ALL / CLOSE ALL buttons are still in the overview", () => {
  const ov = rd("./components/realAuto/tabs/OverviewTab.tsx"); const ps = psSource();
  assert.ok(ov.includes("<ArmStepper") && ps.includes("<ArmStepper") && !ov.includes("<GateStep") && !ps.includes("<GateStep"));
  for (const b of ["doArm()", "doDisarm()", "doEmergencyPause()", "setShowCloseAll(true)", "<CloseAllDialog"]) assert.ok(ov.includes(b), b);
});
test("header and tabs make no call and hold no trading handler of their own", () => {
  for (const f of ["TradingHeader.tsx", "GroupTabs.tsx", "ArmStepper.tsx", "tradingGroups.ts"]) {
    const s = code(rd(`./components/trading/${f}`));
    assert.ok(!/fetch\(|Api\.|api\.|\.arm\(|\.disarm\(|\.kill\(|closeAll/.test(s), `${f} calls something`);
  }
});
test("AdminGate wrapping and session code are not touched by this round", () => {
  assert.ok(!/AdminGate|adminAuth/.test(code(rd("./components/trading/TradingHeader.tsx") + rd("./components/trading/GroupTabs.tsx"))));
});
test("trading.css: loads second to last, tokens only, no animation, no !important, no blur or gradient, no hex colour; every tr- class used is defined", () => {
  const css = stripCss(rd("./styles/trading.css")); const main = rd("./main.tsx");
  const imports = cascadeOrder();   // E558: base, tokens, components, then the page sheets
  assert.equal(imports[imports.length - 3], "styles/trading.css");   // control.css (E539) and markets.css (E540) are the only later files
  assert.ok(!/@keyframes|animation\s*:|transition\s*:/.test(css) && !/!important/.test(css) && !/backdrop-filter|linear-gradient/.test(css));
  assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(css));
  const used = new Set();
  for (const f of ["TradingHeader.tsx", "GroupTabs.tsx", "ArmStepper.tsx"]) for (const m of rd(`./components/trading/${f}`).matchAll(/["'` ]((?:tr)-[a-z0-9-]+)/g)) used.add(m[1]);
  assert.ok(used.size >= 8); for (const c of used) assert.ok(css.includes(`.${c}`), `.${c} missing from trading.css`);
});
console.log(`${n} trading tests passed`);
