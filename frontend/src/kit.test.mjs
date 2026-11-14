// E534: guards for the component kit (run: node --experimental-strip-types src/kit.test.mjs). Static checks on source and CSS, no browser needed.
import { readFileSync, existsSync } from "node:fs";
import { cascadeOrder } from "./cssText.mjs";
import assert from "node:assert/strict";
const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const css = rd("./styles/components.css").replace(/\/\*[\s\S]*?\*\//g, "");
const main = rd("./main.tsx");
const index = rd("./components/kit/index.ts");
let n = 0; const test = (name, f) => { f(); n++; console.log("ok -", name); };

const FILES = ["Card", "Stat", "StatusPill", "Tabs", "Button", "Field", "DataTable", "Sheet", "Skeleton", "KitPreview"];
test("every kit component file exists and the index exports the whole kit", () => {
  for (const f of FILES) assert.ok(existsSync(new URL(`./components/kit/${f}.tsx`, import.meta.url)), `${f}.tsx missing`);
  for (const name of ["Card", "CardHeader", "Stat", "Stats", "StatusPill", "Tabs", "Button", "Field", "Switch", "DataTable", "Sheet", "Skeleton", "SkeletonLines", "Empty", "ErrorState"]) assert.ok(new RegExp(`\\b${name}\\b`).test(index), `index does not export ${name}`);
});
test("components.css loads after tokens.css; only the shell (E535), home (E536), trading (E537) and control (E539) styles come after it", () => {
  const imports = cascadeOrder();   // round 532: base, tokens, components, then pages.css expanded
  assert.deepEqual(imports.slice(-6), ["styles/components.css", "styles/shell.css", "styles/home.css", "styles/trading.css", "styles/control.css", "styles/markets.css"]);
  assert.ok(imports.indexOf("styles/components.css") > imports.indexOf("styles/tokens.css"));
});
test("every class the kit styles starts with k- (it cannot clash with the old layers)", () => {
  const bad = [...css.matchAll(/(?:^|[},\s])\.([a-z][\w-]*)/gm)].map((m) => m[1]).filter((c) => !c.startsWith("k-") && !["light", "tv", "anim-off", "up", "down", "warn", "sm", "lg", "md", "block", "flush", "quiet", "invalid", "live", "armed", "paused", "offline", "closed", "info", "primary", "soft", "ghost", "danger", "asking", "click", "dense", "ready", "r", "tint-warn", "tint-sell"].includes(c));
  assert.deepEqual([...new Set(bad)], []);
});
test("every animation and transition sits behind reduced-motion, TV and anim-off; keyframes use transform / opacity only", () => {
  const motion = css.slice(css.indexOf("@media (prefers-reduced-motion: no-preference)"));
  const end = motion.indexOf("\n}\n");
  const guarded = motion.slice(0, end);
  const before = css.slice(0, css.indexOf("@media (prefers-reduced-motion: no-preference)"));
  assert.ok(!/\b(animation|transition)\s*:/.test(before), "an animation or transition is declared outside the guarded block");
  for (const [, sel] of [...guarded.matchAll(/([^{}]+)\{[^{}]*\b(?:animation|transition)\s*:[^{}]*\}/g)]) {
    for (const one of sel.split(",").map((x) => x.trim()).filter(Boolean)) assert.ok(one.startsWith("html:not(.tv):not(.anim-off)"), `"${one}" has no TV / anim-off off switch`);
  }
  for (const [, name, inner] of [...css.matchAll(/@keyframes\s+([\w-]+)\s*\{([\s\S]*?\})\s*\}/g)]) for (const [, p] of inner.matchAll(/([a-z-]+)\s*:/g)) assert.ok(p === "transform" || p === "opacity", `${name} animates ${p}`);
  assert.ok(!/animation[^;{}]*\b(both|forwards)\b/.test(css), "use backwards, not both/forwards");
  assert.ok(css.includes('html[data-hidden="true"] .k-skel::after'), "looping shimmer pauses on a hidden tab");
});
test("a state is never colour-only: pills have their own dot shapes, stats carry an arrow, errors carry a mark", () => {
  for (const s of ["paused", "offline", "closed"]) assert.ok(new RegExp(`\\.k-pill\\.${s} i\\s*\\{`).test(css), `pill ${s} has no shape rule`);
  assert.ok(/ARROW[\s\S]*▲[\s\S]*▼/.test(rd("./components/kit/Stat.tsx")));
  assert.ok(css.includes('.k-field-error::before { content: "✕ "') && css.includes('.k-error-title::before { content: "⚠ "'));
});
test("accessibility wiring: tablist + arrow keys, switch role, dialog + Escape + focus return, labelled fields", () => {
  const tabs = rd("./components/kit/Tabs.tsx"), sheet = rd("./components/kit/Sheet.tsx"), field = rd("./components/kit/Field.tsx");
  assert.ok(/role="tablist"/.test(tabs) && /role="tab"/.test(tabs) && /ArrowRight/.test(tabs) && /ArrowLeft/.test(tabs) && /"Home"/.test(tabs) && /"End"/.test(tabs) && /tabIndex=\{value === t\.id \? 0 : -1\}/.test(tabs));
  assert.ok(/role="switch"/.test(field) && /aria-checked/.test(field) && /htmlFor=\{id\}/.test(field) && /role="alert"/.test(field));
  assert.ok(/role="dialog"/.test(sheet) && /aria-modal="true"/.test(sheet) && /Escape/.test(sheet) && /createPortal/.test(sheet) && /opener\?\.focus/.test(sheet));
});
test("danger confirm: the first click only asks, the second click inside 3 s acts", () => {
  const b = rd("./components/kit/Button.tsx");
  assert.ok(/confirmText && !asking/.test(b) && /setTimeout\(\(\) => setAsking\(false\), 3000\)/.test(b) && /onClick\?\.\(e\)/.test(b));
  assert.ok(b.indexOf("return;") < b.indexOf("onClick?.(e)"), "the asking branch returns before onClick");
});
test("inputs are 16px (no iOS zoom) and touch targets reach 44px on touch screens", () => {
  assert.ok(/\.k-field input[^{]*\{[^}]*font-size:\s*16px/.test(css));
  assert.ok(/\(pointer: coarse\)\s*\{[^}]*min-height:\s*44px/.test(css));
});
test("the preview page is opt-in (?kit=1), lazy, and calls no service", () => {
  assert.ok(/get\("kit"\) === "1"/.test(main) && /lazy\(\(\) => import\("\.\/components\/kit\/KitPreview"\)\)/.test(main));
  const p = rd("./components/kit/KitPreview.tsx");
  assert.ok(!/fetch\(|from "\.\.\/\.\.\/api"|from "\.\.\/\.\.\/realTradeApi"/.test(p));
});
console.log(`kit.test.mjs: ${n} ok`);
