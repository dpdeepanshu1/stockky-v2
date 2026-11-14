// E513: guards for styles/tokens.css (run: node --experimental-strip-types src/tokens.test.mjs)
import { readFileSync } from "node:fs";
import { cascadeOrder } from "./cssText.mjs";
import assert from "node:assert/strict";
const css = readFileSync(new URL("./styles/tokens.css", import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const main = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");
const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const block = (sel) => { const m = css.match(new RegExp(`(?:^|\\})\\s*${sel}\\s*\\{([^}]*)\\}`)); assert.ok(m, `${sel} block missing`); return m[1]; };
const dark = block(":root"), light = block("html\\.light");
const tok = (b, n) => { const m = b.match(new RegExp(`--${n}-rgb:\\s*(\\d+) (\\d+) (\\d+)`)); assert.ok(m, `token ${n} missing`); return m.slice(1).map(Number); };
const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
let n = 0; const test = (name, f) => { f(); n++; console.log("ok -", name); };

test("tokens.css is imported after base.css and before the kit styles (the kit file, if present, is last)", () => {
  const imports = cascadeOrder();   // round 532: base, tokens, components, then pages.css expanded
  const t = imports.indexOf("styles/tokens.css");
  assert.ok(imports.indexOf("styles/base.css") >= 0 && t > imports.indexOf("styles/base.css"), "base, then tokens");
  const tail = imports.slice(t + 1);
  assert.ok(tail.every((x) => ["styles/components.css", "styles/shell.css", "styles/home.css", "styles/trading.css", "styles/control.css", "styles/markets.css"].includes(x)), "only the kit, shell, home, trading, control and markets styles come after tokens.css");
});
test("every colour role exists in both themes", () => {
  for (const t of ["bg", "panel", "panel-2", "border", "border-strong", "fg", "muted", "muted-2", "accent", "accent-2", "info", "buy", "sell", "warn", "on-solid", "shadow"]) { tok(dark, t); tok(light, t); }
});
test("body text, muted text and grey labels pass AA on page and card in both themes", () => {
  for (const [name, b] of [["dark", dark], ["light", light]]) for (const bg of ["bg", "panel"]) for (const fg of ["fg", "muted", "muted-2"]) {
    const r = ratio(tok(b, fg), tok(b, bg)); assert.ok(r >= 4.5, `${name} ${fg} on ${bg} ${r.toFixed(2)}`);
  }
});
test("brand green: text on the page and the label on a solid brand fill pass AA", () => {
  for (const [name, b] of [["dark", dark], ["light", light]]) {
    assert.ok(ratio(tok(b, "accent"), tok(b, "bg")) >= 4.5, `${name} accent on bg`);
    assert.ok(ratio(tok(b, "on-solid"), tok(b, "accent")) >= 4.5, `${name} on-solid on accent`);
  }
});
test("gain and loss colours pass AA as text on the card in both themes", () => {
  for (const [name, b] of [["dark", dark], ["light", light]]) for (const c of ["buy", "sell"]) {
    const r = ratio(tok(b, c), tok(b, "panel")); assert.ok(r >= 4.5, `${name} ${c} ${r.toFixed(2)}`);
  }
});
test("the brand green is not the gain green (they stay tellable apart)", () => {
  assert.notDeepEqual(tok(light, "accent"), tok(light, "buy"));
  assert.notDeepEqual(tok(dark, "accent"), tok(dark, "buy"));
});
test("tokens.css adds no animation, and removes the title gradient and the hover lift", () => {
  assert.ok(!/@keyframes/.test(css) && [...css.matchAll(/animation\s*:\s*([^;}]+)/g)].every((m) => m[1].trim() === "none"), "no new animation");
  assert.ok(/\.page-title\s*\{[^}]*background:\s*none/.test(css));
  assert.ok(/--v5-grad:\s*linear-gradient\(rgb\(var\(--accent-rgb\)\), rgb\(var\(--accent-rgb\)\)\)/.test(css));
  assert.ok(/transform:\s*none/.test(css));
});
test("one font family is loaded: Inter (plus JetBrains Mono for logs)", () => {
  assert.ok(/family=Inter/.test(html) && /family=JetBrains\+Mono/.test(html));
  assert.ok(!/Jakarta|DM\+Sans/.test(html));
});
console.log(`tokens.test.mjs: ${n} ok`);
