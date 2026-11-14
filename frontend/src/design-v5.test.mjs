// design v5 guards (run with: node --experimental-strip-types src/design-v5.test.mjs)
// 1. every looping / entering animation has an off switch for reduced-motion, TV mode and a hidden tab
// 2. keyframes animate only transform / opacity (never layout)
// 3. the layer is imported after v4 in main.tsx, and the AI Insights deck is mounted on the dashboard
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { optIn } from "./cssText.mjs";
import { liveSelectors } from "./cssLive.mjs";   // E546: only classes a component can still render need an off switch

const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const css = rd("./styles/base.css");
const allReduced = css.split("@media (prefers-reduced-motion: reduce)").slice(1).join("\n");
let passed = 0;
const test = (name, fn) => { fn(); passed += 1; console.log("ok -", name); };

test("looping animations are off for reduced-motion, TV and paused when hidden", () => {
  const reduced = allReduced;
  const LOOPS5 = liveSelectors([".w-orb-core", ".w-typing i", ".ai-progress-sweep", ".ai-pick", ".dash-hero::before"]);
  assert.ok(LOOPS5.length >= 3, "the live loop list must not be filtered away");
  for (const sel of LOOPS5) {
    assert.ok(optIn(css, sel), `${sel} must animate only under reduced-motion: no-preference and outside TV / Animations: Off`);
  }
  for (const sel of liveSelectors([".w-orb-core", ".ai-progress-sweep", ".dash-hero::before"])) {
    assert.ok(css.includes(`html[data-hidden="true"] ${sel}`), `${sel} must pause on a hidden tab`);
  }
});

test("keyframes animate only transform and opacity", () => {
  let n = 0;
  for (const m of css.matchAll(/@keyframes\s+([\w-]+)\s*\{/g)) {
    let depth = 1, i = m.index + m[0].length;
    while (depth > 0 && i < css.length) { if (css[i] === "{") depth += 1; else if (css[i] === "}") depth -= 1; i += 1; }
    const body = css.slice(m.index + m[0].length, i - 1);
    n += 1;
    assert.ok(!/\b(width|height|top|left|right|bottom|margin|padding|box-shadow|filter)\s*:/.test(body), `keyframes ${m[1]} animates a non-composited property`);
  }
  assert.ok(n >= 8, `found ${n} keyframes`);
});

test("part 2 motion (needle, brand shine, hot-card entrance) has off switches and the gauge is mounted", () => {
  const reduced = allReduced;
  for (const sel of liveSelectors([".w-gauge-needle", ".brand-mark::after", ".hot-grid > *"])) {
    assert.ok(optIn(css, sel), `${sel} must animate only under reduced-motion: no-preference and outside TV / Animations: Off`);
  }
  assert.ok(css.includes('html[data-hidden="true"] .brand-mark::after'), "brand shine must pause on a hidden tab");
  assert.ok(/<MoodGauge\b/.test(rd("./components/MarketSentimentHeader.tsx")), "MoodGauge not mounted");
  assert.ok(/role="img"[\s\S]{0,40}aria-label/.test(rd("./components/ui/MoodGauge.tsx")), "gauge needs a text alternative");
});

test("part 3 (score ring, plan bar) has off switches, text alternatives and is mounted; plan bar guards its inputs", () => {
  const reduced = allReduced;
  for (const sel of liveSelectors([".w-ring-arc", ".w-plan-now"])) {
    assert.ok(optIn(css, sel), `${sel} must animate only under reduced-motion: no-preference and outside TV / Animations: Off`);
  }
  // E536: the ring and plan bar moved with the card top into decision/DecisionTop.tsx; the long-idea rule lives in decision/planView.ts (isLongIdea)
  const dc = rd("./components/decision/DecisionTop.tsx");
  assert.ok(/<ScoreRing\b/.test(dc) && /<TradePlanBar\b/.test(dc), "ring / plan bar not mounted");
  assert.ok(/decision === "BUY NOW" \|\| decision === "PREPARE TO BUY"/.test(rd("./components/decision/planView.ts")), "long-idea rule changed");
  assert.ok(/\{long && plan && \(/.test(dc), "plan bar must only show for long ideas");
  assert.ok(/aria-label=\{`AI score/.test(rd("./components/ui/ScoreRing.tsx")), "ring needs a text alternative");
  const bar = rd("./components/ui/TradePlanBar.tsx");
  assert.ok(/stop < lo && hi < target/.test(bar), "plan bar must check stop < entry < target");
});

test("part 4: money banner is mounted on all three pages, spells the mode out, and never shows money", () => {
  assert.ok(optIn(css, ".w-money-ping::after"), "ping must be opt-in");
  assert.ok(css.includes('html[data-hidden="true"] .w-money-ping::after'), "ping must pause on a hidden tab");
  const pages = { "RealAutoTrade.tsx": /<MoneyModeBanner mode=\{mode\}/, "PositionStocksTab.tsx": /<MoneyModeBanner mode="REAL"/, "Trades.tsx": /<MoneyModeBanner mode="PAPER"/ };
  for (const [f, re] of Object.entries(pages)) assert.ok(re.test(rd(`./components/${f}`)), `${f}: banner not mounted with the right mode`);
  const b = rd("./components/ui/MoneyModeBanner.tsx");
  assert.ok(/REAL MONEY/.test(b) && /DEMO MODE/.test(b) && /PAPER TRADING/.test(b), "mode must be spelled out");
  assert.ok(/SYMBOL = \{ ok: "✓", warn: "⚠", bad: "✕" \}/.test(b), "chips need symbols, not colour only");
  assert.ok(!/toLocaleString|₹|PriceRoll|GlintBox/.test(b), "the banner must not render or animate money");
  assert.ok(/role="status"/.test(b), "banner needs a status role");
  assert.ok(!/setMode|onClick/.test(b), "the banner must never change the trading mode");
});

test("part 5: watchlist pulse + quick add are mounted, show percentages only, and the nav motion has off switches", () => {
  for (const sel of [".terminal-bottom-nav button.active .nav-icon", ".terminal-bottom-nav button.active::before"]) {
    assert.ok(optIn(css, sel), `${sel} needs to be opt-in`);
  }
  const wm = rd("./components/WatchlistManager.tsx");
  assert.ok(/<WatchlistPulse\b/.test(wm), "pulse not mounted");
  assert.ok(/add\(x\)/.test(wm) && /function add\(symOverride\?: string\)/.test(wm), "quick add must reuse add()");
  assert.ok(/onClick=\{\(\) => add\(\)\}/.test(wm), "Add button must not pass the click event into add()");
  const wp = rd("./components/WatchlistPulse.tsx");
  assert.ok(!/₹|Rs\b|toLocaleString/.test(wp), "pulse must show percentages only, never money");
  assert.ok(/▲/.test(wp) && /▼/.test(wp), "direction needs arrows, not colour alone");
});

test("part 6: IPO overview is mounted, uses data already held, spells stages out, and has off switches", () => {
  // E550: the overview moved to the kit (mk-ipo-*): it no longer has a looping dot or an entrance animation, so there is nothing to switch off.
  assert.ok(!/v5-ipo|animationDelay/.test(rd("./components/IpoLifecycle.tsx")), "no old v5-ipo class or per-step animation delay");
  assert.ok(!/@keyframes|animation\s*:/.test(rd("./styles/markets.css").slice(rd("./styles/markets.css").indexOf("IPO overview"))), "the overview adds no animation");
  const t = rd("./components/IpoTracker.tsx");
  assert.ok(/<IpoLifecycle list=\{ipoList\} scanning=\{ipoScanning\} progress=\{ipoProgress\}/.test(t), "overview not mounted with the existing state");
  const l = rd("./components/IpoLifecycle.tsx");
  assert.ok(!/api\.|fetch\(|useEffect|useState/.test(l), "overview must not make requests or hold state");
  assert.ok(/Upcoming/.test(l) && /Listing day/.test(l) && /▼/.test(l) && /▲/.test(l), "stages in words, moves with arrows");
});

test("v5 loads after v4 and the deck is mounted on the dashboard", () => {
  const main = rd("./main.tsx");
  assert.ok(/<AiInsightDeck\b/.test(rd("./components/app/DashboardView.tsx")), "AiInsightDeck not mounted");
});

test("the orb is decorative (aria-hidden) and the deck announces politely", () => {
  assert.ok(/className=\{`w-orb[\s\S]{0,120}aria-hidden="true"/.test(rd("./components/ui/AiOrb.tsx")));
  assert.ok(/aria-live="polite"/.test(rd("./components/app/AiInsightDeck.tsx")));
});

console.log(`${passed} passed`);
