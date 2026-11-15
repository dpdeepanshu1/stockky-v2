// round 550 (P1): the VIX rank stat on the F&O Chain tab. Pure words from the service's `vix_rank` key; no number is invented, a half-ready reply never reads as 0.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { vixRankView } from "./fnoChainView.ts";

const ok = (m) => console.log("ok - " + m);
const base = { vix: 17.5, rank: 50, percentile: 48, days: 25, need: 20, pending: 0, ready: true };

assert.deepEqual(vixRankView(base), { value: "50 / 100", sub: "VIX 17.50, above 48% of 25 days" });
assert.equal(vixRankView({ ...base, rank: 0, percentile: 0 }).value, "0 / 100", "a real rank of 0 is shown as 0, not as missing");
assert.equal(vixRankView({ ...base, rank: 99.6 }).value, "100 / 100");
assert.equal(vixRankView({ ...base, percentile: null }).sub, "VIX 17.50, 25 days");
ok("ready: the rank out of 100, the VIX and how many days it is measured against; a real 0 stays 0");

assert.deepEqual(vixRankView({ vix: 15, rank: null, ready: false, days: 12, need: 20, pending: 0 }), { value: "-", sub: "12 of 20 days recorded" });
assert.deepEqual(vixRankView({ vix: 15, rank: null, ready: false, days: 8, need: 20, pending: 17 }), { value: "-", sub: "reading history, 17 days left" });
assert.deepEqual(vixRankView({ vix: null, rank: null, ready: false, days: 25, need: 20, pending: 0 }), { value: "-", sub: "India VIX not available" });
assert.equal(vixRankView({ ready: true, rank: null, vix: 15, days: 30 }).value, "-", "ready without a number is not shown as a rank");
ok("not ready: a dash and the reason in words (too few days, still reading, no VIX)");

for (const bad of [null, undefined, 0, "x", []]) assert.equal(vixRankView(bad).value, "-");
assert.equal(vixRankView(undefined).sub, "not reported by this service");
assert.doesNotMatch(JSON.stringify(vixRankView({ vix: NaN, rank: NaN, percentile: Infinity, days: "x", ready: true })), /NaN|undefined|Infinity/);
ok("an older service (no key) or junk reads as a dash, never NaN or undefined");

const chain = readFileSync(new URL("./components/fno/FnoChain.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./fnoApi.ts", import.meta.url), "utf8");
const view = readFileSync(new URL("./fnoChainView.ts", import.meta.url), "utf8");
assert.match(chain, /vixRankView\(d\?\.vix_rank\)/);
assert.match(chain, /label="VIX rank" value=\{vr\.value\} sub=\{vr\.sub\}/);
assert.match(api, /vix_rank\?:/);
assert.ok(!/fetch\(|fnoApi|localStorage|sessionStorage/.test(view.slice(view.indexOf("round 550"))), "the helper makes no call and keeps nothing");
ok("wired: the Chain tab shows the stat from the same chain call, the API type has the optional key, the helper is pure");
console.log("vixRank.test.mjs: 3 ok");
