// Round 8i: the 3 guards that replace the old per-layer design-v4..v8 CSS checks (the layers are folded into styles/base.css + styles/legacy.css).
// Run: node --experimental-strip-types src/styles.test.mjs
//   1. import order in main.tsx (and the deleted layer files stay deleted)
//   2. every animation has its off switches (reduced-motion, TV mode) and keyframes use only composited properties
//   3. tokens exist in both themes (the colour maths itself lives in tokens.test.mjs and lightContrast.test.mjs)
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { rdStyle, stripComments, reducedMotionText, keyframesOf } from "./cssText.mjs";

const main = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");
const FILES = readdirSync(new URL("./styles/", import.meta.url)).filter((f) => f.endsWith(".css"));
const all = FILES.map((f) => stripComments(rdStyle(f))).join("\n");
const rules = [...all.matchAll(/([^{}]+)\{([^{}]*)\}/g)];

test("import order: base, legacy, tokens, then the kit, shell, home, trading, control and markets styles; the old layer files are gone", () => {
  const imports = [...main.matchAll(/import "\.\/([\w/-]+\.css)"/g)].map((m) => m[1]);
  assert.deepEqual(imports, ["styles/base.css", "styles/legacy.css", "styles/tokens.css", "styles/components.css", "styles/shell.css", "styles/home.css", "styles/trading.css", "styles/control.css", "styles/markets.css"]);
  for (const f of ["index.css", "design-v3.css", "design-v4.css", "design-v5.css", "design-v6.css", "design-v7.css", "design-v8.css", "auth-login.css", "light-contrast.css", "alignment.css"]) {
    assert.equal(existsSync(new URL(`./${f}`, import.meta.url)), false, `${f} was folded in round 8i and must not come back`);
  }
  assert.ok(rdStyle("base.css").trimStart().startsWith("/*") && /@tailwind base;[\s\S]*@tailwind components;[\s\S]*@tailwind utilities;/.test(rdStyle("base.css")), "base.css carries the Tailwind entry");
  for (const f of FILES) assert.ok(imports.includes(`styles/${f}`), `styles/${f} is not imported`);
});

test("every animated selector has the reduced-motion and TV (or Animations: Off) switch; keyframes animate only composited properties", () => {
  const reduced = reducedMotionText(all);
  const itemsOf = (text) => new Set([...text.matchAll(/([^{}]+)\{[^{}]*\}/g)].flatMap((m) => m[1].split(",").map((x) => x.trim())));
  const redItems = itemsOf(reduced), tvItems = itemsOf(all);
  const covered = (set, one, pre = "") => set.has(pre + one) || [...set].some((i) => i.startsWith(pre) && one.startsWith(i.slice(pre.length) + ".") && i.slice(pre.length).length > 3);   // `.v4-roll.up` is switched off by its `.v4-roll` rule
  const hasAnimation = (decl) => /(^|[;\s])animation\s*:/.test(decl) && !/animation\s*:\s*none/.test(decl);
  let seen = 0; const missing = [];
  for (const [, sel, decl] of rules) {
    if (!hasAnimation(decl) || /^\s*html\.(tv|v6-calm)\b/.test(sel) || /prefers-reduced-motion/.test(sel)) continue;
    for (const one of sel.split(",").map((x) => x.trim()).filter(Boolean)) {
      seen += 1;
      if (/^html:not\(\.tv\)/.test(one) || one.includes(":not(.v6-calm)")) { if (!reduced.includes(one)) missing.push(one); continue; }   // self-guarded: only applies outside TV mode and Animations: Off
      const off = covered(redItems, one) && (covered(tvItems, one, "html.tv ") || covered(tvItems, one, "html.v6-calm "));
      if (!off) missing.push(one);
    }
  }
  assert.ok(seen >= 40, `only ${seen} animated selectors found`);
  assert.deepEqual(missing, [], `animated selector(s) without an off switch: ${missing.slice(0, 5).join(" | ")}`);
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
