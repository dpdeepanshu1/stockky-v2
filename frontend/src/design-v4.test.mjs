// group 461: guards for the design layer (run with: node --experimental-strip-types src/design-v4.test.mjs)
// 1. every looping / entering animation has an off switch for reduced-motion, TV mode and a hidden tab
// 2. every .mode-frame names its mode and spells it out in a ribbon label (never colour-only)
// 3. PriceRoll / GlintBox are only given PRICES: never a P&L, order value, quantity or charge
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import assert from "node:assert/strict";

const css = readFileSync(new URL("./design-v4.css", import.meta.url), "utf8");
let passed = 0;
const test = (name, fn) => { fn(); passed += 1; console.log("ok -", name); };

function* walk(dir) {
  for (const f of readdirSync(dir)) {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) yield* walk(p); else if (p.endsWith(".tsx")) yield p;
  }
}
const srcDir = new URL(".", import.meta.url).pathname;
const files = [...walk(srcDir)].map((p) => ({ p, s: readFileSync(p, "utf8") }));

test("looping animations are switched off for reduced-motion, TV and paused when hidden", () => {
  const reduced = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));
  for (const sel of [".v4-roll", ".v4-live-dot", ".v4-skel::after", ".page-terminal", ".v4-glint-up::after"]) {
    assert.ok(reduced.includes(sel), `${sel} missing from the reduced-motion block`);
    assert.ok(css.includes(`html.tv ${sel}`), `${sel} missing from the TV off switch`);
  }
  assert.ok(css.includes('html[data-hidden="true"] .v4-live-dot'), "live dot must pause on a hidden tab");
  assert.ok(css.includes('html[data-hidden="true"] .v4-skel::after'), "skeleton must pause on a hidden tab");
});

test("only transform, opacity (and box-shadow flash on the glint) are animated", () => {
  const frames = [];
  for (const m of css.matchAll(/@keyframes\s+([\w-]+)\s*\{/g)) {
    let depth = 1, i = m.index + m[0].length;
    while (depth > 0 && i < css.length) { if (css[i] === "{") depth += 1; else if (css[i] === "}") depth -= 1; i += 1; }
    frames.push([m[1], css.slice(m.index + m[0].length, i - 1)]);
  }
  assert.ok(frames.length >= 5, `found ${frames.length} keyframes`);
  for (const [name, body] of frames) {
    assert.ok(!/\b(width|height|top|left|margin|padding)\s*:/.test(body), `keyframes ${name} animates a layout property`);
  }
});

test("every mode-frame has data-mode and a ribbon label that spells the mode out", () => {
  let n = 0;
  for (const { p, s } of files) {
    for (const m of s.matchAll(/className="[^"]*\bmode-frame\b[^"]*"([\s\S]{0,260}?)>/g)) {
      n += 1;
      assert.ok(/data-mode=/.test(m[1]), `${p}: mode-frame without data-mode`);
      assert.ok(/data-frame-label=/.test(m[1]), `${p}: mode-frame without data-frame-label`);
      if (/data-mode="REAL"/.test(m[1])) assert.ok(/REAL MONEY/.test(m[1]), `${p}: a REAL frame must say REAL MONEY`);
    }
  }
  assert.ok(n >= 3, `expected the 3 trading pages to carry a frame, found ${n}`);
});

test("the Real Trade frame follows the DEMO / REAL switch and says which one", () => {
  const s = files.find((f) => f.p.endsWith("RealAutoTrade.tsx")).s;
  assert.ok(/data-mode=\{mode\}/.test(s));
  assert.ok(/REAL MONEY/.test(s) && /DEMO ·/.test(s));
});

test("motion helpers are only given prices", () => {
  const bad = /(PriceRoll|GlintBox)[^>]*value=\{[^}]*(pnl|profit|loss|charge|qty|quantity|amount|value_|cost|equity|balance|capital)/i;
  for (const { p, s } of files) {
    if (p.endsWith("motion.tsx")) continue;
    assert.ok(!bad.test(s), `${p}: PriceRoll / GlintBox given a money value`);
  }
});

test("group 463: skip link targets the main landmark, a page crash is contained, empty states exist", () => {
  const app = files.find((f) => f.p.endsWith("/App.tsx")).s;
  assert.ok(/href="#main-content"/.test(app) && /<main id="main-content"/.test(app), "skip link needs a target");
  assert.ok(/<ErrorBoundary key=\{tab\}/.test(app) && /<\/ErrorBoundary>\s*<\/main>/.test(app), "main content must sit inside a per-tab ErrorBoundary");
  assert.ok(/role="status"/.test(app), "toast needs a live region");
  for (const cls of [".skip-link", ".v4-empty", ".v4-empty-icon"]) assert.ok(css.includes(cls), `${cls} missing from design-v4.css`);
  for (const f of ["MarketMovers", "WatchlistManager"]) assert.ok(files.find((x) => x.p.endsWith(f + ".tsx")).s.includes("<EmptyState"), `${f} should use EmptyState`);
});

console.log(`${passed} passed`);
