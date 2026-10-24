// design v6 guards (run with: node --experimental-strip-types src/design-v6.test.mjs)
// 1. every loop / entrance animation has all off switches (reduced-motion, TV, "Animations: Off") and loops pause on a hidden tab
// 2. keyframes animate only transform / opacity; entrances never keep a transform after they end (fill-mode "both" would trap fixed pop-ups)
// 3. the hero, KPI tiles and AI strip are mounted on the three pages; money never animates; direction is never colour-only
// 4. v6 loads after v5; the Calibration switches slide and stay accessible
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";

const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const css = rd("./design-v6.css");
const stripComments = (t) => t.replace(/\/\*[\s\S]*?\*\//g, "");
const offAt = css.indexOf("/* -- off switches");
assert.ok(offAt > 0, "off-switch section marker missing");
const body = stripComments(css.slice(0, offAt));
const offs = stripComments(css.slice(offAt));
const reducedBlock = offs.slice(offs.indexOf("@media (prefers-reduced-motion"), offs.indexOf("html.tv "));
let passed = 0;
const test = (name, fn) => { fn(); passed += 1; console.log("ok -", name); };

const LOOPS = [".v6-hero::before", ".v6-hero::after", ".v6-art-wave", ".v6-candle", ".v6-node", ".v6-kicker i::after", ".v6-pulse.working::after"];
const ENTRANCES = [".v6-hero", ".v6-kpi", ".v6-stage > *", ".v6-flow > *", ".v6-card", ".v6-mixbar", ".v6-spark-draw", ".v6-row", ".v6-detail"];

test("every loop and entrance has the three off switches; loops pause when the tab is hidden", () => {
  for (const sel of [...LOOPS, ...ENTRANCES]) {
    assert.ok(reducedBlock.includes(sel), `${sel} missing from the reduced-motion block`);
    assert.ok(offs.includes(`html.tv ${sel}`), `${sel} missing from the TV off switch`);
    assert.ok(offs.includes(`html.v6-calm ${sel}`), `${sel} missing from the Animations: Off switch`);
  }
  for (const sel of LOOPS) assert.ok(offs.includes(`html[data-hidden="true"] ${sel}`), `${sel} must pause on a hidden tab`);
});

test("every animated selector in the file is covered by an off switch (nothing animates unguarded)", () => {
  const rules = [...body.matchAll(/([^{}]+)\{([^{}]*)\}/g)];
  let seen = 0;
  for (const [, sel, decl] of rules) {
    if (!/\banimation\s*:/.test(decl)) continue;
    for (const one of sel.split(",").map((x) => x.trim()).filter(Boolean)) {
      seen += 1;
      assert.ok(reducedBlock.includes(one) && offs.includes(`html.tv ${one}`) && offs.includes(`html.v6-calm ${one}`), `animated selector "${one}" has no off switch`);
    }
  }
  assert.ok(seen >= 14, "expected to find the animated selectors");
});

test("keyframes animate only transform and opacity", () => {
  const frames = [...body.matchAll(/@keyframes\s+([\w-]+)\s*\{([\s\S]*?)\}\s*(?=@keyframes|$)/g)];
  assert.ok(frames.length >= 8, "keyframes not found");
  for (const [, name, inner] of frames) {
    for (const [, prop] of inner.matchAll(/([a-z-]+)\s*:/g)) assert.ok(prop === "transform" || prop === "opacity", `${name} animates ${prop}`);
  }
});

test("entrances never keep their end state (fill-mode both would turn the page into a containing block for fixed pop-ups)", () => {
  assert.ok(!/animation[^;{}]*\bboth\b/.test(body), "use 'backwards', not 'both'");
  assert.ok(!/animation[^;{}]*\bforwards\b/.test(body), "use 'backwards', not 'forwards'");
});

test("the hero, KPI strip and AI strip are mounted on the three pages with state the pages already hold", () => {
  const rat = rd("./components/RealAutoTrade.tsx"), ps = rd("./components/PositionStocksTab.tsx"), cal = rd("./components/CalibrationControlTab.tsx");
  for (const [name, t] of [["RealAutoTrade", rat], ["PositionStocksTab", ps], ["CalibrationControlTab", cal]]) {
    assert.ok(/<PageHero\b/.test(t), `${name}: hero`);
    assert.ok(/<AiPulse\b/.test(t), `${name}: AI strip`);
  }
  assert.ok(/<KpiStrip\b/.test(rat) && /<KpiStrip\b/.test(ps), "KPI tiles on Real Trade and Position Stocks");
  assert.ok(!/realTradeApi\.[a-zA-Z]+\(|fetch\(/.test(rd("./components/ui/KpiStrip.tsx") + rd("./components/ui/AiPulse.tsx") + rd("./components/ui/PageHero.tsx")), "UI parts make no requests");
});

test("money never animates: no count-up, PriceRoll or GlintBox in the v6 components", () => {
  for (const f of ["PageHero", "KpiStrip", "AiPulse", "Spark"]) {
    const t = rd(`./components/ui/${f}.tsx`);
    assert.ok(!/PriceRoll|GlintBox|useTickGlint|requestAnimationFrame|countUp/i.test(t), `${f} must not animate numbers`);
  }
  assert.ok(!/PriceRoll|GlintBox|useTickGlint/.test(rd("./components/realAuto/tabs/HistoryTab.tsx")), "History tab numbers stay still");
});

test("direction is never colour only: arrows / signs are in the text", () => {
  assert.ok(/ARROW[\s\S]*▲[\s\S]*▼/.test(rd("./components/ui/KpiStrip.tsx")), "KPI arrows");
  assert.ok(/SYMBOL[\s\S]*✓[\s\S]*⚠[\s\S]*✕/.test(rd("./components/ui/AiPulse.tsx")), "chip symbols");
  const h = rd("./components/realAuto/tabs/HistoryTab.tsx");
  assert.ok(/"▲ "/.test(h) && /"▼ "/.test(h), "P&L arrows in the history rows");
});

test("the artwork is decorative and the animation switch is a labelled toggle that remembers its choice safely", () => {
  const t = rd("./components/ui/PageHero.tsx");
  assert.ok(/<svg className="v6-art"[^>]*aria-hidden="true"/.test(t), "artwork aria-hidden");
  assert.ok(/role="switch"/.test(t) && /aria-checked=\{!calm\}/.test(t) && /Animations: /.test(t), "labelled switch");
  assert.ok(/try \{[^}]*localStorage\.getItem[^}]*\} catch/.test(t) && /try \{[^}]*localStorage\.setItem[^}]*\} catch/.test(t), "storage wrapped in try/catch");
});

test("v6 loads after v5; the Calibration switch keeps role + aria-checked and gets the v6 class", () => {
  const main = rd("./main.tsx");
  assert.ok(main.indexOf("design-v6.css") > main.indexOf("design-v5.css"), "v6 must import after v5");
  const sw = rd("./components/calibration/SwitchesPanel.tsx");
  assert.ok(/role="switch" aria-checked=\{cur === "true"\}/.test(sw) && /className=\{`v6-switch \$\{cur === "true" \? "on" : "off"\}`\}/.test(sw), "switch markup");
  assert.ok(/\.v6-switch\.on::before\s*\{[^}]*transform: translateX/.test(css), "knob slides");
});

console.log(`${passed} passed`);
