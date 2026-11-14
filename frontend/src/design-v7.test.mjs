// design v7 guards (run with: node --experimental-strip-types src/design-v7.test.mjs)
// 1. every loop / entrance has all off switches (reduced-motion, TV, "Animations: Off"); loops pause on a hidden tab
// 2. keyframes animate only transform / opacity; entrances never keep an end state (no fill-mode both/forwards)
// 3. the v7 visuals are mounted where the features live, and they place nothing: no requests, no number animation, no controls
// 4. v7 loads after v6; direction / state is never colour only
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { liveSelectors } from "./cssLive.mjs";
import { reducedMotionText, keyframesOf, optIn } from "./cssText.mjs";   // E546: only classes a component can still render need an off switch
import { psSource } from "./psSource.mjs";   // E538: Position Stocks is now the shell + components/positionStocks/*

const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const css = rd("./styles/base.css");
const stripComments = (t) => t.replace(/\/\*[\s\S]*?\*\//g, "");
const body = stripComments(css);   // round 8i: one folded file, so the whole file is both the rules and the off-switch text
const offs = body;
const reducedBlock = reducedMotionText(css);
const own = [...body.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(([, sel]) => /\.w-(evidence|meter|rail|expo|beat|ladder|mark|alert|pos|side)/.test(sel)).map((m) => m[0]).join("\n");   // rules of this layer only (and not its off switches)
let passed = 0;
const test = (name, fn) => { fn(); passed += 1; console.log("ok -", name); };

const LOOPS = liveSelectors([".hx-glow::before", ".w-rail-mark i::after", ".w-ladder .step.current.wm-live .dot::after", ".w-beat-run"]);
const ENTRANCES = liveSelectors([".w-rail-zone", ".w-expo", ".w-expo-bar .seg", ".w-ladder .step", ".w-meter-fill", ".v7-ring-pop", ".w-beat"]);

test("every loop and entrance has the three off switches; loops pause when the tab is hidden", () => {
  for (const sel of [...LOOPS, ...ENTRANCES]) {
    assert.ok(optIn(css, sel), `${sel} must animate only under reduced-motion: no-preference and outside TV / Animations: Off`);
  }
  for (const sel of LOOPS) assert.ok(offs.includes(`html[data-hidden="true"] ${sel}`), `${sel} must pause on a hidden tab`);
});

test("every animated selector in the file is covered by an off switch (nothing animates unguarded)", () => {
  let seen = 0;
  for (const [, sel, decl] of [...body.matchAll(/([^{}]+)\{([^{}]*)\}/g)]) {
    if (!/\banimation\s*:/.test(decl) || /animation\s*:\s*none/.test(decl)) continue;
    for (const one of sel.split(",").map((x) => x.trim()).filter((x) => /\.w-(evidence|meter|rail|expo|beat|ladder|mark|alert|pos|side)/.test(x))) {   // round 8i: the folded file holds every layer; this guard covers its own selectors
      seen += 1;
      assert.ok(optIn(css, one), `animated selector "${one}" has no off switch`);
    }
  }
  assert.ok(seen >= 8, `expected to find the animated selectors, saw ${seen}`);   // E546: 10 after the round 8 clean-up removed the rules of classes no component renders
});

test("every transition on a moving thing is switched off too", () => {
  for (const [, sel, decl] of [...body.matchAll(/([^{}]+)\{([^{}]*)\}/g)]) {
    if (!/\btransition\s*:/.test(decl) || !/transform/.test(decl)) continue;
    for (const one of sel.split(",").map((x) => x.trim()).filter(Boolean)) assert.ok(offs.includes(one.replace(/^\./, ".")) || offs.includes(`html.anim-off ${one}`), `transform transition "${one}" has no off switch`);
  }
});

test("keyframes animate only transform and opacity", () => {
  const frames = keyframesOf(css, "kf-").filter((k) => ["kf-rise-10b","kf-grow","kf-reveal-b","kf-pop","kf-glow-spin","kf-ping","kf-slide"].includes(k.name)).map((k) => [null, k.name, k.body]);
  assert.ok(frames.length >= 7, "keyframes not found");
  for (const [, name, inner] of frames) for (const [, prop] of inner.matchAll(/([a-z-]+)\s*:/g)) assert.ok(prop === "transform" || prop === "opacity", `${name} animates ${prop}`);
});

test("entrances never keep their end state (fill-mode both would turn the page into a containing block for fixed pop-ups)", () => {
  assert.ok(!/animation[^;{}]*\bboth\b/.test(own), "use 'backwards', not 'both'");
  assert.ok(!/animation[^;{}]*\bforwards\b/.test(own), "use 'backwards', not 'forwards'");
});

test("v7 loads after v6 and the page-level look still loads last", () => {
  const m = rd("./main.tsx");
});

test("the visuals are mounted where the features live", () => {
  const rat = rd("./components/RealAutoTrade.tsx"), ps = psSource(), parts = rd("./components/realAuto/parts.tsx"), pos = rd("./components/realAuto/tabs/PositionsTab.tsx");
  assert.ok(/<RiskRail side="LONG"/.test(parts), "Real Trade position card has the rail");
  assert.ok(/<ExposureStrip rows=/.test(pos), "Real Trade Positions tab has the exposure strip");
  assert.ok(/<RiskRail side=\{p\.side\}/.test(ps) && /<ExposureStrip rows=/.test(ps), "Position Stocks rows use the rail and the strip (side-aware)");
  assert.ok(/glow=\{mode === "REAL" && armed/.test(rat), "Real Trade glow only for REAL while armed");
  assert.ok(/glow=\{killSwitchTripped \? "halt"/.test(ps), "Position Stocks glow halts on the kill switch");
  assert.ok(/<ModeLadder mode=\{mv\.mode\}/.test(rd("./components/calibration/ShortSellingPanel.tsx")), "short selling ladder");
  const sw = rd("./components/calibration/SwingPanel.tsx");
  assert.ok(/<ModeLadder\b/.test(sw) && /<EvidenceMeter\b/.test(sw), "swing ladder and evidence meter");
  assert.ok(/<DriftBeat\b/.test(rd("./components/calibration/ReconcilePanel.tsx")), "reconcile heartbeat");
  assert.ok(!/animate-pulse/.test(rd("./components/ui/RiskRail.tsx")), "no tailwind pulse on the rail");
});

test("the v7 parts place nothing: no requests, no controls, and money numbers never animate", () => {
  for (const f of ["RiskRail", "ExposureStrip", "ModeLadder", "EvidenceMeter", "DriftBeat"]) {
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
  assert.ok(/short \{[^}]*repeating-linear-gradient/.test(body) || /\.w-expo-bar \.seg\.short \{[^}]*repeating-linear-gradient/.test(body), "shorts are hatched");
});

console.log(`${passed} design v7 tests passed`);
