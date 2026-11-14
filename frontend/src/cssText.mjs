// Round 8i: the old design layers are folded into styles/base.css + styles/legacy.css. Shared helpers for the CSS guard tests.
import { readFileSync } from "node:fs";
export const rdStyle = (name) => readFileSync(new URL(`./styles/${name}`, import.meta.url), "utf8");
export const stripComments = (t) => t.replace(/\/\*[\s\S]*?\*\//g, "");
/** the text inside every `@media (prefers-reduced-motion ...) { ... }` block */
export function reducedMotionText(css) {
  const c = stripComments(css); let out = ""; let from = 0;
  for (;;) {
    const i = c.indexOf("@media (prefers-reduced-motion", from); if (i < 0) break;
    const open = c.indexOf("{", i); let d = 0, j = open;
    for (; j < c.length; j++) { if (c[j] === "{") d++; else if (c[j] === "}") { d--; if (d === 0) break; } }
    out += c.slice(open + 1, j) + "\n"; from = j + 1;
  }
  return out;
}
/** every @keyframes block whose name starts with `prefix`: [{ name, body }] (brace-matched, so it never runs on into the next rule) */
export function keyframesOf(css, prefix = "") {
  const c = stripComments(css), out = [];
  for (const m of c.matchAll(/@keyframes\s+([\w-]+)\s*\{/g)) {
    if (!m[1].startsWith(prefix)) continue;
    let d = 1, i = m.index + m[0].length;
    while (d > 0 && i < c.length) { if (c[i] === "{") d++; else if (c[i] === "}") d--; i++; }
    out.push({ name: m[1], body: c.slice(m.index + m[0].length, i - 1) });
  }
  return out;
}
