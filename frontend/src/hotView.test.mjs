// Run: node --experimental-strip-types src/hotView.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { hotKpis, hotAge } from "./components/hotView.ts";

assert.deepEqual(hotKpis(null), []);
const live = hotKpis({ news_driven: [{ symbol: "AAA", score: 61 }], results_driven: [{ symbol: "BBB", combined_score: 72.4 }], bulk_insider_driven: [{ symbol: "CCC", score: 0 }], universe_size: 480, scan_seed_count: 60 });
assert.equal(live[0].value, "3"); assert.match(live[0].sub, /news 1/);
assert.equal(live[1].value, "72"); assert.equal(live[1].sub, "BBB");
assert.equal(live[2].value, "Live scan"); assert.equal(live[2].tone, "info");
assert.equal(live[3].value, "480"); assert.equal(live[3].sub, "scan seeds 60");
// a real 0 is a score, not "missing" (F-11 lesson)
const zero = hotKpis({ news_driven: [{ symbol: "ZZZ", score: 0 }] });
assert.equal(zero[1].value, "0"); assert.equal(zero[1].sub, "ZZZ");
// nothing scored -> dash, never 0
const none = hotKpis({ news_driven: [{ symbol: "N", score: null }] });
assert.equal(none[1].value, "—"); assert.equal(none[1].sub, "no scored pick yet");
// stored + stale is a warning that is also spelled in words
const st = hotKpis({ source: "hotpicks_static_feed", fresh: false, age_hours: 30, hours: 24, count: 5 });
assert.equal(st[2].value, "Stored feed"); assert.equal(st[2].tone, "warn"); assert.match(st[2].sub, /Stale/); assert.match(st[2].sub, /1\.3d ago/);
const fresh = hotKpis({ source: "hotpicks_static_feed", fresh: true, age_hours: 0.5, hours: 24 });
assert.equal(fresh[2].tone, "info"); assert.match(fresh[2].sub, /30m ago/);
// partial scan
const part = hotKpis({ stopped_early: true, processed_symbols: 12, universe_size: 400 });
assert.equal(part[3].tone, "warn"); assert.match(part[3].sub, /Partial - stopped after 12/);
// missing universe -> dash; NaN age -> words
assert.equal(hotKpis({})[3].value, "—"); assert.equal(hotAge(NaN), "age unknown"); assert.equal(hotAge(undefined), "age unknown");
// money rule: the strip holds text only (no numbers that animate); labels are plain words
for (const k of live) assert.equal(typeof k.value, "string");

// E160: card body guards (source level, no browser)
const cc = readFileSync(new URL("./components/ConvictionCard.tsx", import.meta.url), "utf8");
const css7 = readFileSync(new URL("./design-v7.css", import.meta.url), "utf8");
assert.ok(/import \{ ScoreRing \} from "\.\/ui\/ScoreRing"/.test(cc) && /import \{ TradePlanBar \} from "\.\/ui\/TradePlanBar"/.test(cc), "card uses the shared ring and plan bar");
assert.ok(/Number\.isFinite\(Number\(score\)\)/.test(cc) && !/score\s*\?\s*<ScoreRing/.test(cc), "a score of 0 still gets a ring (finite test, not truthiness)");
assert.ok(/decision === "BUY NOW" \|\| decision === "PREPARE TO BUY"/.test(cc), "buy zone only for a buy-side call");
assert.ok(/!compact && scoreNum != null/.test(cc) && /\{!showRing && \(/.test(cc), "compact cards and cards without a score keep the plain Score tile");
assert.ok(cc.includes(">Stop loss<") && cc.includes(">Target<") && cc.includes(">Entry<") && cc.includes(">Horizon<"), "the text levels stay (words for the drawing)");
assert.ok(!/PriceRoll|GlintBox|useTickGlint|requestAnimationFrame|countUp|setInterval|setTimeout|fetch\(/i.test(cc), "card stays display-only, no animated numbers");
assert.ok(/\.cc-v7-hero\s*\{/.test(css7) && !/E160[\s\S]*animation\s*:/.test(css7.slice(css7.indexOf("E160"))), "layout rule exists and adds no animation");
console.log("hotView ok");
