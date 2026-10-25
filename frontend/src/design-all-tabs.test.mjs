// E148: design v7 on every tab. Every page header goes through PageHeader, and the tabs that have no hero of their own must pass icon + kicker
// so they get the shared v6/v7 hero (with its "Animations" switch). Source-level guards (no browser needed). Run: node --experimental-strip-types src/design-all-tabs.test.mjs
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const tc = read("./components/app/TabContent.tsx");
const ph = read("./components/ui/PageHeader.tsx");
const sp = read("./components/app/SettingsPage.tsx");
const mn = read("./main.tsx");
const nav = read("./components/ui/NavIcon.tsx");
let n = 0;
const test = (name, fn) => { fn(); n++; console.log("ok -", name); };

test("PageHeader draws the shared hero when it gets icon + kicker, and the plain header otherwise", () => {
  assert.ok(/import \{ PageHero \} from "\.\/PageHero"/.test(ph), "PageHeader must import PageHero");
  assert.ok(/<PageHero icon=\{icon\} title=\{title\} kicker=\{kicker\} subtitle=\{desc\} \/>/.test(ph), "hero branch missing");
  assert.ok(/if \(icon && kicker\)/.test(ph), "hero only when both icon and kicker are given");
  assert.ok(ph.includes('className="page-header"'), "plain header must remain");
});

test("the hero branch keeps one h1 for screen readers (the hero itself draws an h2)", () => {
  assert.ok(/<h1 className="sr-only">\{title\}<\/h1>/.test(ph), "visually hidden h1 missing");
});

test("every tab without its own hero passes icon + kicker", () => {
  for (const title of ["Alerts", "Training Lab", "Paper Trades", "Data Feed", "Hot Picks", "Surprise Momentum", "IPO Tracker", "Watchlist"]) {
    const m = tc.match(new RegExp(`<PageHeader title="${title}"[^>]*/>`));
    assert.ok(m, `${title}: PageHeader not found`);
    assert.ok(/ icon="[a-z]+"/.test(m[0]) && / kicker="[A-Za-z]+"/.test(m[0]), `${title} must pass icon and kicker`);
  }
  assert.ok(/<PageHeader title="Settings"[^>]*icon="[a-z]+"[^>]*kicker="[A-Za-z]+"/.test(sp), "Settings must pass icon and kicker");
});

test("the three pages that draw their own hero keep the plain header above it (no second hero)", () => {
  for (const title of ["Auto Trade", "Position Stocks", "Real Money Calibration"]) {
    const m = tc.match(new RegExp(`<PageHeader title="${title}"[^>]*/>`));
    assert.ok(m && !/ icon=/.test(m[0]), `${title} must stay a plain header (its page draws the hero)`);
  }
});

test("icons used are real nav icons", () => {
  const names = new Set([...nav.matchAll(/"([a-z]+)"/g)].map((m) => m[1]));
  for (const m of [...tc.matchAll(/ icon="([a-z]+)"/g), ...sp.matchAll(/ icon="([a-z]+)"/g)]) assert.ok(names.has(m[1]), `icon ${m[1]} is not a NavIcon name`);
});

test("kickers match the sidebar groups", () => {
  const app = read("./App.tsx");
  const groups = { Alerts: "System", "Training Lab": "System", "Paper Trades": "Trading", "Data Feed": "System", "Hot Picks": "Discover", "Surprise Momentum": "Discover", "IPO Tracker": "Discover", Watchlist: "Discover" };
  for (const [title, kick] of Object.entries(groups)) {
    assert.ok(new RegExp(`<PageHeader title="${title}"[^>]*kicker="${kick}"`).test(tc), `${title} kicker should be ${kick}`);
    assert.ok(new RegExp(`label: "${title}"[^}]*group: "${kick}"`).test(app), `${title} sits in sidebar group ${kick}`);
  }
});

test("no hard-coded hex colours in the files touched", () => {
  for (const [name, src] of [["PageHeader", ph], ["TabContent", tc], ["SettingsPage", sp]]) assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(src.replace(/&#\d+;/g, "")), `${name} has a hex colour`);
});

test("the saved 'Animations: Off' choice is applied at start-up, not only when a hero mounts (F-09)", () => {
  assert.ok(/getItem\("stockky:v6_calm"\) === "1"/.test(mn) && /classList\.add\("v6-calm"\)/.test(mn), "main.tsx must apply v6-calm at start");
  assert.ok(/try \{[^\n]*stockky:v6_calm[^\n]*\} catch/.test(mn), "storage read must be wrapped in try/catch");
  assert.ok(mn.indexOf("v6-calm") < mn.indexOf("ReactDOM.createRoot"), "applied before the first render");
});

console.log(`${n} all-tabs design tests passed`);
