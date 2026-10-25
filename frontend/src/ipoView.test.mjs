// Run: node --experimental-strip-types src/ipoView.test.mjs
import assert from "node:assert/strict";
import { ipoKpis, ipoRowView } from "./components/ipoView.ts";
import { readFileSync } from "node:fs";

assert.deepEqual(ipoKpis([], 5), []);
assert.deepEqual(ipoKpis(null), []);
const rows = [
  { symbol: "A", stage: "upcoming", listing_date: "2026-11-05", decision: "HOLD" },
  { symbol: "B", stage: "pre_listing", listing_date: "2026-10-30" },
  { symbol: "C", stage: "listed", listing_pop_pct: 20, decision: "BUY NOW" },
  { symbol: "D", stage: "listing_day", listing_pop_pct: -10, decision: "PREPARE TO BUY" },
  { symbol: "E", stage: "no_data_yet" },
  { symbol: "F", stage: "error" },
];
const k = ipoKpis(rows, 40);
assert.equal(k[0].value, "6"); assert.equal(k[0].sub, "of 40 scanned");
assert.equal(k[1].value, "2"); assert.equal(k[1].sub, "next lists 2026-10-30");
assert.equal(k[2].value, "+5.0%"); assert.equal(k[2].tone, "up"); assert.equal(k[2].sub, "2 listed IPOs");
assert.equal(k[3].value, "2");
assert.equal(k[4].value, "2"); assert.equal(k[4].tone, "warn"); assert.match(k[4].sub, /data error/);
// direction is in the sign as well as the tone
assert.equal(ipoKpis([{ stage: "listed", listing_pop_pct: -3.25 }])[2].value, "-3.3%"); assert.equal(ipoKpis([{ stage: "listed", listing_pop_pct: -3.25 }])[2].tone, "down");
// a flat 0% listing is a reading
const z = ipoKpis([{ stage: "listed", listing_pop_pct: 0 }]);
assert.equal(z[2].value, "0.0%"); assert.equal(z[2].tone, "flat"); assert.equal(z[2].sub, "1 listed IPO");
// no gain figure -> dash, never 0
const n = ipoKpis([{ stage: "listed", listing_pop_pct: null }, { stage: "upcoming", listing_date: "soon" }]);
assert.equal(n[2].value, "—"); assert.equal(n[1].sub, "no listing date yet");
// every row fine -> no warning
const ok = ipoKpis([{ stage: "listed", listing_pop_pct: 1 }]);
assert.equal(ok[4].tone, "flat"); assert.equal(ok[0].sub, "in this window");
for (const x of k) assert.equal(typeof x.value, "string");
// E162: row face - dashes for missing, a real 0 stays 0, words and glyphs carry the meaning
const up = ipoRowView({ stage: "upcoming", listing_date: "2026-11-03T00:00:00", issue_price: 120, gmp: 18, gmp_pct_of_issue: 15, ipo_score: 66.4 });
assert.deepEqual(up.stage, { text: "Lists 2026-11-03", tone: "flat" }); assert.equal(up.issue, "₹120"); assert.equal(up.cmp, null); assert.equal(up.gain, null);
assert.deepEqual(up.gmp, { text: "GMP ₹18 (+15%)", dir: "up" }); assert.deepEqual(up.score, { text: "66/100", word: "Good", tone: "acc" });
const bare = ipoRowView({ stage: "upcoming" });
assert.equal(bare.stage.text, "Listing date not set"); assert.equal(bare.issue, "—");
for (const bad of [bare.stage.text, bare.issue]) assert.ok(!/undefined|NaN|null/.test(bad));
assert.equal(ipoRowView({ stage: "listed" }).stage.text, "Listed"); assert.equal(ipoRowView({ stage: "listed", days_since_listing: 0 }).stage.text, "Day 0");
assert.equal(ipoRowView({ stage: "listed", days_since_listing: 12 }).stage.text, "Day 12");
assert.deepEqual(ipoRowView({ stage: "listing_day" }).stage, { text: "Listing day · live", tone: "up" });
assert.deepEqual(ipoRowView({ stage: "pre_listing" }).stage.tone, "warn"); assert.equal(ipoRowView({ stage: "error" }).stage.tone, "down");
assert.equal(ipoRowView({ stage: "no_data_yet" }).stage.tone, "warn"); assert.equal(ipoRowView({ stage: "weird" }).stage.text, "weird"); assert.equal(ipoRowView({}).stage.text, "—");
// 0 gain is a reading; 0 price is "no price"
assert.deepEqual(ipoRowView({ current_vs_issue_pct: 0 }).gain, { text: "0.0%", dir: "flat" });
assert.deepEqual(ipoRowView({ current_vs_issue_pct: -3.26 }).gain, { text: "-3.3%", dir: "down" }); assert.deepEqual(ipoRowView({ current_vs_issue_pct: 8 }).gain, { text: "+8.0%", dir: "up" });
assert.equal(ipoRowView({ current_price: 0 }).cmp, null); assert.equal(ipoRowView({ current_price: 141.5 }).cmp, "₹141.5");
assert.equal(ipoRowView({ ipo_score: 0 }).score.word, "Weak"); assert.equal(ipoRowView({ ipo_score: 0 }).score.text, "0/100"); assert.equal(ipoRowView({ ipo_score: null }).score, null);
// GMP is hidden once the stock trades; negative GMP spelled with a sign and a down direction; GMP 0 is flat
assert.equal(ipoRowView({ stage: "listed", gmp: 10 }).gmp, null);
assert.deepEqual(ipoRowView({ stage: "pre_listing", gmp: -4, gmp_pct_of_issue: -3.4 }).gmp, { text: "GMP ₹-4 (-3%)", dir: "down" });
assert.deepEqual(ipoRowView({ stage: "upcoming", gmp: 0 }).gmp, { text: "GMP ₹0", dir: "flat" }); assert.equal(ipoRowView({ stage: "upcoming", gmp: null }).gmp, null);
// source guards
const tsx = readFileSync(new URL("./components/IpoTracker.tsx", import.meta.url), "utf8");
const css = readFileSync(new URL("./design-v7.css", import.meta.url), "utf8");
assert.ok(/ipoRowView\(ipo\)/.test(tsx) && !/Issue ₹\{ipo\.issue_price\}/.test(tsx) && !/Lists \$\{ipo\.listing_date\}/.test(tsx), "row face reads the row view");
assert.ok(/v7-ipo-row/.test(tsx) && /aria-label=\{`Score /.test(tsx) && /Hide detail/.test(tsx) && /Scan for Buy/.test(tsx), "state edge, spoken score label, detail toggle and buy button stay");
assert.ok(!/animation\s*:/.test(css.slice(css.indexOf("E162"))), "E162 CSS adds no animation");
console.log("ipoView ok");
