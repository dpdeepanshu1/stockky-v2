// E534: guards for the component kit (run: node --experimental-strip-types src/kit.test.mjs). Static checks on source and CSS, no browser needed.
import { readFileSync, existsSync } from "node:fs";
import { cascadeOrder } from "./cssText.mjs";
import assert from "node:assert/strict";
const rd = (f) => readFileSync(new URL(f, import.meta.url), "utf8");
const css = rd("./styles/components.css").replace(/\/\*[\s\S]*?\*\//g, "");
const main = rd("./main.tsx");
const index = rd("./components/kit/index.ts");
let n = 0; const test = (name, f) => { f(); n++; console.log("ok -", name); };

const FILES = ["Card", "Stat", "StatusPill", "Tabs", "Button", "Chip", "Field", "DataTable", "Sheet", "Skeleton", "KitPreview"];
test("every kit component file exists and the index exports the whole kit", () => {
  for (const f of FILES) assert.ok(existsSync(new URL(`./components/kit/${f}.tsx`, import.meta.url)), `${f}.tsx missing`);
  for (const name of ["Card", "CardHeader", "Stat", "Stats", "StatusPill", "Tabs", "Button", "Chip", "ChipList", "Field", "Switch", "DataTable", "Sheet", "Skeleton", "SkeletonLines", "Empty", "ErrorState"]) assert.ok(new RegExp(`\\b${name}\\b`).test(index), `index does not export ${name}`);
});
test("components.css loads after tokens.css; only the shell (E535), home (E536), trading (E537) and control (E539) styles come after it", () => {
  const imports = cascadeOrder();   // E558: base, tokens, components, then the page sheets
  assert.deepEqual(imports.slice(-6), ["styles/components.css", "styles/shell.css", "styles/home.css", "styles/trading.css", "styles/control.css", "styles/markets.css"]);
  assert.ok(imports.indexOf("styles/components.css") > imports.indexOf("styles/tokens.css"));
});
test("every class the kit styles starts with k- (it cannot clash with the old layers)", () => {
  const bad = [...css.matchAll(/(?:^|[},\s])\.([a-z][\w-]*)/gm)].map((m) => m[1]).filter((c) => !c.startsWith("k-") && !["light", "tv", "anim-off", "up", "down", "warn", "sm", "lg", "md", "block", "flush", "quiet", "invalid", "live", "armed", "paused", "offline", "closed", "info", "primary", "soft", "ghost", "danger", "asking", "click", "dense", "ready", "r", "tint-warn", "tint-sell", "ok", "bad"].includes(c));
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
test("F1: one kit Chip replaces the three look-alike chips; a toned chip always carries a symbol and the tone never colours the text", () => {
  const chip = rd("./components/kit/Chip.tsx");
  assert.ok(/aria-hidden="true"/.test(chip) && /k-chip-sym/.test(chip), "the symbol is a hidden glyph beside the words");
  assert.ok(/\.k-chip \{[^}]*color:\s*rgb\(var\(--fg-rgb\)\)/.test(css), "chip text stays the normal text colour (tone is on the symbol, border and fill)");
  assert.ok(/\.k-chip-sym \{[^}]*color:\s*rgb\(var\(--cc\)\)/.test(css));
  const base = rd("./styles/base.css");
  for (const old of ["w-money-chip", "w-pulse-chip", "w-expo-chip"]) assert.ok(!base.includes(old), `${old} rules are gone from base.css`);
  for (const f of ["MoneyModeBanner", "AiPulse", "ExposureStrip"]) {
    const t = rd(`./components/ui/${f}.tsx`);
    assert.ok(/<ChipList>/.test(t) && /<Chip\b/.test(t) && !/w-(money|pulse|expo)-chip/.test(t), `${f} uses the kit chip`);
  }
  assert.ok(/tone=\{x\.state\} symbol=\{SYMBOL\[x\.state\]\}/.test(rd("./components/ui/MoneyModeBanner.tsx")) && /symbol=\{SYMBOL\[c\.state\]\}/.test(rd("./components/ui/AiPulse.tsx")), "state chips pass their symbol");
  const ex = rd("./components/ui/ExposureStrip.tsx");
  assert.ok(/symbol=\{pnlTone === "up" \? "▲" : pnlTone === "down" \? "▼" : undefined\}/.test(ex) && /tone="warn" symbol="⚠"/.test(ex), "P&L and near-stop chips carry an arrow / warning mark");
  assert.ok(/<Chip as="span">Risk : reward/.test(rd("./components/ui/TradePlanBar.tsx")), "the plan bar's risk : reward is a chip");
});
test("F1: panels with a plain card surface sit on the kit Card (no second border / padding), and EmptyState is the kit Empty with an icon", () => {
  assert.ok(/<Card className="w-expo"/.test(rd("./components/ui/ExposureStrip.tsx")) && /<Card quiet className=\{`w-day/.test(rd("./components/session/SessionRail.tsx")));
  const base = rd("./styles/base.css");
  for (const sel of [".w-expo {", ".w-day {"]) {
    const rule = base.split("\n").find((l) => l.startsWith(sel)) || "";
    assert.ok(rule && !/(border|background|padding|box-shadow)/.test(rule), `${sel} must not repeat the Card surface`);
  }
  const es = rd("./components/ui/EmptyState.tsx");
  assert.ok(/from "\.\.\/kit\/Skeleton"/.test(es) && /<Empty\b/.test(es) && !/k-empty/.test(es), "EmptyState wraps the kit Empty");
  assert.ok(/icon\?: ReactNode/.test(rd("./components/kit/Skeleton.tsx")) && /\.k-empty-icon \{/.test(css) && !/\.k-empty-icon/.test(base), "the empty icon rules live in the kit");
});
console.log(`kit.test.mjs: ${n} ok`);
