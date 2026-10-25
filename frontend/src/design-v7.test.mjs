// design v7 guards (run with: node --experimental-strip-types src/design-v7.test.mjs)
// 1. every loop / entrance has all off switches (reduced-motion, TV, "Animations: Off"); loops pause on a hidden tab
// 2. keyframes animate only transform / opacity; entrances never keep an end state (no fill-mode both/forwards)
// 3. the v7 visuals are mounted where the features live, and they place nothing: no requests, no number animation, no controls
// 4. v7 loads after v6; direction / state is never colour only
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";

const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const css = rd("./design-v7.css");
const stripComments = (t) => t.replace(/\/\*[\s\S]*?\*\//g, "");
const offAt = css.indexOf("/* -- off switches");
assert.ok(offAt > 0, "off-switch section marker missing");
const body = stripComments(css.slice(0, offAt));
const offs = stripComments(css.slice(offAt));
const reducedBlock = offs.slice(offs.indexOf("@media (prefers-reduced-motion"), offs.indexOf("html.tv "));
let passed = 0;
const test = (name, fn) => { fn(); passed += 1; console.log("ok -", name); };

const LOOPS = [".v7-glow::before", ".v7-rail-mark i::after", ".v7-ladder .step.current.v7m-live .dot::after", ".v7-beat-run"];
const ENTRANCES = [".v7-rail-zone", ".v7-expo", ".v7-expo-bar .seg", ".v7-ladder .step", ".v7-meter-fill", ".v7-ring-pop", ".v7-beat"];

test("every loop and entrance has the three off switches; loops pause when the tab is hidden", () => {
  for (const sel of [...LOOPS, ...ENTRANCES]) {
    assert.ok(reducedBlock.includes(sel), `${sel} missing from the reduced-motion block`);
    assert.ok(offs.includes(`html.tv ${sel}`), `${sel} missing from the TV off switch`);
    assert.ok(offs.includes(`html.v6-calm ${sel}`), `${sel} missing from the Animations: Off switch`);
  }
  for (const sel of LOOPS) assert.ok(offs.includes(`html[data-hidden="true"] ${sel}`), `${sel} must pause on a hidden tab`);
});

test("every animated selector in the file is covered by an off switch (nothing animates unguarded)", () => {
  let seen = 0;
  for (const [, sel, decl] of [...body.matchAll(/([^{}]+)\{([^{}]*)\}/g)]) {
    if (!/\banimation\s*:/.test(decl) || /animation\s*:\s*none/.test(decl)) continue;
    for (const one of sel.split(",").map((x) => x.trim()).filter(Boolean)) {
      seen += 1;
      assert.ok(reducedBlock.includes(one) && offs.includes(`html.tv ${one}`) && offs.includes(`html.v6-calm ${one}`), `animated selector "${one}" has no off switch`);
    }
  }
  assert.ok(seen >= 11, `expected to find the animated selectors, saw ${seen}`);
});

test("every transition on a moving thing is switched off too", () => {
  for (const [, sel, decl] of [...body.matchAll(/([^{}]+)\{([^{}]*)\}/g)]) {
    if (!/\btransition\s*:/.test(decl) || !/transform/.test(decl)) continue;
    for (const one of sel.split(",").map((x) => x.trim()).filter(Boolean)) assert.ok(offs.includes(one.replace(/^\./, ".")) || offs.includes(`html.v6-calm ${one}`), `transform transition "${one}" has no off switch`);
  }
});

test("keyframes animate only transform and opacity", () => {
  const frames = [...body.matchAll(/@keyframes\s+([\w-]+)\s*\{([\s\S]*?)\}\s*(?=@keyframes|$)/g)];
  assert.ok(frames.length >= 7, "keyframes not found");
  for (const [, name, inner] of frames) for (const [, prop] of inner.matchAll(/([a-z-]+)\s*:/g)) assert.ok(prop === "transform" || prop === "opacity", `${name} animates ${prop}`);
});

test("entrances never keep their end state (fill-mode both would turn the page into a containing block for fixed pop-ups)", () => {
  assert.ok(!/animation[^;{}]*\bboth\b/.test(body), "use 'backwards', not 'both'");
  assert.ok(!/animation[^;{}]*\bforwards\b/.test(body), "use 'backwards', not 'forwards'");
});

test("v7 loads after v6 and the page-level look still loads last", () => {
  const m = rd("./main.tsx");
  assert.ok(m.indexOf("design-v6.css") > 0 && m.indexOf("design-v7.css") > m.indexOf("design-v6.css"), "v7 after v6");
  assert.ok(m.indexOf("alignment.css") > m.indexOf("design-v7.css"), "alignment.css stays last");
});

test("the visuals are mounted where the features live", () => {
  const rat = rd("./components/RealAutoTrade.tsx"), ps = rd("./components/PositionStocksTab.tsx"), parts = rd("./components/realAuto/parts.tsx"), pos = rd("./components/realAuto/tabs/PositionsTab.tsx");
  assert.ok(/<RiskRail side="LONG"/.test(parts), "Real Trade position card has the rail");
  assert.ok(/<ExposureStrip rows=/.test(pos), "Real Trade Positions tab has the exposure strip");
  assert.ok(/<RiskRail side=\{p\.side\}/.test(ps) && /<ExposureStrip rows=/.test(ps), "Position Stocks rows use the rail and the strip (side-aware)");
  assert.ok(/glow=\{mode === "REAL" && armed/.test(rat), "Real Trade glow only for REAL while armed");
  assert.ok(/glow=\{killSwitchTripped \? "halt"/.test(ps), "Position Stocks glow halts on the kill switch");
  assert.ok(/<CapitalRing\b/.test(rd("./components/CapitalSplitCard.tsx")), "capital ring on the shared capital card");
  assert.ok(/<ModeLadder mode=\{mv\.mode\}/.test(rd("./components/calibration/ShortSellingPanel.tsx")), "short selling ladder");
  const sw = rd("./components/calibration/SwingPanel.tsx");
  assert.ok(/<ModeLadder\b/.test(sw) && /<EvidenceMeter\b/.test(sw), "swing ladder and evidence meter");
  assert.ok(/<DriftBeat\b/.test(rd("./components/calibration/ReconcilePanel.tsx")), "reconcile heartbeat");
  assert.ok(!/animate-pulse/.test(rd("./components/ui/RiskRail.tsx")), "no tailwind pulse on the rail");
});

test("the v7 parts place nothing: no requests, no controls, and money numbers never animate", () => {
  for (const f of ["RiskRail", "ExposureStrip", "ModeLadder", "EvidenceMeter", "CapitalRing", "DriftBeat"]) {
    const t = rd(`./components/ui/${f}.tsx`);
    assert.ok(!/realTradeApi\.|positionStocksApi\.|controlApi\.|fetch\(|XMLHttpRequest|onClick|<button|<input|<select/.test(t), `${f} must be display-only`);
    assert.ok(!/PriceRoll|GlintBox|useTickGlint|requestAnimationFrame|countUp|setInterval|setTimeout/i.test(t), `${f} must not animate numbers`);
  }
  assert.ok(!/realTradeApi|positionStocksApi|fetch\(|XMLHttpRequest/.test(rd("./v7View.ts")), "v7View is pure");
  assert.ok(!/^\s*import\s/m.test(rd("./v7View.ts")), "v7View has no runtime imports");
});

test("direction and state are never colour only", () => {
  assert.ok(/arrow = "▲"/.test(rd("./v7View.ts")) && /arrow = "▼"/.test(rd("./v7View.ts")), "rail arrows");
  assert.ok(/"▲"[\s\S]*"▼"/.test(rd("./components/ui/ExposureStrip.tsx").replace(/v\.pnlTotal/g, "x")) || /▲/.test(rd("./components/ui/ExposureStrip.tsx")), "exposure arrows");
  assert.ok(/SYMBOL = \{ past: "✓", current: "●", future: "○" \}/.test(rd("./components/ui/ModeLadder.tsx")), "ladder symbols");
  assert.ok(/WORDS = \{ steady: "✓/.test(rd("./components/ui/DriftBeat.tsx")), "beat words");
  assert.ok(/short \{[^}]*repeating-linear-gradient/.test(body) || /\.v7-expo-bar \.seg\.short \{[^}]*repeating-linear-gradient/.test(body), "shorts are hatched");
});

test("the ring's rotation sits on an inner group so the CSS pop does not replace it", () => {
  const t = rd("./components/ui/CapitalRing.tsx");
  assert.ok(/<g className="v7-ring-pop">\s*(\{[^}]*\}\s*)?<g transform="rotate\(-90 60 60\)">/.test(t.replace(/\{\/\*[\s\S]*?\*\/\}/g, "")), "rotate lives on the inner <g>");
  assert.ok(!/className="v7-ring-pop"[^>]*transform=/.test(t), "no transform attribute on the animated group");
});

console.log(`${passed} design v7 tests passed`);
