// Run: node --experimental-strip-types src/design-v8.test.mjs. E277: every animation in design-v8.css has the off switches; keyframes use transform / opacity only.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
const css = readFileSync(new URL("./design-v8.css", import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const reduced = css.slice(css.indexOf("@media (prefers-reduced-motion"));
let seen = 0;
for (const [, sel, decl] of [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]) {
  if (!/\banimation\s*:/.test(decl) || /animation\s*:\s*none/.test(decl)) continue;
  for (const one of sel.split(",").map((x) => x.trim()).filter(Boolean)) {
    seen++;
    assert.ok(reduced.includes(one) && css.includes(`html.tv ${one}`) && css.includes(`html.v6-calm ${one}`), `animated selector "${one}" has no off switch`);
  }
}
assert.ok(seen >= 4, `saw ${seen} animated selectors`);
assert.ok(css.includes('html[data-hidden="true"] .v8-slot.now .dot::after'), "the loop pauses on a hidden tab");
for (const [, name, inner] of [...css.matchAll(/@keyframes\s+([\w-]+)\s*\{([\s\S]*?\})\s*\}/g)]) for (const [, p] of inner.matchAll(/([a-z-]+)\s*:/g)) assert.ok(p === "transform" || p === "opacity", `${name} animates ${p}`);
assert.ok(!/animation[^;{}]*\b(both|forwards)\b/.test(css), "use backwards");
const m = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");
assert.ok(m.indexOf("design-v8.css") > m.indexOf("design-v7.css") && m.indexOf("alignment.css") > m.indexOf("design-v8.css"), "v8 after v7, alignment last");
console.log("design-v8.test.mjs: ok");
