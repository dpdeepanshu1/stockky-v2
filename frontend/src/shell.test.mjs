// E535: guards for redesign round 3, the app shell (run: node --experimental-strip-types src/shell.test.mjs). Pure logic is run; the rest is static checks on source and CSS.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { psSource } from "./psSource.mjs";   // E538
const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const stripCss = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");
const nav = await import("./components/app/navModel.ts");
const ms = await import("./components/app/modeStrip.ts");
const adm = await import("./adminSession.ts");
let n = 0; const test = (name, f) => { f(); n++; console.log("ok -", name); };

const ORIGINAL_TABS = ["dashboard", "notifications", "training", "trades", "hot", "surprise", "ipo", "datafeed", "settings", "watchlist", "realtrade", "positionstocks", "fno", "calibration"];

/* ---------- navigation model ---------- */
test("the 14 tab ids are exactly the old ones (no route or tab id changed)", () => {
  assert.deepEqual([...nav.NAV_ITEMS.map((i) => i.id)].sort(), [...ORIGINAL_TABS].sort());
  const t = rd("./components/app/appTypes.ts");
  for (const id of ORIGINAL_TABS) assert.ok(t.includes(`"${id}"`), `Tab type lost ${id}`);
});
test("five sections hold every tab exactly once, and each entry page belongs to its own section", () => {
  assert.deepEqual(nav.SECTIONS.map((s) => s.id), ["home", "markets", "trading", "paper", "system"]);
  const all = nav.SECTIONS.flatMap((s) => s.tabs);
  assert.equal(all.length, 14); assert.equal(new Set(all).size, 14);
  for (const s of nav.SECTIONS) assert.ok(s.tabs.includes(s.entry), `${s.id} entry`);
});
test("Markets = Hot Picks, Surprise, IPO, Watchlist; Trading = the four admin-login pages", () => {
  assert.deepEqual(nav.MARKETS_TABS, ["hot", "surprise", "ipo", "watchlist"]);
  assert.deepEqual(nav.TRADING_TABS, ["realtrade", "positionstocks", "fno", "calibration"]);
  assert.ok(nav.SECTIONS.find((s) => s.id === "trading").locked);
});
test("Paper Trades never shares a section with the real-money pages", () => {
  assert.equal(nav.sectionOf("trades").id, "paper");
  for (const t of nav.TRADING_TABS) assert.notEqual(nav.sectionOf(t).id, "paper");
  assert.equal(nav.isTradingTab("trades"), false);
});
test("System is folded by default; every item's group equals its section label (page kickers rely on it)", () => {
  assert.equal(nav.SECTIONS.find((s) => s.id === "system").foldedByDefault, true);
  for (const i of nav.NAV_ITEMS) assert.equal(i.group, nav.sectionOf(i.id).id === "home" ? "" : nav.sectionOf(i.id).label, i.id);
});
test("a section click keeps the page you are on inside it, otherwise opens its first page", () => {
  assert.equal(nav.sectionTarget("dashboard", "markets"), "hot");
  assert.equal(nav.sectionTarget("ipo", "markets"), "ipo");
  assert.equal(nav.sectionTarget("settings", "trading"), "realtrade");
  assert.equal(nav.sectionTarget("fno", "trading"), "fno");
});
test("phone bar: five slots in order Home, Markets, Trading, Alerts, More", () => {
  assert.deepEqual(nav.BOTTOM_SLOTS.map((s) => s.id), ["home", "markets", "trading", "alerts", "more"]);
});
test("phone bar: every tab lights exactly one slot; an open sheet or drawer wins", () => {
  for (const t of ORIGINAL_TABS) { const a = nav.activeSlot(t, null); assert.ok(nav.BOTTOM_SLOTS.some((s) => s.id === a), t); }
  assert.equal(nav.activeSlot("dashboard", null), "home"); assert.equal(nav.activeSlot("hot", null), "markets");
  assert.equal(nav.activeSlot("calibration", null), "trading"); assert.equal(nav.activeSlot("notifications", null), "alerts");
  assert.equal(nav.activeSlot("trades", null), "more"); assert.equal(nav.activeSlot("settings", null), "more");
  assert.equal(nav.activeSlot("hot", "trading"), "trading"); assert.equal(nav.activeSlot("hot", "more"), "more");
});
test("the More drawer lists Paper and System, so every page stays reachable on a phone", () => {
  assert.deepEqual(nav.DRAWER_SECTIONS, ["paper", "system"]);
  const reachable = new Set([...nav.SECTIONS.find((s) => s.id === "home").tabs, ...nav.MARKETS_TABS, ...nav.TRADING_TABS, ...nav.DRAWER_SECTIONS.flatMap((id) => nav.itemsOf(id).map((i) => i.id)), "notifications"]);
  for (const t of ORIGINAL_TABS) assert.ok(reachable.has(t), t);
});

/* ---------- login is still the one shared gate ---------- */
test("the four Trading pages are still wrapped in AdminGate, with the same titles; no other page is", () => {
  const tc = rd("./components/app/TabContent.tsx");
  for (const t of ["Auto Trade", "Position Stocks", "F&O Options", "Real Money Calibration"]) assert.ok(tc.includes(`<AdminGate title="${t}"`), t);
  assert.equal([...tc.matchAll(/<AdminGate /g)].length, 4);
});
test("AdminGate still logs in through adminAuth and keeps its gate state machine; storage keys are untouched", () => {
  const g = rd("./components/AdminGate.tsx");
  for (const piece of ["loginAll", "changePasswordAll", "logoutEverywhere", "nextGatePhase", "initialGatePhase"]) assert.ok(g.includes(piece), piece);
  assert.ok(!/localStorage\.setItem\(["'`]stockky[_:]admin/.test(g));
});
test("the old per-page login code is gone (doLogin, username / password fields) from the three pages", () => {
  for (const f of ["./components/RealAutoTrade.tsx", "./components/PositionStocksTab.tsx", "./components/realAuto/tabs/OverviewTab.tsx"]) {
    const s = f.endsWith("PositionStocksTab.tsx") ? psSource() : rd(f);
    for (const gone of ["doLogin", "rt-admin-user", "rt-admin-pass", "ps-admin-user", "ps-admin-pass", "rt-login", "Admin login required", "setUsername", "setPassword"]) assert.ok(!s.includes(gone), `${f} still has ${gone}`);
    assert.ok(s.includes("SessionMissingNote"), `${f} uses SessionMissingNote`);
  }
});
test("the pages follow the shared login: they re-read their token when the gate announces a change (Extend / re-login)", () => {
  for (const f of ["./components/RealAutoTrade.tsx", "./components/PositionStocksTab.tsx"]) {
    const s = f.endsWith("PositionStocksTab.tsx") ? psSource() : rd(f);
    assert.ok(/addEventListener\(AUTH_CHANGED_EVENT/.test(s), f);
  }
});
test("login page: names the page being unlocked; no looping animation, shake or lock animation", () => {
  const g = rd("./components/AdminGate.tsx"); const c = [...stripCss(rd("./styles/legacy.css")).matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(([, sel]) => /\.auth-/.test(sel)).map((m) => m[0]).join("\n");   // round 8i: the login rules inside the folded legacy file
  assert.ok(g.includes("heading={`Unlock ${title}`}") && g.includes("aria-label={`Unlock ${title}`}"));
  assert.ok(!/@keyframes|animation\s*:/.test(c), "the login rules have no animation");
  assert.ok(!/backdrop-filter|linear-gradient/.test(c), "flat: no blur, no gradient");
  assert.ok(/font-size:\s*16px/.test(c), "16px inputs (no iOS zoom)");
  assert.ok(g.includes("autoFocus") && g.includes('autoComplete="current-password"') && g.includes('autoComplete="username"'));
  assert.ok(/\.auth-error::before\s*\{\s*content:\s*"✕ "/.test(c), "errors are not colour-only");
});

/* ---------- session pill ---------- */
test("pillTime: hours for long sessions, minutes while there is time, m:ss at 5 minutes or less, 'Admin' when unknown", () => {
  assert.equal(adm.PILL_WARN_SECONDS, 300);
  assert.equal(adm.pillTime(null), "Admin"); assert.equal(adm.pillTime(24 * 60), "24 min"); assert.equal(adm.pillTime(24 * 60 - 30), "24 min");
  assert.equal(adm.pillTime(301), "6 min"); assert.equal(adm.pillTime(300), "5:00"); assert.equal(adm.pillTime(59), "0:59");
  assert.equal(adm.pillTime(8 * 3600 - 60), "7h 59m"); assert.equal(adm.pillTime(-5), "0:00");
});
test("session pill: warning colour and Extend at 5 minutes, one-click Log out, Change password in the menu; the old chip is gone", () => {
  const g = rd("./components/AdminGate.tsx");
  for (const piece of ["export function SessionPill", "PILL_WARN_SECONDS", "Extend", "onClick={logoutEverywhere}", "Change password", 'startMode="change"']) assert.ok(g.includes(piece), piece);
  assert.ok(!g.includes("export function SessionChip"));
  assert.ok(!/SessionChip/.test(rd("./components/app/AppNav.tsx")));
  assert.ok(/\.sh-pill\.warn/.test(rd("./styles/shell.css")));
});

/* ---------- mode strip ---------- */
test("signedRupees: sign is in the text, Indian grouping, no minus on a zero", () => {
  assert.equal(ms.signedRupees(1250), "+₹1,250"); assert.equal(ms.signedRupees(-340), "-₹340"); assert.equal(ms.signedRupees(0), "₹0");
  assert.equal(ms.signedRupees(-0.2), "₹0"); assert.equal(ms.signedRupees(1234567), "+₹12,34,567");
});
test("strip: no snapshot = Locked; the control room always says it changes real-money rules; logged in but nothing yet is not 'Locked'", () => {
  const v = ms.stripView("realtrade", null); assert.equal(v.tone, "locked"); assert.ok(/log in/i.test(v.hint));
  const c = ms.stripView("calibration", null); assert.equal(c.tone, "control"); assert.ok(/real-money/i.test(c.hint));
  const w = ms.stripView("realtrade", null, 1, true); assert.ok(!/log in/i.test(w.hint) && /not loaded/i.test(w.modeText));
});
test("strip: DEMO and REAL differ in words and tone; armed has its own text; P&L carries sign and arrow", () => {
  const demo = ms.stripView("fno", { mode: "DEMO", armed: false, pnl: 0, at: 0 }, 1000);
  const real = ms.stripView("realtrade", { mode: "REAL", armed: true, pnl: -340, at: 0 }, 1000);
  assert.equal(demo.tone, "demo"); assert.equal(real.tone, "real");
  assert.ok(/REAL/.test(real.modeText) && /DEMO/.test(demo.modeText) && real.modeText !== demo.modeText);
  assert.equal(real.armedText, "Armed"); assert.equal(demo.armedText, "Not armed");
  assert.equal(real.pnlText, "▼ -₹340"); assert.equal(real.pnlTone, "down"); assert.equal(demo.pnlTone, "flat");
  assert.equal(ms.stripView("realtrade", { mode: "REAL", armed: null, pnl: 55, at: 0 }, 1).pnlText, "▲ +₹55");
});
test("strip: unknown values stay unknown (no guessed armed state, no invented zero)", () => {
  const v = ms.stripView("realtrade", { mode: "REAL", armed: null, pnl: null, at: 0 }, 1);
  assert.equal(v.armedText, null); assert.equal(v.pnlText, null); assert.equal(v.pnlTone, null);
  assert.equal(ms.stripView("realtrade", { mode: "REAL", armed: true, pnl: Number.NaN, at: 0 }, 1).pnlText, null);
});
test("strip: data older than 90 s is marked stale", () => {
  const s = { mode: "REAL", armed: true, pnl: 1, at: 0 };
  assert.equal(ms.stripView("realtrade", s, 90_000).stale, false); assert.equal(ms.stripView("realtrade", s, 90_001).stale, true);
});
test("strip: publish / read / clear round trip, and subscribers are told", () => {
  let hits = 0; const off = ms.subscribeModeStrip(() => hits++);
  ms.publishModeStrip("realtrade", { mode: "REAL", armed: true, pnl: 5 }, 42);
  assert.equal(ms.readModeStrip("realtrade").at, 42); ms.publishModeStrip("realtrade", null);
  assert.equal(ms.readModeStrip("realtrade"), null); off(); assert.equal(hits, 2);
});
test("the strip only READS: modeStrip.ts imports nothing, and the ModeStrip component makes no api call", () => {
  const m = rd("./components/app/modeStrip.ts"); assert.ok(!/^import /m.test(m)); assert.ok(!/fetch\(|XMLHttpRequest/.test(m));
  const a = rd("./components/app/AppNav.tsx"); const body = a.slice(a.indexOf("export function ModeStrip"));
  assert.ok(!/api\.|fetch\(|Api\./.test(body));
});
test("the three trading pages publish their own state and clear it when they close", () => {
  for (const [f, key] of [["./components/RealAutoTrade.tsx", "realtrade"], ["./components/PositionStocksTab.tsx", "positionstocks"], ["./components/fno/FnoOverview.tsx", "fno"]]) {
    assert.ok((f.endsWith("PositionStocksTab.tsx") ? psSource() : rd(f)).includes(`publishModeStrip("${key}", {`), f);
  }
  for (const [f, key] of [["./components/RealAutoTrade.tsx", "realtrade"], ["./components/PositionStocksTab.tsx", "positionstocks"], ["./components/fno/FnoTab.tsx", "fno"]]) assert.ok((f.endsWith("PositionStocksTab.tsx") ? psSource() : rd(f)).includes(`publishModeStrip("${key}", null)`), `${f} clears`);
  assert.ok(/mode: "REAL", armed: status \? status\.armed : null/.test(psSource()), "Position Stocks is always REAL money");
});

/* ---------- App wiring ---------- */
test("App uses the shared nav model: no local nav list; bottom bar, Trading sheet, mode strip and Markets tabs are mounted", () => {
  const a = rd("./App.tsx");
  assert.ok(!/const navItems/.test(a) && !a.includes("MOBILE_BOTTOM_TABS"));
  for (const piece of ["<MobileBottomNav ", "<TradingSheet ", "<ModeStrip tab={tab}", "<MarketsTabs ", "isTradingTab(tab)", "isMarketsTab(tab)"]) assert.ok(a.includes(piece), piece);
});
test("the command palette knows the five sections", () => {
  const a = rd("./App.tsx");
  for (const w of nav.NAV_SECTION_WORDS) assert.ok(a.includes(`label: "${w}", group: "Sections"`), w);
});

/* ---------- CSS ---------- */
test("shell.css: loads right after the kit (only home.css, trading.css, control.css and markets.css are later), tokens only, no animation, no !important, no blur or gradient", () => {
  const css = stripCss(rd("./styles/shell.css")); const main = rd("./main.tsx");
  const imports = [...main.matchAll(/import "\.\/([\w/-]+\.css)"/g)].map((m) => m[1]);
  assert.equal(imports[imports.length - 5], "styles/shell.css"); assert.equal(imports[imports.length - 4], "styles/home.css"); assert.equal(imports[imports.length - 3], "styles/trading.css"); assert.equal(imports[imports.length - 2], "styles/control.css"); assert.equal(imports[imports.length - 1], "styles/markets.css");
  assert.ok(!/@keyframes|animation\s*:|transition\s*:/.test(css) && !/!important/.test(css) && !/backdrop-filter|linear-gradient/.test(css));
  assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(css), "no hard-coded hex colours");
});
test("mode strip is sticky below the top bar and REAL has a solid edge on the flat card colour", () => {
  const css = stripCss(rd("./styles/shell.css"));
  assert.ok(/\.sh-modestrip\s*\{[^}]*position:\s*sticky/.test(css));
  assert.ok(/\.sh-modestrip\.real\s*\{[^}]*border-bottom:\s*2px solid rgb\(var\(--warn-rgb\)\)/.test(css));
});
test("the folded System section never hides pages from the tablet rail", () => {
  const css = stripCss(rd("./styles/shell.css"));
  assert.ok(/@media \(min-width: 1024px\)\s*\{\s*\.sidebar-link\.sh-folded\s*\{\s*display:\s*none/.test(css));
});
test("every sh- class used in the shell components is styled", () => {
  const css = rd("./styles/shell.css"); const src = rd("./components/app/AppNav.tsx") + rd("./components/AdminGate.tsx");
  for (const m of new Set([...src.matchAll(/["` ]((?:sh)-[a-z-]+)/g)].map((x) => x[1]))) assert.ok(css.includes(`.${m}`), `.${m} has no style`);
});

console.log(`shell tests: ${n} ok`);
