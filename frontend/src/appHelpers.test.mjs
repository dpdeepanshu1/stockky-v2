// Run: node --experimental-strip-types src/appHelpers.test.mjs  (Node >= 22.6)
// E67 guard for the E31 split of App.tsx: the clocks, the settings page and the small helpers live in components/app/.
import assert from "node:assert/strict";
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(here, p), "utf8");
const app = read("App.tsx");

const FILES = { TabButton: ["TabButton"], BackendStatusDot: ["BackendStatusDot"], clocks: ["StatusClock", "MarketClock"],
                OvernightAutomationPanel: ["OvernightAutomationPanel"], SettingsPage: ["SettingsPage", "SettingsBanner", "powerOffAll"] };
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
assert.match(app, /import \{ SettingsPage \} from "\.\/components\/app\/SettingsPage";/);
assert.ok(app.includes("<SettingsPage"), "App renders <SettingsPage");
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
  const at = app.indexOf("<DashboardView ");
  const dcall = app.slice(at, app.indexOf("/>", at));
  for (const prop of dprops) assert.ok(new RegExp(`\\b${prop}=\\{`).test(dcall), `App passes ${prop} to DashboardView`);
}
assert.ok(/import \{ DashboardView \} from "\.\/components\/app\/DashboardView";/.test(app));
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
console.log("appHelpers.test.mjs: ok");
