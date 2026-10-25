// E150 (F-18): one label for the stop-loss price and the target price on every screen. Run: node --experimental-strip-types src/labels.test.mjs
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

function walk(dir, out = []) {
  for (const n of readdirSync(dir)) {
    const p = join(dir, n);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(tsx|ts)$/.test(n) && !/\.test\./.test(n)) out.push(p);
  }
  return out;
}
const files = walk(new URL("./", import.meta.url).pathname);
const src = files.map((f) => [f, readFileSync(f, "utf8")]);
const hits = (re) => src.flatMap(([f, t]) => t.split("\n").map((l, i) => [f, i + 1, l]).filter(([, , l]) => !/^\s*(\/\/|\*)/.test(l) && re.test(l)));

// the price label is "Stop loss" (lower-case l), never "Stop Loss", and never a bare "Stop" label next to a price
assert.deepEqual(hits(/>\s*Stop Loss\s*</).map(([f, i]) => `${f}:${i}`), [], '"Stop Loss" (capital L) found');
assert.deepEqual(hits(/Stop Loss \(/).map(([f, i]) => `${f}:${i}`), [], '"Stop Loss (" found');
const bareStop = hits(/(<(?:span|div|dt|label|small)[^>]*>\s*Stop\s*<\/(?:span|div|dt|label|small)>)|label:\s*"Stop"|add\("Stop"|\["Stop",/).map(([f, i, l]) => `${f}:${i} ${l.trim().slice(0, 80)}`);
assert.deepEqual(bareStop, [], "bare Stop label next to a price: " + bareStop.join(" | "));
// the screens fixed in E150 now say it
for (const [file, needle] of [["components/ConvictionCard.tsx", ">Stop loss<"], ["components/SignalStream.tsx", "<label>Stop loss</label>"], ["components/board/TopPicks.tsx", "<dt>Stop loss</dt>"],
  ["components/realAuto/format.ts", 'add("Stop loss"'], ["components/trading/ManualTradeTicket.tsx", "Stop loss (₹, optional)"], ["components/Trades.tsx", ">Stop loss<"]]) {
  assert.ok(readFileSync(new URL(file, import.meta.url), "utf8").includes(needle), `${file} should contain ${needle}`);
}
console.log("labels ok");
