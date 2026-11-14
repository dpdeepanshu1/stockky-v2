// E546 (redesign round 8): helper for the old design-layer tests. A selector only needs an animation off switch while a component can still put its
// classes on screen. When a class is not in any .tsx / .ts / index.html (and is not built from a "prefix-${x}" pattern), its rules were deleted with the
// round 8 clean-up, so the hard-coded lists in design-v5 / v6 / v7 tests keep only the selectors that are still live.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const root = new URL(".", import.meta.url).pathname;
function* walk(dir) {
  for (const f of readdirSync(dir)) {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) yield* walk(p);
    else if (p.endsWith(".tsx") || p.endsWith(".ts")) yield p;
  }
}
let code = "";
for (const p of walk(root)) code += readFileSync(p, "utf8") + "\n";
try { code += readFileSync(new URL("../index.html", import.meta.url), "utf8"); } catch { /* no index.html: only the sources count */ }

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
export function classIsLive(c) {
  if (/^\d+$/.test(c) || new RegExp(`(?<![\\w-])${esc(c)}(?![\\w-])`).test(code)) return true;   // E550: whole-word match, so "mk-hot-grid" no longer keeps ".hot-grid" alive
  if (!c.includes("-")) return false;
  const pre = c.slice(0, c.lastIndexOf("-") + 1);
  return new RegExp(`${esc(pre)}\\$\\{`).test(code) || new RegExp(`${esc(pre)}["'\`]\\s*\\+`).test(code);
}
/** keep the selectors whose every class is still live */
export function liveSelectors(list) {
  return list.filter((sel) => [...sel.matchAll(/\.([a-zA-Z_][\w-]*)/g)].every((m) => classIsLive(m[1])));
}
