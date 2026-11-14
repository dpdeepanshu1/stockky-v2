// E536 (redesign round 4): guards for Home and the stock decision card. Run: node --experimental-strip-types src/home.test.mjs
import { readFileSync, existsSync } from "node:fs";
import assert from "node:assert/strict";
import * as plan from "./components/decision/planView.ts";
import * as home from "./components/app/homeView.ts";

const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const stripCss = (t) => t.replace(/\/\*[\s\S]*?\*\//g, "");
const code = (t) => t.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");   // comments may say "close" or "arm"; only code counts
let n = 0; const test = (name, f) => { f(); n++; console.log("ok -", name); };

/* ---------- decision plan ---------- */
test("trade plan: entry is the middle of the range, risk / reward in percent of entry, ratio to one decimal", () => {
  const p = plan.tradePlan({ entryLow: 98, entryHigh: 102, stop: 95, target: 110, price: 100 });
  assert.equal(p.entry, 100); assert.equal(p.stop, 95); assert.equal(p.target, 110);
  assert.equal(p.riskPct, 5); assert.equal(p.rewardPct, 10); assert.equal(p.rr, 2); assert.equal(p.rrText, "1 : 2.0");
});
test("trade plan: no range -> the live price is the entry", () => {
  const p = plan.tradePlan({ entryLow: null, entryHigh: null, stop: 90, target: 120, price: 100 });
  assert.equal(p.entry, 100); assert.equal(p.rrText, "1 : 2.0");
});
test("trade plan: numbers in the wrong order, missing or not finite give NO plan (never a ratio from nonsense)", () => {
  assert.equal(plan.tradePlan({ entryLow: 98, entryHigh: 102, stop: 105, target: 110, price: 100 }), null, "stop above entry");
  assert.equal(plan.tradePlan({ entryLow: 98, entryHigh: 102, stop: 95, target: 99, price: 100 }), null, "target below entry");
  assert.equal(plan.tradePlan({ entryLow: 98, entryHigh: 102, stop: null, target: 110, price: 100 }), null);
  assert.equal(plan.tradePlan({ entryLow: 98, entryHigh: 102, stop: 95, target: Infinity, price: 100 }), null);
  assert.equal(plan.tradePlan({ entryLow: null, entryHigh: null, stop: 95, target: 110, price: null }), null, "no entry and no price");
  assert.equal(plan.tradePlan({ entryLow: 102, entryHigh: 98, stop: 95, target: 110, price: null }), null, "inverted range with no price");
  assert.equal(plan.tradePlan({ entryLow: 0, entryHigh: 0, stop: 95, target: 110, price: 100 }).entry, 100, "a zero range falls back to the price");
});
test("signal words: BUY, PREPARE, HOLD, AVOID (+ WAIT, SELL), each with a glyph so it is never colour-only", () => {
  const w = (d) => plan.signalWord(d);
  assert.deepEqual([w("BUY NOW").word, w("PREPARE TO BUY").word, w("HOLD").word, w("DO NOT BUY").word, w("WAIT").word, w("SELL").word], ["BUY", "PREPARE", "HOLD", "AVOID", "WAIT", "SELL"]);
  for (const d of ["BUY NOW", "PREPARE TO BUY", "HOLD", "DO NOT BUY", "WAIT", "SELL", "SOMETHING NEW"]) assert.ok(w(d).glyph.length === 1);
  assert.equal(w("SOMETHING NEW").word, "AVOID", "an unknown decision is shown as AVOID, never as a buy");
});
test("only BUY NOW and PREPARE TO BUY are long ideas", () => {
  assert.ok(plan.isLongIdea("BUY NOW") && plan.isLongIdea("PREPARE TO BUY"));
  for (const d of ["HOLD", "WAIT", "SELL", "DO NOT BUY", ""]) assert.ok(!plan.isLongIdea(d));
});
test("rupees and signed percent: Indian grouping, sign in the text, dash for a missing number", () => {
  assert.equal(plan.rupees(1234567.5), "₹12,34,567.5"); assert.equal(plan.rupees(null), "—"); assert.equal(plan.rupees(NaN), "—");
  assert.equal(plan.signedPct(3.24), "+3.2%"); assert.equal(plan.signedPct(-1), "-1.0%"); assert.equal(plan.signedPct(0), "0.0%"); assert.equal(plan.signedPct(undefined), "—");
});

/* ---------- home helpers ---------- */
test("signed rupees: sign in the text, no minus on a zero", () => {
  assert.equal(home.signedRupees(1250), "+₹1,250"); assert.equal(home.signedRupees(-340), "-₹340"); assert.equal(home.signedRupees(0), "₹0"); assert.equal(home.signedRupees(-0.4), "₹0");
});
test("index move: signed numbers, and a missing number is a dash (a half-loaded feed never looks like a flat day)", () => {
  assert.equal(home.indexDelta(12.3, 0.45), "+12.30 (+0.45%)"); assert.equal(home.indexDelta(-5, -0.02), "-5.00 (-0.02%)");
  assert.equal(home.indexDelta(undefined, 0.1), "—"); assert.equal(home.indexDelta(1, NaN), "—"); assert.equal(home.indexValue(null), "—");
});
test("market mood: word, tone and score text", () => {
  assert.deepEqual(home.moodView("BULLISH", 71.4), { word: "Bullish", tone: "up", scoreText: "71/100" });
  assert.equal(home.moodView("BEARISH", 20).tone, "down"); assert.equal(home.moodView("NEUTRAL", 50).tone, "flat"); assert.equal(home.moodView(null, null).scoreText, "—");
});
test("positions: Real Trade rows need shares still open; Position Stocks rows need status OPEN; a short is labelled SHORT", () => {
  const rt = home.fromRealTrade([{ symbol: "A", qty_open: 5, unrealized_pnl: 100, pnl_pct: 1 }, { symbol: "B", qty_open: 0, unrealized_pnl: 5, pnl_pct: 0 }, null]);
  assert.deepEqual(rt.map((r) => r.symbol), ["A"]); assert.equal(rt[0].source, "Auto Trade"); assert.equal(rt[0].side, "LONG");
  const ps = home.fromScalp([{ symbol: "C", status: "OPEN", quantity: 3, side: "short", unrealized_pnl: -50, unrealized_pnl_pct: -0.5 }, { symbol: "D", status: "TARGET_HIT", quantity: 3, unrealized_pnl: null, unrealized_pnl_pct: null }, { symbol: "E", status: "OPEN", quantity: 1, unrealized_pnl: null, unrealized_pnl_pct: null }]);
  assert.deepEqual(ps.map((r) => r.symbol), ["C", "E"]); assert.equal(ps[0].side, "SHORT"); assert.equal(ps[1].side, "LONG"); assert.equal(ps[1].pnl, null);
  assert.deepEqual(home.fromRealTrade(null), []); assert.deepEqual(home.fromScalp(undefined), []);
});
test("positions summary: total over known P&L, unknown rows counted (not hidden), sign and arrow in the text", () => {
  const s = home.summarizePositions([{ pnl: 300 }, { pnl: -100 }, { pnl: null }]);
  assert.equal(s.count, 3); assert.equal(s.pnl, 200); assert.equal(s.missing, 1); assert.equal(s.tone, "up"); assert.equal(s.pnlText, "▲ +₹200");
  const none = home.summarizePositions([{ pnl: null }]); assert.equal(none.pnl, null); assert.equal(none.pnlText, "—"); assert.equal(none.tone, "flat");
  assert.equal(home.summarizePositions([{ pnl: -5 }]).pnlText, "▼ -₹5"); assert.equal(home.summarizePositions([]).count, 0);
});
test("positions order: biggest absolute P&L first, unknown last", () => {
  const rows = home.sortPositions([{ symbol: "A", pnl: 10 }, { symbol: "B", pnl: -500 }, { symbol: "C", pnl: null }, { symbol: "D", pnl: 200 }]);
  assert.deepEqual(rows.map((r) => r.symbol), ["B", "D", "A", "C"]);
});

/* ---------- source guards ---------- */
test("DecisionCard was split: helper files exist, the card is far smaller, and it still exports HorizonStrip", () => {
  for (const f of ["newsScore.ts", "EventParts.tsx", "Bits.tsx", "DecisionTop.tsx", "planView.ts"]) assert.ok(existsSync(new URL(`./components/decision/${f}`, import.meta.url)), f);
  const dc = rd("./components/DecisionCard.tsx");
  assert.ok(dc.split("\n").length < 700, "DecisionCard.tsx should be well under its old 1,546 lines");
  assert.ok(/export \{ HorizonStrip \} from "\.\/decision\/Bits"/.test(dc));
  assert.ok(/export function HorizonStrip/.test(rd("./components/decision/Bits.tsx")));
  assert.ok(!dc.includes("dc-hero"), "the old hero block is gone from the card");
});
test("DecisionCard keeps every handler: Trade This, Call Alert (same body), watchlist; the top only receives them as props", () => {
  const dc = rd("./components/DecisionCard.tsx"); const top = rd("./components/decision/DecisionTop.tsx");
  assert.ok(dc.includes("api.commitActionablePicks(") && dc.includes("api.testCallMeBot(msg)") && dc.includes("await onAddToWatchlist(data.symbol)"));
  assert.ok(!/api\./.test(top) && !/fetch\(/.test(top), "DecisionTop makes no call");
  for (const p of ["onTrade", "onCallAlert", "onWatchlist"]) assert.ok(top.includes(p), p);
});
test("detail tabs: four, one at a time, a new stock opens on Overview, and the chart / event panels mount only when their tab is open", () => {
  const dc = rd("./components/DecisionCard.tsx");
  for (const id of ["overview", "chart", "reasons", "news"]) assert.ok(dc.includes(`{ id: "${id}"`) && dc.includes(`detailTab === "${id}"`), id);
  assert.ok(/setDetailTab\("overview"\)[\s\S]*\[data\.symbol\]/.test(dc));
  const at = (s) => dc.indexOf(s);
  const c0 = at('{detailTab === "chart"'), r0 = at('{detailTab === "reasons"'), n0 = at('{detailTab === "news"');
  assert.ok(c0 < at("<StockChart") && at("<StockChart") < r0, "chart inside the chart tab");
  assert.ok(n0 < at("<EventSection"), "events inside the news tab");
  assert.ok(at("<Tabs") > at("</BottomSheet>"), "tabs come after the trade sheet, under the top");
});
test("DecisionCard keeps the structure policy lines above the top (they are safety text)", () => {
  const dc = rd("./components/DecisionCard.tsx");
  assert.ok(dc.indexOf('data-testid="decision-structure"') < dc.indexOf("<DecisionTop"));
});
test("DecisionTop: signal word with glyph, four stats, plan bar only for a complete long plan, honest note otherwise", () => {
  const top = rd("./components/decision/DecisionTop.tsx");
  for (const l of ['label="Entry"', 'label="Stop"', 'label="Target"', 'label="Reward : risk"']) assert.ok(top.includes(l), l);
  assert.ok(top.includes("{sig.glyph}") && top.includes("signalWord("));
  assert.ok(top.includes("long && plan") && top.includes("long && !plan") && top.includes("No complete trade plan"));
  assert.ok(top.includes("loading={calling}") && top.includes("loading={isAddingWatchlist}"), "buttons show busy state from the card");
});
test("HomePositions is read-only: GET positions only, only with an admin session, no button, no write call", () => {
  const h = code(rd("./components/app/HomePositions.tsx"));
  assert.ok(/realTradeApi\.positions\("REAL"\)/.test(h) && /positionStocksApi\.positions\(\)/.test(h));
  assert.ok(/if \(!rt && !ps\)/.test(h) && /if \(\(!rt && !ps\) \|\| rows === null\) return null/.test(h), "idle and silent while logged out");
  assert.ok(!/<button|onClick|method:|POST|close|kill|commit|arm\(|disarm/.test(h), "no control, no write");
  assert.ok(/setVisibleInterval\(load, POLL_MS\)/.test(h), "pauses with the hidden tab");
});
test("market strip: real fields only (Nifty, Sensex, mood), no invented Bank Nifty / VIX / breadth, direction never colour-only", () => {
  const m = rd("./components/MarketSentimentHeader.tsx");
  assert.ok(m.includes('label="NIFTY 50"') && m.includes('label="SENSEX"') && m.includes('label="Market mood"'));
  assert.ok(!/bank\s*nifty|vix|breadth/i.test(code(m)), "no tile for data the API does not send");
  assert.ok(/api\.marketIndices\(/.test(m) && /setVisibleInterval\(\(\) => fetchData\(false\), 60000\)/.test(m), "same call, same 60 s poll");
  assert.ok(rd("./components/kit/Stat.tsx").includes("▲ ") && rd("./components/kit/Stat.tsx").includes("▼ "));
});
test("Home order: search, market strip, positions, then insights + movers; a result opens straight under the search", () => {
  const d = rd("./components/app/DashboardView.tsx"); const at = (s) => d.indexOf(s);
  assert.ok(at('aria-label="Symbol search"') < at("<MarketSentimentHeader") && at("<MarketSentimentHeader") < at("<HomePositions") && at("<HomePositions") < at("<MarketMovers"));
  assert.ok(at("{active && deck}") < at('view.mode === "loading"') && at('view.mode === "scan"') < at("<MarketSentimentHeader"), "results sit above the market sections while active");
  assert.ok(d.includes("{!active && deck}"), "the insights deck sits beside the movers only when idle");
});
test("scan menu: both scans stay reachable, closes on Esc and outside click, menu roles", () => {
  const s = rd("./components/app/ScanMenu.tsx");
  assert.ok(s.includes('role="menu"') && s.includes('role="menuitem"') && s.includes('"Escape"') && s.includes("mousedown") && s.includes("aria-expanded"));
});
test("every Home / decision class used in the sources is defined in home.css", () => {
  const css = stripCss(rd("./styles/home.css"));
  const files = ["./components/app/DashboardView.tsx", "./components/app/HomePositions.tsx", "./components/app/ScanMenu.tsx", "./components/MarketSentimentHeader.tsx", "./components/MarketMovers.tsx", "./components/decision/DecisionTop.tsx"];
  const used = new Set();
  for (const f of files) for (const m of rd(f).matchAll(/["'` ]((?:hm|dt)-[a-z0-9-]+)/g)) used.add(m[1]);
  assert.ok(used.size > 20, "found the classes");
  for (const c of used) assert.ok(css.includes(`.${c}`), `.${c} missing from home.css`);
});
test("home.css: loads right before trading.css, control.css and markets.css, tokens only, no animation, no !important, no blur or gradient, no hex colour, 16px search input", () => {
  const css = stripCss(rd("./styles/home.css")); const main = rd("./main.tsx");
  const imports = [...main.matchAll(/import "\.\/([\w/-]+\.css)"/g)].map((m) => m[1]);
  assert.equal(imports[imports.length - 4], "styles/home.css");   // trading.css (E537), control.css (E539) and markets.css (E540) are the only later files
  assert.ok(!/@keyframes|animation\s*:|transition\s*:/.test(css) && !/!important/.test(css) && !/backdrop-filter|linear-gradient/.test(css));
  assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(css), "no hard-coded hex colours");
  assert.match(css, /\.hm-search input \{[^}]*font-size: 1rem/);
});
test("Home touches no trading control: no arm / close / kill call in the Home files", () => {
  for (const f of ["./components/app/DashboardView.tsx", "./components/app/HomePositions.tsx", "./components/app/ScanMenu.tsx", "./components/decision/DecisionTop.tsx"]) {
    assert.ok(!/\.(arm|disarm|kill|closePosition|closeAll|commit\w*)\(/.test(code(rd(f))), `${f} calls a control`);
  }
});
console.log(`${n} home tests passed`);
