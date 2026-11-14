// Round 8i: the old design layers are folded into styles/base.css + styles/base.css. Shared helpers for the CSS guard tests.
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

// Round 532: every animation is opt-in. animRules reads each `animation*` declaration with the @media chain around it.
import { readFileSync as _rf } from "node:fs";
const _strip = (t) => t.replace(/\/\*[\s\S]*?\*\//g, "");
function _parse(text) {
  const items = []; let i = 0;
  while (i < text.length) {
    while (i < text.length && /\s/.test(text[i])) i++;
    if (i >= text.length) break;
    const open = text.indexOf("{", i), semi = text.indexOf(";", i);
    if (text[i] === "@" && semi >= 0 && (open < 0 || semi < open)) { i = semi + 1; continue; }
    if (open < 0) break;
    const prelude = text.slice(i, open).trim(); let d = 1, j = open + 1;
    while (d > 0 && j < text.length) { if (text[j] === "{") d++; else if (text[j] === "}") d--; j++; }
    items.push({ prelude, body: text.slice(open + 1, j - 1) }); i = j;
  }
  return items;
}
/** every style rule with an animation declaration: [{ sel, decl, chain, none, guarded }]; guarded = inside prefers-reduced-motion: no-preference AND the selector carries :not(.tv):not(.anim-off) (or the declaration is `none`) */
export function animRules(css) {
  const out = [];
  const walk = (text, chain) => {
    for (const it of _parse(text)) {
      if (it.prelude.startsWith("@keyframes")) continue;
      if (it.prelude.startsWith("@")) { walk(it.body, [...chain, it.prelude]); continue; }
      const decls = it.body.split(";").map((d) => d.trim()).filter((d) => /^animation(-[a-z-]+)?\s*:/.test(d) && !/^animation-play-state/.test(d));
      if (!decls.length) continue;
      for (const sel of it.prelude.split(/,(?![^(]*\))/).map((s) => s.trim())) for (const decl of decls) {
        const none = /:\s*none\s*(!important)?$/.test(decl);
        const noPref = chain.some((c) => /prefers-reduced-motion:\s*no-preference/.test(c));
        const selGuard = /:not\(\.tv\)/.test(sel) && /:not\(\.anim-off\)/.test(sel);
        out.push({ sel, decl, chain, none, guarded: none || (noPref && selGuard) });
      }
    }
  };
  walk(_strip(css), []); return out;
}
/** main.tsx css imports, with pages.css expanded into the page sheets it @imports (cascade order) */
export function cascadeOrder() {
  const main = _rf(new URL("./main.tsx", import.meta.url), "utf8");
  const imports = [...main.matchAll(/import "\.\/([\w/-]+\.css)"/g)].map((m) => m[1]);
  return imports.flatMap((f) => f === "styles/pages.css" ? [...rdStyle("pages.css").matchAll(/@import "\.\/([\w-]+\.css)"/g)].map((m) => `styles/${m[1]}`) : [f]);
}
export const mainCssImports = () => [..._rf(new URL("./main.tsx", import.meta.url), "utf8").matchAll(/import "\.\/([\w/-]+\.css)"/g)].map((m) => m[1]);

const _unguard = (s) => s.replace(/^html:not\(\.tv\):not\(\.anim-off\)\s+/, "").replace(/^:where\(html:not\(\.tv\):not\(\.anim-off\)\)\s+/, "").replace(/^(html[^\s]*):where\(:not\(\.tv\):not\(\.anim-off\)\)/, "$1");
/** true when `sel` (or a state variant such as `sel.up`) animates and every animation declaration for it is opt-in */
export function optIn(css, sel) {
  const s0 = _unguard(sel);
  const r = animRules(css).filter((x) => { const u = _unguard(x.sel); return !x.none && (u === s0 || u.startsWith(s0 + ".") || u.startsWith(s0 + ":")); });
  return r.length > 0 && r.every((x) => x.guarded);
}
