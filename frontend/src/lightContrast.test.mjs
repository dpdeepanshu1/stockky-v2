// group 464: guards for the light-theme contrast layer (run: node --experimental-strip-types src/lightContrast.test.mjs)
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
const css = readFileSync(new URL("./light-contrast.css", import.meta.url), "utf8");
const main = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");
let passed = 0;
const test = (n, f) => { f(); passed += 1; console.log("ok -", n); };
const tok = (name) => { const m = css.match(new RegExp(`--${name}-rgb:\\s*(\\d+) (\\d+) (\\d+)`)); assert.ok(m, `token ${name} missing`); return m.slice(1).map(Number); };
const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
const mix = (f, b, a) => f.map((v, i) => Math.round(v * a + b[i] * (1 - a)));
const WHITE = [255, 255, 255], PAGE = [245, 246, 252];

test("light-contrast.css is imported after every other stylesheet except the group-465 alignment layer", () => {
  const imports = [...main.matchAll(/import "\.\/([\w-]+\.css)"/g)].map((m) => m[1]).filter((f) => f !== "alignment.css");
  assert.equal(imports[imports.length - 1], "light-contrast.css");
});
test("white labels on the primary gradient stay >= 4.5:1, even after the darkening hover", () => {
  const a2 = tok("accent-2");
  assert.ok(ratio(WHITE, a2) >= 4.5, `accent-2 ${ratio(WHITE, a2).toFixed(2)}`);
  assert.ok(ratio(WHITE, a2.map((v) => Math.round(v * 0.92))) >= 4.5);
  assert.ok(css.includes("brightness(0.92)") && !/hover[^{]*\{[^}]*brightness\(1\.0/.test(css));
});
test("ink colours pass AA on their own 35% tint over white and over the page", () => {
  const base = { buy: [4, 120, 87], sell: [197, 38, 62], warn: [146, 80, 0], accent: [0, 122, 88] };
  for (const [n, c] of Object.entries(base)) for (const bg of [WHITE, PAGE]) {
    const r = ratio(tok(`${n}-ink`), mix(c, bg, 0.35));
    assert.ok(r >= 4.5, `${n}-ink on tint ${r.toFixed(2)}`);
  }
});
test("disabled buttons are readable (>= 4.5:1) and not opacity-faded", () => {
  assert.ok(ratio(tok("disabled-fg"), tok("disabled-bg")) >= 4.5);
  assert.match(css, /\.btn-primary-term:disabled[\s\S]*?opacity: 1;/);
});
test("plain text-white can no longer land on a pale background", () => {
  assert.match(css, /\.text-white:not\(\[class\*="bg-signal-"\]\)/);
  assert.match(css, /\.hover\\:text-white:hover/);
});
console.log(`${passed} light-contrast tests passed`);
