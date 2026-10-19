// group 465: guards for the layout alignment layer (run: node --experimental-strip-types src/alignment.test.mjs)
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const css = readFileSync(new URL("./alignment.css", import.meta.url), "utf8");
const main = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");
const movers = readFileSync(new URL("./components/MarketMovers.tsx", import.meta.url), "utf8");
let passed = 0;
const test = (n, f) => { f(); passed += 1; console.log("ok -", n); };

test("alignment.css is imported last", () => {
  const imports = [...main.matchAll(/import "\.\/([\w-]+\.css)"/g)].map((m) => m[1]);
  assert.equal(imports[imports.length - 1], "alignment.css");
});
test("desktop top bar is forced off below 768px and the mobile bars off from 768px", () => {
  assert.match(css, /@media \(max-width: 767px\)\s*\{\s*\.app-topbar \{ display: none !important; \}/);
  assert.match(css, /@media \(min-width: 768px\)\s*\{\s*\.terminal-topbar, \.terminal-bottom-nav \{ display: none !important; \}/);
});
test("skip link is moved out by its own height, not a fixed -64px", () => {
  assert.match(css, /\.skip-link \{[^}]*translateY\(calc\(-100% - /);
  assert.match(css, /\.skip-link:focus \{[^}]*transform: none/);
});
test("sidebar rows tighten in height tiers and TV gets a scroll fade", () => {
  for (const h of [900, 800, 700]) assert.ok(css.includes(`(max-height: ${h}px)`), `tier ${h}`);
  assert.match(css, /html\.tv \.sidebar-nav \{[\s\S]*mask-image/);
});
test("dashboard hero lets the title keep its width and wraps the clock below on phones", () => {
  assert.match(css, /\.dash-hero-top \{ flex-wrap: wrap/);
  assert.match(css, /flex-basis: 100%/);
});
test("Market Movers header wraps instead of squeezing its title", () => {
  assert.match(movers, /flex flex-wrap items-center justify-between gap-x-3 gap-y-2 mb-4/);
  assert.match(movers, /whitespace-nowrap">📊 Market Movers/);
});
console.log(`${passed} alignment tests passed`);
