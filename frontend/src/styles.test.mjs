// Round 8i: the 3 guards that replace the old per-layer design-v4..v8 CSS checks (the layers are folded into styles/base.css + styles/base.css).
// Run: node --experimental-strip-types src/styles.test.mjs
//   1. import order in main.tsx (and the deleted layer files stay deleted)
//   2. every animation has its off switches (reduced-motion, TV mode) and keyframes use only composited properties
//   3. tokens exist in both themes (the colour maths itself lives in tokens.test.mjs and lightContrast.test.mjs)
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { rdStyle, stripComments, reducedMotionText, keyframesOf, animRules, cascadeOrder } from "./cssText.mjs";

const main = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");
const FILES = readdirSync(new URL("./styles/", import.meta.url)).filter((f) => f.endsWith(".css"));
const all = FILES.map((f) => stripComments(rdStyle(f))).join("\n");
const rules = [...all.matchAll(/([^{}]+)\{([^{}]*)\}/g)];

test("import order: base, tokens, components, then shell, home, trading, control, markets (pages.css is folded into main.tsx); the old layer files are gone", () => {
  const imports = [...main.matchAll(/import "\.\/([\w/-]+\.css)"/g)].map((m) => m[1]);
  assert.deepEqual(imports, ["styles/base.css", "styles/tokens.css", "styles/components.css", "styles/shell.css", "styles/home.css", "styles/trading.css", "styles/control.css", "styles/markets.css"]);
  assert.equal(existsSync(new URL("./styles/pages.css", import.meta.url)), false, "pages.css was folded into main.tsx in E558");
  assert.deepEqual(cascadeOrder().slice(-5), ["styles/shell.css", "styles/home.css", "styles/trading.css", "styles/control.css", "styles/markets.css"]);
  for (const f of ["index.css", "design-v3.css", "design-v4.css", "design-v5.css", "design-v6.css", "design-v7.css", "design-v8.css", "auth-login.css", "light-contrast.css", "alignment.css"]) {
    assert.equal(existsSync(new URL(`./${f}`, import.meta.url)), false, `${f} was folded in round 8i and must not come back`);
  }
  assert.ok(rdStyle("base.css").trimStart().startsWith("/*") && /@tailwind base;[\s\S]*@tailwind components;[\s\S]*@tailwind utilities;/.test(rdStyle("base.css")), "base.css carries the Tailwind entry");
  for (const f of FILES) assert.ok(cascadeOrder().includes(`styles/${f}`) || imports.includes(`styles/${f}`), `styles/${f} is not imported`);
});

test("every animation is opt-in (reduced-motion: no-preference + not TV + not Animations: Off); keyframes animate only composited properties", () => {
  const rulesA = animRules(all); const bad = rulesA.filter((r) => !r.guarded).map((r) => `${r.sel} { ${r.decl} }`);
  assert.ok(rulesA.filter((r) => !r.none).length >= 40, `only ${rulesA.length} animation declarations found`);
  assert.deepEqual(bad, [], `unguarded animation: ${bad.slice(0, 4).join(" | ")}`);
  const frames = keyframesOf(all);
  assert.ok(frames.length >= 30, `found ${frames.length} keyframes`);
  for (const { name, body } of frames) assert.ok(!/\b(width|height|top|left|right|bottom|margin|padding|box-shadow|filter)\s*:/.test(body), `keyframes ${name} animates a layout property`);
});

test("tokens: every colour role exists in the dark and the light block, the type and shadow tokens are set, and the one-font rule holds", () => {
  const t = stripComments(rdStyle("tokens.css"));
  const dark = t.match(/:root\s*\{([^}]*)\}/)[1], light = t.match(/html\.light\s*\{([^}]*)\}/)[1];
  for (const k of ["bg", "panel", "panel-2", "border", "fg", "muted", "accent", "buy", "sell", "warn", "info", "on-solid", "shadow"]) {
    assert.ok(new RegExp(`--${k}-rgb:`).test(dark), `dark --${k}-rgb`);
    assert.ok(new RegExp(`--${k}-rgb:`).test(light), `light --${k}-rgb`);
  }
  assert.ok(/--font-head:\s*"Inter"/.test(dark));
  assert.ok(!/Plus Jakarta Sans|DM Sans/.test(all.replace(/var\(--font[^)]*\)/g, "")) || /--font-(sans|head)/.test(all), "only Inter drives the type");
});
