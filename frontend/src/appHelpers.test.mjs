// Run: node --experimental-strip-types src/appHelpers.test.mjs  (Node >= 22.6)
// Guard for the E31 split of App.tsx (E67-E70): the clocks, settings page, nav, dashboard view and tab switch live in components/app/, the state in hooks/.
import assert from "node:assert/strict";
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(here, p), "utf8");
const app = read("App.tsx");

const FILES = { clocks: ["StatusClock", "MarketClock"],
                OvernightAutomationPanel: ["OvernightAutomationPanel"], SettingsPage: ["SettingsPage", "powerOffAll"] };
for (const [file, fns] of Object.entries(FILES)) {
  assert.ok(existsSync(join(here, `components/app/${file}.tsx`)), `${file}.tsx exists`);
  const src = read(`components/app/${file}.tsx`);
  for (const fn of fns) {
    assert.match(src, new RegExp(`function ${fn}\\b`), `${fn} defined in ${file}.tsx`);
    assert.ok(!new RegExp(`^(async )?function ${fn}\\b`, "m").test(app), `App.tsx no longer defines ${fn}`);
  }
}
// App.tsx still renders what it used to, now imported.
assert.ok(!app.includes("MarketClock"), "MarketClock is rendered by DashboardView now");  // StatusClock moved into DesktopTopBar (AppNav.tsx)
// E70: the tab switch (and so <SettingsPage>) lives in TabContent.tsx; see the E70 block below.
assert.ok(read("components/app/TabContent.tsx").includes("<SettingsPage"), "TabContent renders <SettingsPage");
assert.ok(read("components/app/DashboardView.tsx").includes("<MarketClock />"), "the dashboard still shows the market clock");
assert.ok(read("components/app/AppNav.tsx").includes("<StatusClock />"), "the desktop top bar still shows the status clock");

// Moved code must not keep a path relative to the old folder (a dynamic import("./api") would only fail at run time).
for (const f of readdirSync(join(here, "components/app"))) {
  const src = read(`components/app/${f}`);
  assert.ok(!/import\(\s*["']\.\/api["']\s*\)/.test(src), `${f}: dynamic import of ./api must be ../../api`);
  for (const m of src.matchAll(/^import .* from "(\.[^"]+)";/gm)) {
    assert.ok(m[1].startsWith("../../") || m[1].startsWith("../") || m[1].startsWith("./"), m[1]);
    const target = join(here, "components/app", m[1]);
    assert.ok(["", ".ts", ".tsx"].some((e) => existsSync(target + e)), `${f}: import ${m[1]} resolves`);
  }
}

// E68: navigation and top bars moved to components/app/AppNav.tsx.
const nav = read("components/app/AppNav.tsx");
const NAV = { DesktopSidebar: "terminal-sidebar", MobileTopBar: "terminal-topbar", MobileDrawer: "mobile-drawer-panel",
              DesktopTopBar: "app-topbar", BackendDownBanner: "Wake Backend" };
for (const [fn, marker] of Object.entries(NAV)) {
  assert.match(nav, new RegExp(`export function ${fn}\\(`), `${fn} exported`);
  assert.ok(nav.includes(marker), `${fn} keeps ${marker}`);
  assert.ok(!app.includes(marker), `App.tsx no longer holds ${marker}`);
  assert.ok(app.includes(`<${fn} `), `App renders <${fn}`);
  // every prop of the component is passed by App
  const sig = nav.slice(nav.indexOf(`export function ${fn}(`));
  const destructured = sig.slice(sig.indexOf("({") + 2, sig.indexOf("}: {")).split(",").map((x) => x.trim()).filter(Boolean);
  assert.ok(destructured.length >= 4, `${fn} props parsed`);
  const callStart = app.indexOf(`<${fn} `);
  const call = app.slice(callStart, app.indexOf("/>", callStart));
  for (const prop of destructured) assert.ok(new RegExp(`\\b${prop}=\\{`).test(call), `App passes ${prop} to ${fn}`);
}
assert.ok(nav.includes('aria-label="Main navigation"') && nav.includes('aria-label="Menu"'), "nav landmarks and the drawer dialog label kept");
assert.ok(existsSync(join(here, "components/app/appTypes.ts")) && /import type \{ NavItem, Tab(, ViewState)? \} from "\.\/components\/app\/appTypes";/.test(app));
assert.ok(!/^type Tab =/m.test(app), "Tab type lives in appTypes.ts");

// E69: the dashboard view moved to components/app/DashboardView.tsx.
const dash = read("components/app/DashboardView.tsx");
assert.match(dash, /export function DashboardView\(/);
for (const piece of ["Analyse any stock", 'aria-label="Symbol search"', "Quick scan", "Full market scan", "<MarketSentimentHeader", "<MarketMovers",
                     "<BreadthStrip", "<Pipeline", "<DecisionCard", "<ScanPanel", "<SignalStream", 'view.mode === "idle"', 'view.mode === "loading"',
                     'view.mode === "error"', 'view.mode === "stock"', 'view.mode === "scan"', "handleSendAllActionable"]) {
  assert.ok(dash.includes(piece), `DashboardView keeps ${piece}`);
}
for (const gone of ["Analyse any stock", 'aria-label="Symbol search"', "<DecisionCard", "<ScanPanel", "<MarketMovers", "function formatTime("]) {
  assert.ok(!app.includes(gone), `App.tsx no longer holds ${gone}`);
}
{
  const dprops = dash.slice(dash.indexOf("export function DashboardView({") + "export function DashboardView({".length, dash.indexOf("}: DashboardViewProps)"))
    .split(",").map((x) => x.trim()).filter(Boolean);
  assert.ok(dprops.length >= 20, "DashboardView props parsed");
  const at = app.indexOf("const dashboard: DashboardViewProps = {");
  assert.ok(at > 0, "App builds the dashboard props object");
  const dcall = app.slice(at, app.indexOf("};", at));
  for (const prop of dprops) assert.ok(new RegExp(`\\b${prop}\\b`).test(dcall), `App passes ${prop} to DashboardView (via the dashboard object)`);
}
assert.ok(/import \{ DashboardView, type DashboardViewProps \} from "\.\/DashboardView";/.test(read("components/app/TabContent.tsx")));
assert.ok(/export type ViewState =/.test(read("components/app/appTypes.ts")) && !/^type ViewState =/m.test(app), "ViewState lives in appTypes.ts");
// formatTime is pure: run the moved source (types stripped) and check the three output shapes.
{
  const m = dash.match(/function formatTime\(seconds: number\): string \{[\s\S]*?\n\}/);
  assert.ok(m, "formatTime found in DashboardView");
  const formatTime = new Function(`${m[0].replace("seconds: number): string", "seconds)")}; return formatTime;`)();
  assert.equal(formatTime(0), "0s"); assert.equal(formatTime(59), "59s"); assert.equal(formatTime(61), "1m 1s");
  assert.equal(formatTime(3661), "1h 1m 1s"); assert.equal(formatTime(-5), "0s"); assert.equal(formatTime(NaN), "0s"); assert.equal(formatTime(12.9), "12s");
}
// the ErrorBoundary / skip-link contract of design-v4.test.mjs is still in App.tsx
assert.ok(/<ErrorBoundary key=\{tab\}/.test(app) && /<main id="main-content"/.test(app));

// E70: the tab switch moved to TabContent.tsx and the state / handlers moved to hooks/.
const tc = read("components/app/TabContent.tsx");
assert.match(tc, /export function TabContent\(/);
assert.match(tc, /export const loadTrades = /, "loadTrades is exported (App fires it before the open-deposit event)");
for (const piece of ['PageHeader title="Alerts"', 'PageHeader title="Training Lab"', 'PageHeader title="Paper Trades"', 'PageHeader title="Data Feed"',
                     'PageHeader title="Hot Picks"', 'PageHeader title="Surprise Momentum"', 'PageHeader title="IPO Tracker"', 'AdminGate title="Auto Trade"',
                     'AdminGate title="Position Stocks"', 'AdminGate title="Real Money Calibration"', "<SettingsPage", 'PageHeader title="Watchlist"',
                     "<DashboardView {...dashboard} />", "<RealAutoTrade />", "<PositionStocksTab />", "<CalibrationControlTab />", "<DataHealthAudit />"]) {
  assert.ok(tc.includes(piece), `TabContent keeps ${piece}`);
}
// every lazy page is still its own chunk and resolves from components/app/
for (const m of tc.matchAll(/lazy\(\(\) => import\("([^"]+)"\)\)|import\("([^"]+)"\)/g)) {
  const target = join(here, "components/app", m[1] || m[2]);
  assert.ok(["", ".ts", ".tsx"].some((e) => existsSync(target + e)), `lazy import ${m[1] || m[2]} resolves`);
}
assert.equal([...tc.matchAll(/= lazy\(/g)].length, 11, "the same 11 lazy pages as before (Trades via loadTrades)");
for (const gone of ["lazy(", 'PageHeader title=', "<AdminGate", "<SettingsPage", "<RealAutoTrade", "<CalibrationControlTab"]) {
  assert.ok(!app.includes(gone), `App.tsx no longer holds ${gone}`);
}
assert.ok(/<Suspense fallback=[\s\S]*<TabContent [\s\S]*<\/Suspense>\s*<\/ErrorBoundary>/.test(app), "TabContent sits inside the Suspense and the per-tab ErrorBoundary");
{
  const tprops = tc.slice(tc.indexOf("export function TabContent({") + "export function TabContent({".length, tc.indexOf("}: TabContentProps)"))
    .split(",").map((x) => x.trim()).filter(Boolean);
  assert.ok(tprops.length >= 11, "TabContent props parsed");
  const at = app.indexOf("<TabContent ");
  const tcall = app.slice(at, app.indexOf("/>", at));
  for (const prop of tprops) assert.ok(new RegExp(`\\b${prop}=\\{`).test(tcall), `App passes ${prop} to TabContent`);
}

// hooks
const HOOKS = {
  useWatchlist: ["handleWatchlistUpdate", "handleAddToWatchlist", "handleAddManyToWatchlist"],
  useBackendStatus: ["checkBackend", "handleWakeBackend"],
  useScanRunner: ["clearScanActivity", "handleRealtime", "handleSearch", "handleScan", "handleScanWatchlist", "pollScanStatus", "handleStopScan", "handleRetry",
                  "handleSendTopPicks", "handleSendAllActionable"],
};
for (const [hook, fns] of Object.entries(HOOKS)) {
  const h = read(`hooks/${hook}.ts`);
  assert.match(h, new RegExp(`export function ${hook}\\(`), `${hook} exported`);
  for (const fn of fns) {
    assert.ok(new RegExp(`(function|const) ${fn}\\b`).test(h), `${fn} defined in ${hook}`);
    assert.ok(!new RegExp(`(function|const) ${fn}\\b`).test(app), `App.tsx no longer defines ${fn}`);
  }
  assert.ok(app.includes(`${hook}(`) && app.includes(`import { ${hook} } from "./hooks/${hook}";`), `App calls ${hook}`);
  for (const m of h.matchAll(/^import .* from "(\.[^"]+)";/gm)) {
    const target = join(here, "hooks", m[1]);
    assert.ok(["", ".ts", ".tsx"].some((e) => existsSync(target + e)), `${hook}: import ${m[1]} resolves`);
  }
}
// call order = the order the old effects ran in: watchlist (gives setWatchlist), then the backend ping, then the scan restore / resume
assert.ok(app.indexOf("useWatchlist(") < app.indexOf("useBackendStatus(") && app.indexOf("useBackendStatus(") < app.indexOf("useScanRunner("), "hook call order");
assert.ok(read("hooks/useBackendStatus.ts").includes("setWatchlist(r.symbols)"), "a good ping still reloads the watchlist");
{
  const sr = read("hooks/useScanRunner.ts");
  assert.ok(!sr.includes("checkBackend"), "the scan runner does not ping the backend itself (useBackendStatus does)");
  for (const key of ["stockky_scan_task_id", "stockky_last_scan", "stockky_last_analysis"]) assert.ok(sr.includes(key), `scan runner keeps ${key}`);
  for (const piece of ["api.scanStream(", "api.scanStart(", "api.scanCancel(", "api.stopAllScans(", "subscribeScan(", "useStockkyRealtime(handleRealtime)", "api.sendPicksToTelegram("]) {
    assert.ok(sr.includes(piece), `scan runner keeps ${piece}`);
  }
  for (const gone of ["api.scanStream(", "api.scanStart(", "useStockkyRealtime", "api.sendPicksToTelegram(", "api.getStock(", "sessionStorage"]) {
    assert.ok(!app.includes(gone), `App.tsx no longer holds ${gone}`);
  }
}

// E70: the three components nothing used are gone, and the two "settings" buttons now open the Settings page (they used to set a flag nobody rendered).
for (const f of ["TabButton", "BackendStatusDot"]) assert.ok(!existsSync(join(here, `components/app/${f}.tsx`)), `${f}.tsx deleted`);
assert.ok(!read("components/app/SettingsPage.tsx").includes("SettingsBanner"), "SettingsBanner deleted");
assert.ok(!app.includes("showSettings") && !app.includes("setShowSettings"), "the dead showSettings flag is gone");
assert.ok(app.includes('const openSettings = () => goTo("settings");') && app.includes("openSettings={openSettings}"), "App wires openSettings");
assert.ok(app.includes('backendUp === "down" && tab !== "settings"'), "the backend-down banner hides on the Settings page itself");
assert.ok(read("components/app/AppNav.tsx").includes("onClick={openSettings}") && dash.includes("onClick={openSettings}"), "both buttons call openSettings");

// buildScanResultFromRows is pure: run it.
{
  const { buildScanResultFromRows } = await import("./hooks/scanResult.ts");
  const rows = [{ symbol: "A", decision: "BUY NOW" }, { symbol: "B", decision: "PREPARE TO BUY" }, { symbol: "C", decision: "SELL" },
                { symbol: "D", decision: "HOLD" }, { symbol: "E", decision: "WAIT" }, { symbol: "F", decision: "DO NOT BUY", error: "boom" }];
  const done = buildScanResultFromRows(rows, 6, false);
  assert.equal(done.scanned, 6); assert.equal(done.universe_size, 6);
  assert.deepEqual(done.recommendations.map((r) => r.symbol), ["A", "B", "C"]);
  assert.deepEqual(done.market_stats, { buy_signals: 2, sell_signals: 1, hold_signals: 1, cautious: 2 });
  assert.equal(done.market_mood, "bullish"); assert.equal(done.verdict, "Stream complete: 6 symbols");
  assert.deepEqual(done.errors, [{ symbol: "F", error: "boom" }]);
  assert.ok(!("partial" in done), "a complete run is not marked partial");
  const part = buildScanResultFromRows(rows.slice(0, 3), 10, true);
  assert.equal(part.verdict, "Partial stream: 3/10"); assert.equal(part.universe_size, 10); assert.equal(part.partial, true); assert.equal(part.stopped_early, true);
  assert.equal(buildScanResultFromRows([], 0, false).market_mood, "mixed");
  assert.equal(buildScanResultFromRows([{ symbol: "S", decision: "SELL" }], 1, false).market_mood, "bearish");
  assert.equal(buildScanResultFromRows([{ symbol: "S" }], 0, false).universe_size, 1, "total 0 falls back to the row count");
}

console.log("appHelpers.test.mjs: ok");
