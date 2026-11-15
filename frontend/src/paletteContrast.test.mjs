// E558 (F4): the contrast test covers the WHOLE palette in both themes, read from styles/tokens.css (run: node --experimental-strip-types src/paletteContrast.test.mjs)
// Text colours must reach WCAG AA (4.5:1) on every surface they can sit on; the label colour on a solid fill must too.
// Hairline borders are decorative and are not part of this: they carry no information on their own.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { test } from "node:test";

const css = readFileSync(new URL("./styles/tokens.css", import.meta.url), "utf8");
const block = (sel) => { const i = css.indexOf(`${sel} {`); assert.ok(i >= 0, `${sel} block missing`); return css.slice(i, css.indexOf("}", i)); };
const parse = (b) => Object.fromEntries([...b.matchAll(/--([\w-]+)-rgb:\s*(\d+) (\d+) (\d+)/g)].map((m) => [m[1], [+m[2], +m[3], +m[4]]]));
const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

const THEMES = { dark: parse(block(":root")), light: parse(block("html.light")) };
const SURFACES = ["bg", "panel", "panel-2"];
const TEXT = ["fg", "muted", "muted-2", "accent", "buy", "sell", "warn", "info"];
const SOLIDS = ["accent", "buy", "sell", "warn", "info"];

test("every theme defines the whole palette", () => {
  for (const [name, p] of Object.entries(THEMES)) for (const k of [...SURFACES, ...TEXT, ...SOLIDS, "on-solid", "border", "border-strong"]) assert.ok(p[k], `${name}: --${k}-rgb missing`);
});
for (const [name, p] of Object.entries(THEMES)) {
  test(`${name}: every text colour reaches 4.5:1 on bg, panel and panel-2`, () => {
    const bad = [];
    for (const fg of TEXT) for (const bg of SURFACES) { const r = ratio(p[fg], p[bg]); if (r < 4.5) bad.push(`${fg} on ${bg} ${r.toFixed(2)}`); }
    assert.deepEqual(bad, []);
  });
  test(`${name}: the label colour on every solid fill reaches 4.5:1`, () => {
    const bad = [];
    for (const s of SOLIDS) { const r = ratio(p["on-solid"], p[s]); if (r < 4.5) bad.push(`on-solid on ${s} ${r.toFixed(2)}`); }
    assert.deepEqual(bad, []);
  });
}
