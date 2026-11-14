// E540 (redesign round 7): plain-node tests (Node >= 22.6): node --experimental-strip-types src/markets.test.mjs
import { test } from "node:test";
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import * as wl from "./watchlistOrder.ts";
import * as mf from "./marketsFilter.ts";
import * as mc from "./marketCardView.ts";
import { surpriseRowView } from "./components/surpriseView.ts";

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
    assert.ok(/import \{ Button[^}]*\} from "\.\/kit"/.test(t), n);
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

/* ---------- E543: shared card face ---------- */
test("surpriseCard shows the same text as the table row: tier, change with an arrow, score for the ring, RVOL / target / trail, trigger", () => {
  const s = { symbol: "ABC", score: 72.4, tier: "breakout", change_pct: 3.456, rvol: 4.2, rvol_slope: 0.5, target_1: 105, trailing_stop: 98.5, trigger_type: "VOLUME_SHOCK" };
  const c = mc.surpriseCard(s, surpriseRowView(s));
  assert.equal(c.symbol, "ABC"); assert.equal(c.score, 72); assert.equal(c.tag, "VOLUME_SHOCK");
  assert.deepEqual(c.badge, { label: "Breakout", glyph: "▲", tone: "buy" });
  assert.equal(c.change.text, "+3.46%"); assert.equal(mc.changeArrow(c.change.dir), "▲ ");
  assert.deepEqual(c.facts.map((f) => f.label), ["RVOL", "Target +5%", "Trail (VWAP)"]);
  assert.equal(c.facts[0].value, "4.20x ↑0.50"); assert.equal(c.facts[1].value, "₹105.00"); assert.equal(c.facts[2].value, "₹98.50");
});
test("surpriseCard: missing values are dashes and no ring; a real 0 score is a ring at 0; a falling change has a down arrow", () => {
  const e = mc.surpriseCard({ symbol: "X" }, surpriseRowView({}));
  assert.equal(e.score, null); assert.equal(e.change.text, "—"); assert.equal(mc.changeArrow(e.change.dir), ""); assert.equal(e.tag, null);
  assert.ok(e.facts.every((f) => f.value === "—") && !JSON.stringify(e).includes("NaN"));
  assert.equal(mc.surpriseCard({ symbol: "Z", score: 0 }, surpriseRowView({ score: 0 })).score, 0);
  const d = mc.surpriseCard({ symbol: "D", score: 250, trigger_type: "  " }, surpriseRowView({ score: 250, change_pct: -1.2, tier: "building" }));
  assert.equal(d.score, 100); assert.equal(d.tag, null); assert.equal(mc.changeArrow(d.change.dir), "▼ "); assert.equal(d.badge.label, "Building"); assert.equal(d.badge.tone, "warn");
  assert.equal(mc.surpriseCard({ symbol: 5 }, surpriseRowView({})).symbol, "—");
});
test("Surprise: Table stays the default; Cards reads the same filtered rows and the same price node; the table's own markup is untouched", () => {
  const t = rd("./components/SurpriseStocks.tsx");
  assert.ok(/useState<SurpriseView>\("table"\)/.test(t) && /<Tabs label="Surprise Momentum view"/.test(t));
  assert.ok(/view === "cards"/.test(t) && /view === "table"/.test(t));
  assert.ok(/shownStocks\.map\(\(s\) => \(\s*<MarketCard/.test(t) && /surpriseCard\(s, surpriseRowView\(s\)\)/.test(t));
  assert.ok(/PriceRoll value=\{resolveDisplayPrice\(s as any, null\)\}/.test(t));
  for (const col of ["Symbol", "Tier", "Score", "Price", "Change %", "RVOL", "Trigger", "Target +5%", "Trail (VWAP)"]) assert.ok(t.includes(`>${col}</th>`), `table column ${col}`);
  assert.ok(/onClick=\{\(\) => onSelect\?\.\(s\.symbol\)\}/.test(t), "table row click is unchanged");
});
test("MarketCard: symbol opens the stock, direction in text, ring only with a score, no request, no money animation; css rules exist and use no gradient / hex", () => {
  const t = rd("./components/MarketCard.tsx"), css = stripCss(rd("./styles/markets.css"));
  assert.ok(/onClick=\{\(\) => onSelect\?\.\(card\.symbol\)\}/.test(t) && /changeArrow\(card\.change\.dir\)/.test(t) && /card\.score != null && <ScoreRing/.test(t));
  assert.ok(!/fetch\(|api\.|PriceRoll|GlintBox|requestAnimationFrame/.test(t));
  for (const c of ["mk-grid", "mk-card", "mk-card-sym", "mk-badge", "mk-card-price", "mk-card-chg", "mk-card-facts", "mk-card-tag"]) assert.ok(css.includes(`.${c}`), c);
  assert.ok(/\.mk-card-sym:focus-visible/.test(css));
});

/* ---------- E544: tables, progress bar and notices on the kit ---------- */
test("Training Lab: the three tables are kit DataTables with the same headings and the same cell helpers; loaders are skeletons", () => {
  const t = rd("./components/Training.tsx");
  assert.equal((t.match(/<DataTable\b/g) || []).length, 3, "rollup, prediction history, fold details");
  assert.ok(!/<table\b/.test(t) && !/<thead>/.test(t), "no hand-made table left");
  for (const h of ["Picks", "Buy Now", "Prepare", "T+1 Success", "T+1 Avg", "T+5 Success", "T+5 Avg", "Pending", "Symbol", "Decision", "Price", "Outcome", "Fold", "Train Start", "Train End", "Val Start", "Val End", "Train Samples", "Val Samples"])
    assert.ok(t.includes(`header: "${h}"`), `column ${h}`);
  assert.ok(/header: periodView === "daily" \? "Date" : "Week"/.test(t));
  assert.ok(/predictionRowView\(p\)/.test(t) && /fmtMetric\(key, metrics\[key\]\)/.test(t), "cells still read the helpers");
  assert.ok(/rowKey=\{\(row\) => row\.period\}/.test(t) && /rowKey=\{\(p\) => String\(p\.prediction_id\)\}/.test(t) && /rowKey=\{\(fold\) => String\(fold\.fold\)\}/.test(t), "stable row keys");
  assert.equal((t.match(/<SkeletonLines rows=\{4\} \/>/g) || []).length, 3, "rollup, history and insights loaders");
  assert.ok(/rate == null \? "text-mist\/40" : rate >= 50/.test(t) && /\$\{rate\}%/.test(t), "a success rate keeps its % text, a missing one is a dash");
});
test("Data Health Audit: kit DataTable with loading, empty and all-clear states in words; Repair is one kit Button with the same handler", () => {
  const t = rd("./components/DataHealthAudit.tsx");
  assert.ok(/<DataTable\b/.test(t) && !/<table\b/.test(t));
  for (const h of ["Symbol", "Price", "Missing", "Action"]) assert.ok(t.includes(`header: "${h}"`), h);
  assert.ok(/SkeletonLines rows=\{4\}/.test(t) && /No feed records yet/.test(t) && /Run Data Feed first, then Refresh Audit/.test(t) && /look complete for required fields/.test(t));
  assert.ok(/onClick=\{\(\) => void handleRepairSingle\(item\.symbol\)\}/.test(t) && /repairingSym === item\.symbol \|\| batchBusy/.test(t) && /Patching…/.test(t));
  assert.ok(/0 \(missing\)/.test(t), "a zero price is still called missing");
});
test("Data Feed: progress is a real progressbar (value and label), the banners are a notice and the kit error state; no width transition is left", () => {
  const t = rd("./components/DataFeed.tsx"), pb = rd("./components/ProgressBar.tsx");
  assert.ok(/<ProgressBar pct=\{pct\} label="Data feed progress" \/>/.test(t) && !/transition-all duration-500/.test(t));
  assert.ok(/role="progressbar"/.test(pb) && /aria-valuenow=\{v\}/.test(pb) && /Number\.isFinite\(pct\)/.test(pb) && !/transition|animation/.test(pb));
  assert.ok(/<p className="mk-note ok" role="status">✅ \{banner\}<\/p>/.test(t) && /<ErrorState title=\{err\} \/>/.test(t));
  assert.ok(!/bg-signal-buy\/10 px-3 py-2/.test(t) && !/bg-signal-sell\/10 px-3 py-2/.test(t), "old banner boxes are gone");
});
test("markets.css has the progress bar and notice rules, still no animation, hex or !important", () => {
  const css = stripCss(rd("./styles/markets.css"));
  for (const c of ["mk-progress", "mk-note"]) assert.ok(css.includes(`.${c}`), c);
  assert.ok(!/@keyframes|animation\s*:|transition\s*:/.test(css) && !/!important/.test(css) && !/#[0-9a-fA-F]{3,8}\b/.test(css));
});

/* ---------- E545: Feed Health, Rate Limit and Training Progress panels on the kit ---------- */
test("Feed Health panel: kit card, kit Stats (a missing score is a dash, not '—%'; the score has a word), kit DataTable, kit Buttons with the same handlers and disabled rules", () => {
  const t = rd("./components/FeedHealthPanel.tsx");
  assert.ok(/className="k-card mt-2 mb-6"/.test(t) && !/<table\b/.test(t) && !/<button\b/.test(t));
  assert.ok(/<Stats>/.test(t) && !/\$\{healthData\?\.health_score \?\? "—"\}%/.test(t), "no '—%'");
  for (const w of ["Healthy", "Needs attention", "Poor", "not reported"]) assert.ok(t.includes(w), w);
  for (const l of ["Health score", "Total tracked", "Fully populated", "Missing data"]) assert.ok(t.includes(`label="${l}"`), l);
  assert.ok(/onClick=\{\(\) => void onRefreshAudit\(\)\}/.test(t) && /onClick=\{\(\) => void onRepairBatch\(\)\}/.test(t) && /onClick=\{\(\) => void onRepairSingle\(stock\.symbol\)\}/.test(t));
  assert.ok(/batchRepairBusy \|\|\s*healthLoading \|\|/.test(t) && /incomplete_stocks\?\.length \?\? healthData\?\.missing_data \?\? 0\) === 0/.test(t), "the repair button is still gated the same way");
  assert.ok(/patchingSymbol === stock\.symbol \|\| batchRepairBusy/.test(t) && /Retry Audit/.test(t) && /Refresh Audit/.test(t));
  for (const h of ["Symbol", "Missing", "Action"]) assert.ok(t.includes(`header: "${h}"`), h);
  assert.ok(/\(stock\.missing_fields \|\| \["price"\]\)/.test(t), "the default missing field is still price");
});
test("Rate Limit dashboard: levels and circuit states are status pills with their words; events are a kit DataTable; error is the kit error state with Try again", () => {
  const t = rd("./components/RateLimitDashboard.tsx");
  assert.ok(!/levelClass|terminal-panel|<table\b|<button\b|btn-terminal/.test(t));
  assert.ok(/critical"\) return "offline"/.test(t) && /"warn" \|\| level === "degraded"\) return "paused"/.test(t) && /"watch"\) return "info"/.test(t));
  assert.ok(/<StatusPill state=\{levelState\(data\.overall\)\}>\{data\.overall\}<\/StatusPill>/.test(t) && /<StatusPill state=\{levelState\(u\.level\)\}>\{u\.level\}<\/StatusPill>/.test(t));
  assert.ok(/c\.state === "open" \? "offline" : c\.state === "half_open" \? "paused" : "live"/.test(t));
  for (const h of ["IST", "Src", "Code", "Symbol", "Path / detail"]) assert.ok(t.includes(`header: "${h}"`), h);
  assert.ok(/No rate-limit events recorded yet\./.test(t) && /<ErrorState title=\{error\} onRetry=\{load\} \/>/.test(t));
  assert.ok(/setVisibleInterval\(load, 30000\)/.test(t) && /api\.getRateLimits\(\)/.test(t), "same poll, same call");
});
test("Training progress: kit card and the shared progress bar; each stage has a glyph and a word; the looping pulse and the width transition are gone; stage mapping is unchanged", () => {
  const t = rd("./components/TrainingProgressPanel.tsx");
  assert.ok(/className="k-card mb-4"/.test(t) && /<ProgressBar pct=\{progressPct\} label="Training progress" \/>/.test(t));
  assert.ok(!/animate-pulse|transition-all/.test(t));
  assert.ok(/state === "done" \? "✓" : state === "active" \? "▸" : "○"/.test(t) && /"in progress"/.test(t) && /"waiting"/.test(t));
  for (const k of ["loading_data", "building_features", "walk_forward", "calibrating", "saving_model", "evaluating_t1", "evaluating_t5"]) assert.ok(t.includes(`key: "${k}"`), k);
  assert.ok(/done \? "Complete" : failed \? "Stopped"/.test(t) && /Math\.min\(99, Math\.round\(/.test(t), "labels and the 99 % cap are unchanged");
});
test("markets.css has the training stage rules", () => {
  const css = stripCss(rd("./styles/markets.css"));
  for (const c of ["mk-stage-dot", "mk-stage-word"]) assert.ok(css.includes(`.${c}`), c);
  assert.ok(!/@keyframes|animation\s*:|transition\s*:/.test(css) && !/!important/.test(css) && !/#[0-9a-fA-F]{3,8}\b/.test(css));
});
test("round 8e (E550): ConvictionCard is a kit Card with mk-cc- rules, HotStocks uses mk-hot-; every class they use is defined; no old class is left", () => {
  const css = stripCss(rd("./styles/markets.css"));
  const cc = rd("./components/ConvictionCard.tsx"), hs = rd("./components/HotStocks.tsx");
  assert.ok(/import \{ Card \} from "\.\/kit\/Card"/.test(cc) && /<Card\b/.test(cc) && !/<article/.test(cc));
  assert.ok(!/(?<![\w-])(cc-|qg-|pillar-)[a-z0-9-]+/.test(cc), "an old cc- / qg- / pillar- class is back");
  assert.ok(!/(?<![\w-])hot-(section|grid|empty|error|meta)/.test(hs), "an old hot- class is back");
  assert.ok(/onClick=\{\(\) => onSelect\?\.\(data\.symbol\)\}/.test(cc) && /role=\{onSelect \? "button" : undefined\}/.test(cc) && /e\.key === "Enter" \|\| e\.key === " "/.test(cc), "click and keyboard select are kept");
  const used = new Set();
  for (const t of [cc, hs]) for (const m of t.matchAll(/["'` ](mk-(?:cc|hot)-[a-z0-9-]+|mk-cc)(?![\w-])/g)) used.add(m[1]);
  assert.ok(used.size >= 20, `found ${used.size}`);
  for (const c of used) assert.ok(css.includes(`.${c}`), `.${c} missing from markets.css`);
});
test("round 8e (E550): the IPO overview is a kit Card, the row faces use mk- names, and the old v5-ipo / v7 row rules are gone from the old layers", () => {
  const css = stripCss(rd("./styles/markets.css"));
  const ip = rd("./components/IpoLifecycle.tsx");
  assert.ok(/import \{ Card \} from "\.\/kit\/Card"/.test(ip) && /<Card\b/.test(ip) && !/<section/.test(ip));
  const old = ["styles/base.css", "styles/legacy.css"].map((f) => rd(`./${f}`)).join("\n");
  assert.ok(!/\.(v5-ipo|v7-sc|v7-tier|v7-arrow|v7-srow|v7-ipo-row|cc-card|hot-grid)\b/.test(old), "an old rule is back");
  const used = new Set();
  for (const f of ["IpoLifecycle", "IpoTracker", "SurpriseStocks", "Trades", "WatchlistManager"]) for (const m of rd(`./components/${f}.tsx`).matchAll(/["'` ](mk-(?:ipo|sc|tier|arrow|srow)[a-z0-9-]*)(?![\w-])/g)) used.add(m[1]);
  assert.ok(used.size >= 14, `found ${used.size}`);
  for (const c of used) assert.ok(css.includes(`.${c}`), `.${c} missing from markets.css`);
  assert.ok(!/@keyframes|animation\s*:/.test(css.slice(css.indexOf("row faces"))), "row faces and the overview add no animation");
});
