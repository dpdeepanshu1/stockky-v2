// E540 (redesign round 7): plain-node tests (Node >= 22.6): node --experimental-strip-types src/markets.test.mjs
import { test } from "node:test";
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import * as wl from "./watchlistOrder.ts";
import * as mf from "./marketsFilter.ts";

const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const stripCss = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "");

/* ---------- watchlist order ---------- */
test("sortWatchlist: My order keeps the saved order, gainers / losers rank by %, a missing figure goes last and is never treated as 0", () => {
  const syms = ["AAA", "BBB", "CCC", "DDD", "EEE"];
  const pct = { AAA: 1.5, BBB: null, CCC: -2, DDD: 0, EEE: undefined };
  assert.deepEqual(wl.sortWatchlist(syms, pct, "mine"), syms);
  assert.deepEqual(wl.sortWatchlist(syms, pct, "best"), ["AAA", "DDD", "CCC", "BBB", "EEE"]);
  assert.deepEqual(wl.sortWatchlist(syms, pct, "worst"), ["CCC", "DDD", "AAA", "BBB", "EEE"]);
  assert.deepEqual(wl.sortWatchlist(["X", "Y"], { X: NaN, Y: Infinity }, "best"), ["X", "Y"], "NaN / Infinity are not figures");
});
test("sortWatchlist: equal figures keep their saved order; the input is not changed", () => {
  const syms = ["B", "A", "C"]; const copy = syms.slice();
  assert.deepEqual(wl.sortWatchlist(syms, { A: 1, B: 1, C: 1 }, "best"), ["B", "A", "C"]);
  assert.deepEqual(syms, copy);
  const mine = wl.sortWatchlist(syms, {}, "mine"); mine.push("Z"); assert.deepEqual(syms, copy, "returns a copy");
});
test("moveSymbol: moves one item, clamps the target, bad indexes change nothing", () => {
  assert.deepEqual(wl.moveSymbol(["A", "B", "C", "D"], 0, 2), ["B", "C", "A", "D"]);
  assert.deepEqual(wl.moveSymbol(["A", "B", "C", "D"], 3, 0), ["D", "A", "B", "C"]);
  assert.deepEqual(wl.moveSymbol(["A", "B", "C"], 1, 99), ["A", "C", "B"]);
  assert.deepEqual(wl.moveSymbol(["A", "B", "C"], 1, -5), ["B", "A", "C"]);
  assert.deepEqual(wl.moveSymbol(["A", "B"], 5, 0), ["A", "B"]);
  assert.deepEqual(wl.moveSymbol(["A", "B"], 0.5, 1), ["A", "B"]);
  assert.deepEqual(wl.moveSymbol([], 0, 0), []);
});
test("dropSymbol: works by name, unknown or identical names change nothing, no symbol is lost or doubled", () => {
  const l = ["A", "B", "C", "D"];
  assert.deepEqual(wl.dropSymbol(l, "A", "C"), ["B", "C", "A", "D"]);
  assert.deepEqual(wl.dropSymbol(l, "D", "A"), ["D", "A", "B", "C"]);
  assert.deepEqual(wl.dropSymbol(l, "A", "A"), l);
  assert.deepEqual(wl.dropSymbol(l, "ZZ", "A"), l);
  assert.deepEqual(wl.dropSymbol(l, "A", "ZZ"), l);
  assert.deepEqual(wl.dropSymbol(l, "B", "D").slice().sort(), l.slice().sort());
  assert.ok(wl.sameOrder(["A", "B"], ["A", "B"]) && !wl.sameOrder(["A", "B"], ["B", "A"]) && !wl.sameOrder(["A"], ["A", "B"]));
});

/* ---------- components ---------- */
test("KpiStrip is built on the kit Stat; direction is never colour-only (sub-line delta, or an arrow in front of the value)", () => {
  const t = rd("./components/ui/KpiStrip.tsx");
  assert.ok(/from "\.\.\/kit"/.test(t) && /<Stats>/.test(t) && /<Stat\b/.test(t));
  assert.ok(/delta=\{k\.sub\}/.test(t) && /ARROW\[tone\]/.test(t), "delta line, or arrow fallback");
  assert.ok(!/PriceRoll|GlintBox|useTickGlint|requestAnimationFrame|countUp|fetch\(/.test(t));
  assert.ok(!/v6-kpi/.test(t), "no old tile classes left");
});
test("Watchlist: order changes go through onChange only in My order; the sorted views are display-only; moves also work without a mouse", () => {
  const t = rd("./components/WatchlistManager.tsx");
  assert.ok(/<Tabs label="Watchlist order"/.test(t));
  assert.ok(/const canMove = sort === "mine"/.test(t));
  assert.ok(/draggable=\{canMove\}/.test(t) && /onDrop=\{canMove && dragging/.test(t), "drag is off outside My order");
  assert.ok(/aria-label=\{`Move \$\{s\} earlier`\}/.test(t) && /aria-label=\{`Move \$\{s\} later`\}/.test(t), "arrow buttons");
  assert.ok(/onChange\(dropSymbol\(symbols, dragging, s\)\)|const next = dropSymbol\(symbols, dragging, s\)/.test(t));
  const orderCalls = [...t.matchAll(/onChange\(/g)].length;
  assert.ok(orderCalls >= 4, "add, remove, drop and the two arrows save through onChange");
  assert.ok(!/api\.(save|update|put|post)/i.test(t.replace(/api\.getQuote/g, "")), "no new request: the saved list still goes through the page's onChange");
});
test("Paper Trades tabs use the kit Tabs with the same four views and the same state", () => {
  const t = rd("./components/Trades.tsx");
  assert.ok(/<Tabs\b[\s\S]*label="Paper trades view"/.test(t));
  for (const id of ["open", "closed", "daily", "weekly"]) assert.ok(new RegExp(`id: "${id}"`).test(t), id);
  assert.ok(/onChange=\{setTab\}/.test(t) && /useState<"open" \| "closed" \| "daily" \| "weekly">\("open"\)/.test(t));
  assert.ok(!/onClick=\{\(\) => setTab\(key\)\}/.test(t), "the hand-made tab buttons are gone");
});
test("Alerts: no history feed exists on the backend, so none is drawn (no invented day groups or read marks)", () => {
  const api = rd("./api.ts"), t = rd("./components/NotificationsPanel.tsx");
  assert.ok(!/getNotificationHistory|\/notifications\/(history|feed|inbox)/.test(api));
  assert.ok(!/mark all read|markAllRead/i.test(t));
});

/* ---------- CSS ---------- */
test("markets.css: loads last, tokens only, no animation, no !important, no blur or gradient, no hex colour; every mk- class used is defined", () => {
  const css = stripCss(rd("./styles/markets.css")); const main = rd("./main.tsx");
  const imports = [...main.matchAll(/import "\.\/([\w/-]+\.css)"/g)].map((m) => m[1]);
  assert.equal(imports[imports.length - 1], "styles/markets.css");
  assert.ok(!/@keyframes|animation\s*:|transition\s*:/.test(css) && !/!important/.test(css) && !/backdrop-filter|linear-gradient/.test(css));
  assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(css), "no hard-coded hex colours");
  const used = new Set();
  for (const f of ["components/ui/KpiStrip.tsx", "components/WatchlistManager.tsx", "components/Trades.tsx", "components/MarketsFilterBar.tsx"])
    for (const m of rd(`./${f}`).matchAll(/["'` ](mk-[a-z0-9-]+)/g)) used.add(m[1]);
  assert.ok(used.size >= 9, `found ${used.size} classes`);
  for (const c of used) assert.ok(css.includes(`.${c}`), `.${c} missing from markets.css`);
});
test("the watchlist drag state and the move buttons have a visible focus / drag style", () => {
  const css = stripCss(rd("./styles/markets.css"));
  assert.ok(/\.mk-move button:focus-visible/.test(css) && /data-dragging="true"/.test(css));
});

/* ---------- E541: shared Markets filter ---------- */
const A = { symbol: (r) => r.s, name: (r) => r.n, score: (r) => r.sc, change: (r) => r.ch };
const ROWS = [
  { s: "TCS", n: "Tata Consultancy", sc: 60, ch: -1 }, { s: "INFY", n: "Infosys", sc: null, ch: 2 },
  { s: "RELIANCE", n: "Reliance Industries", sc: 80, ch: NaN }, { s: "HDFCBANK", n: "HDFC Bank", sc: 0, ch: 0 }, { s: "SBIN", n: undefined, sc: 60, ch: "3" },
];
test("applyMarketFilter: default keeps the incoming order; search matches symbol or name, any case, and ignores spaces around it", () => {
  assert.deepEqual(mf.applyMarketFilter(ROWS, A, mf.NO_FILTER).map((r) => r.s), ["TCS", "INFY", "RELIANCE", "HDFCBANK", "SBIN"]);
  assert.deepEqual(mf.applyMarketFilter(ROWS, A, { query: " rel ", sort: "rank" }).map((r) => r.s), ["RELIANCE"]);
  assert.deepEqual(mf.applyMarketFilter(ROWS, A, { query: "bank", sort: "rank" }).map((r) => r.s), ["HDFCBANK"]);
  assert.deepEqual(mf.applyMarketFilter(ROWS, A, { query: "zzz", sort: "rank" }), []);
  assert.deepEqual(mf.applyMarketFilter(null, A, mf.NO_FILTER), []);
  assert.deepEqual(mf.applyMarketFilter(undefined, A, { query: "a", sort: "score" }), []);
});
test("applyMarketFilter: score / change sort high to low; a real 0 ranks as 0, a missing, NaN or text figure goes last in incoming order", () => {
  assert.deepEqual(mf.applyMarketFilter(ROWS, A, { query: "", sort: "score" }).map((r) => r.s), ["RELIANCE", "TCS", "SBIN", "HDFCBANK", "INFY"]);
  assert.deepEqual(mf.applyMarketFilter(ROWS, A, { query: "", sort: "change" }).map((r) => r.s), ["INFY", "HDFCBANK", "TCS", "RELIANCE", "SBIN"]);
  assert.deepEqual(mf.applyMarketFilter(ROWS, A, { query: "", sort: "name" }).map((r) => r.s), ["HDFCBANK", "INFY", "RELIANCE", "SBIN", "TCS"]);
});
test("applyMarketFilter: the input is never changed; a sort the page has no figure for falls back to the incoming order", () => {
  const copy = JSON.stringify(ROWS); mf.applyMarketFilter(ROWS, A, { query: "", sort: "score" }); assert.equal(JSON.stringify(ROWS), copy);
  const only = { symbol: (r) => r.s };
  assert.deepEqual(mf.applyMarketFilter(ROWS, only, { query: "", sort: "score" }).map((r) => r.s), ROWS.map((r) => r.s));
  assert.deepEqual(mf.sortsFor(only), ["rank", "name"]); assert.deepEqual(mf.sortsFor(A), ["rank", "score", "change", "name"]);
  assert.equal(mf.countLine(5, 5), "5 shown"); assert.equal(mf.countLine(2, 5), "2 of 5 shown");
  assert.ok(mf.isFilteredEmpty(0, 3) && !mf.isFilteredEmpty(0, 0) && !mf.isFilteredEmpty(2, 3));
});
test("Hot Picks, Surprise and IPO use the one filter: state is local, the loaded list is never reassigned, the empty text tells filtered from nothing-found", () => {
  const pages = { hot: rd("./components/HotStocks.tsx"), sur: rd("./components/SurpriseStocks.tsx"), ipo: rd("./components/IpoTracker.tsx") };
  for (const [k, t] of Object.entries(pages)) {
    assert.ok(/<MarketsFilterBar\b/.test(t) && /applyMarketFilter\(/.test(t) && /useState<MarketFilter>\(NO_FILTER\)/.test(t), k);
  }
  assert.ok(/shownStocks\.map\(/.test(pages.sur) && !/\{stocks\.map\(/.test(pages.sur), "surprise draws the filtered rows");
  assert.ok(/shownIpo\.map\(/.test(pages.ipo) && !/\{ipoList\.map\(/.test(pages.ipo), "IPO draws the filtered rows");
  assert.ok(/items=\{hotShown\.bulk\}/.test(pages.hot) && /items=\{hotShown\.results\}/.test(pages.hot) && /items=\{hotShown\.news\}/.test(pages.hot));
  assert.ok(/<NoMatch\b/.test(pages.sur) && /<NoMatch\b/.test(pages.ipo) && /matches the search/.test(pages.hot));
  const bar = rd("./components/MarketsFilterBar.tsx");
  assert.ok(/role="search"/.test(bar) && /type="search"/.test(bar) && /<Tabs\b/.test(bar) && !/fetch\(|api\./.test(bar));
});

/* ---------- E542: Settings, Data Feed, Training Lab on the kit ---------- */
test("Settings: sections are kit Cards; the URL box is a labelled kit Field; every handler and confirm step is unchanged", () => {
  const t = rd("./components/app/SettingsPage.tsx");
  assert.ok(/from "\.\.\/kit"/.test(t) && !/terminal-panel/.test(t) && !/btn-terminal/.test(t), "old panel and button classes are gone");
  for (const title of ["Backend connection", "Appearance", "Databases", "Services", "Deploy checklist"]) assert.ok(new RegExp(`<CardHeader title="${title}"`).test(t), title);
  assert.ok(/<Field\b[\s\S]*label="API Gateway URL"/.test(t) && /onKeyDown=\{\(e\) => e\.key === "Enter" && save\(\)\}/.test(t) && /<Button onClick=\{save\}>Save URL<\/Button>/.test(t));
  assert.ok(/onClick=\{\(\) => powerOffAll\(\)\}/.test(t) && /\n  if \(!window\.confirm\(/.test(t) && !/confirmText/.test(t), "Power Off keeps its one confirm (no second, kit confirm on top)");
  assert.ok(/onClick=\{\(\) => void wakeDbNow\(\)\}/.test(t) && /onClick=\{onOpenServices\}/.test(t) && /<RateLimitDashboard \/>/.test(t) && /<OvernightAutomationPanel \/>/.test(t));
  assert.ok(/<StatusPill state=\{backendUp === "up" \? "live" : backendUp === "down" \? "offline" : "info"\}>/.test(t) && /Backend down/.test(t), "backend state in words and a pill, never colour only");
});
test("Data Feed and Training Lab: action buttons are kit Buttons with the same handlers and labels; panels use the kit card", () => {
  const df = rd("./components/DataFeed.tsx"), tr = rd("./components/Training.tsx");
  for (const h of ["onResume", "onStop", "onRefreshPage", "onRefillAdditional", "startFresh", "startNewOnly"]) assert.ok(new RegExp(`<Button[\\s\\S]{0,200}onClick=\\{${h}\\}`).test(df), `DataFeed ${h}`);
  for (const w of ["Resume", "Stop", "Refresh status", "Refill Additional Data", "Add alert", "Check now", "Remove"]) assert.ok(df.includes(w), w);
  for (const h of ["handleStopTraining", "handleRefresh", "handleRestart", "handleTriggerTraining"]) assert.ok(new RegExp(`onClick=\\{${h}\\}`).test(tr), `Training ${h}`);
  assert.ok(/runEvaluation\("t1"\)/.test(tr) && /runEvaluation\("t5"\)/.test(tr) && /Unlock &amp; reset/.test(tr));
  for (const [n, t] of [["DataFeed", df], ["Training", tr]]) {
    assert.ok(/import \{ Button \} from "\.\/kit"/.test(t), n);
    assert.equal((t.match(/<Button\b/g) || []).length, (t.match(/<\/Button>/g) || []).length, `${n}: every Button is closed`);
    assert.ok(/className="k-card/.test(t), `${n}: kit card surface`);
  }
  assert.ok(!/bg-graphite border border-slate\/[46]0 rounded-2xl p-5/.test(tr) && !/rounded-2xl border border-slate\/50 bg-graphite\/80 p-5/.test(df), "old panel class strings are gone");
});
test("Stop on the Data Feed stays one click (it was one click before: stop-and-commit is safe), and no kit confirm was added to any converted button", () => {
  const df = rd("./components/DataFeed.tsx"), tr = rd("./components/Training.tsx");
  assert.ok(!/confirmText/.test(df) && !/confirmText/.test(tr));
});
test("markets.css has the Settings rules the page uses", () => {
  const css = stripCss(rd("./styles/markets.css"));
  for (const c of ["st-row", "st-actions", "st-list"]) assert.ok(css.includes(`.${c}`), c);
});
