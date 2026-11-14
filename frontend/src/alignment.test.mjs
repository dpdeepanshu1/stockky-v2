// group 465: guards for the layout alignment layer (run: node --experimental-strip-types src/alignment.test.mjs)
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const css = readFileSync(new URL("./styles/legacy.css", import.meta.url), "utf8");
const main = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");
const movers = readFileSync(new URL("./components/MarketMovers.tsx", import.meta.url), "utf8");
let passed = 0;
const test = (n, f) => { f(); passed += 1; console.log("ok -", n); };

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
test("the old dashboard hero rules are gone with the hero (the Home page moved to home.css in round 4; round 8 deleted the dead rules)", () => {
  assert.ok(!/\.dash-hero/.test(css), "no .dash-hero rule is left in alignment.css");
});
test("Market Movers (E536) uses the kit card header and a scrolling-safe tab strip, so its title cannot be squeezed", () => {
  assert.match(movers, /<CardHeader title="Market movers"/);
  assert.match(movers, /<Tabs<MarketType>/);
});
console.log(`${passed} alignment tests passed`);
