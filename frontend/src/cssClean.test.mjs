// E546 (redesign round 8a): the old design layers stay free of dead rules. Plain node (>= 22.6): node --experimental-strip-types src/cssClean.test.mjs
// A style rule whose selector names a class that no component renders (and that no "prefix-${x}" pattern builds) can never match anything:
// the round 8 clean-up deleted 114 such classes. This test fails if one comes back, and ratchets the !important count and the size down.
import { test } from "node:test";
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { classIsLive } from "./cssLive.mjs";

const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "");
const OLD = ["styles/base.css"];

test("no old layer has a rule for a class that nothing renders", () => {
  const dead = [];
  for (const f of OLD) {
    const css = strip(rd(`./${f}`));
    for (const m of css.matchAll(/([^{}@;][^{}]*)\{[^{}]*\}/g)) {
      for (const sel of m[1].split(",")) {
        for (const c of sel.matchAll(/\.([a-zA-Z_][\w-]*)/g)) if (!classIsLive(c[1])) dead.push(`${f}: ${sel.trim().slice(0, 60)}`);
      }
    }
  }
  assert.deepEqual(dead.slice(0, 8), [], `${dead.length} dead selector(s), first shown`);
});
test("braces are balanced in every stylesheet", () => {
  for (const f of [...OLD, "styles/tokens.css", "styles/components.css", "styles/shell.css", "styles/home.css", "styles/trading.css", "styles/control.css", "styles/markets.css"]) {
    let d = 0;
    for (const ch of strip(rd(`./${f}`))) { if (ch === "{") d += 1; else if (ch === "}") d -= 1; assert.ok(d >= 0, `${f}: a } closes nothing`); }
    assert.equal(d, 0, `${f}: unbalanced braces`);
  }
});
test("ratchet: !important and root stylesheet size only go down (round 8i: 80 occurrences across all CSS and 1,801 lines in the two folded files; targets 15 and 2,000)", () => {
  const all = [...OLD, "styles/tokens.css", "styles/components.css", "styles/shell.css", "styles/home.css", "styles/trading.css", "styles/control.css", "styles/markets.css"].map((f) => strip(rd(`./${f}`)));
  const important = all.reduce((n, c) => n + (c.match(/!important/g) || []).length, 0);
  assert.ok(important <= 80, `!important is ${important}, the ratchet is 80`);
  const lines = OLD.map((f) => strip(rd(`./${f}`)).split("\n").length).reduce((a, b) => a + b, 0);
  assert.ok(lines <= 1810, `folded old layers are ${lines} lines, the ratchet is 1810`);
});
test("classIsLive: a literal class, a class built from a prefix pattern, and a dead class", () => {
  assert.ok(classIsLive("k-card") && classIsLive("anim-off") && classIsLive("tv"));
  assert.ok(classIsLive("signal-pill-buy"));
  assert.equal(classIsLive("dash-hero-top"), false);
  assert.equal(classIsLive("definitely-not-a-class-xyz"), false);
});
