// round 551 (P2): the sector rail's traded-value share and up / down count. Pure words; a missing figure is a dash, never 0.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { inrShort, sectorTradedView } from "./sectorTraded.ts";

const ok = (m) => console.log("ok - " + m);

assert.equal(inrShort(1.234e10), "₹1,234 Cr");
assert.equal(inrShort(4.5e6), "₹45 L");
assert.equal(inrShort(0), "₹0 L", "a real zero is shown as zero");
for (const bad of [null, undefined, NaN, Infinity, -5, "x"]) assert.equal(inrShort(bad), "-");
ok("rupees in crore / lakh; missing or junk is a dash");

const full = sectorTradedView({ name: "Banks", turnover: 2.5e10, turnover_share_pct: 12.34, up: 5, down: 3 });
assert.deepEqual([full.share, full.value, full.updown], ["12.3%", "₹2,500 Cr", "5▲ 3▼"]);
assert.match(full.title, /Banks: traded value ₹2,500 Cr, 12\.3% of the tracked stocks' traded value, 5 up, 3 down\. Traded value is not net buying\./);
assert.equal(sectorTradedView({ name: "X", turnover_share_pct: 0, up: 0, down: 0 }).share, "0.0%", "a real 0 share stays 0.0%");
ok("share, value and up / down in words, with the 'not net buying' note in the tooltip");

const none = sectorTradedView({ name: "Old" });                           // an older service: none of the new keys
assert.deepEqual([none.share, none.value, none.updown], ["-", "-", "-"]);
assert.match(none.title, /up \/ down not reported/);
const nul = sectorTradedView({ name: "Z", turnover: null, turnover_share_pct: null, up: 2, down: 1 });
assert.deepEqual([nul.share, nul.value, nul.updown], ["-", "-", "2▲ 1▼"]);
assert.doesNotMatch(JSON.stringify(sectorTradedView({ name: "J", turnover: NaN, turnover_share_pct: NaN, up: NaN, down: "x" })), /NaN|undefined|Infinity|null/);
ok("no data: dashes, never 0, NaN or undefined");

const rail = readFileSync(new URL("./SectorRail.tsx", import.meta.url), "utf8");
const types = readFileSync(new URL("./boardTypes.ts", import.meta.url), "utf8");
const css = readFileSync(new URL("./board.css", import.meta.url), "utf8");
assert.match(rail, /sectorTradedView\(s\)/);
assert.match(rail, /title=\{tv\.title\}/);
assert.match(rail, /className="tv"/);
assert.match(types, /turnover_share_pct\?: number \| null/);
assert.match(css, /\.ob-rail \.tv \{/);
assert.match(css, /grid-template-columns: minmax\(0, 1fr\) auto 46px auto;/);
assert.ok(!/fetch\(|localStorage|sessionStorage/.test(readFileSync(new URL("./sectorTraded.ts", import.meta.url), "utf8")), "the helper makes no call and keeps nothing");
ok("wired: the rail shows the share beside the % change, the types are optional, the helper is pure");
console.log("sectorTraded.test.mjs: 4 ok");
