// Run: node --experimental-strip-types tapeSymbol.test.mjs  (Node >= 22.6). E271: per-symbol helpers of the "Order-flow tape" card (tapeSymbolView.ts) and wiring.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { symbolTapeView, sideOf, CHECK_ORDER } from "./tapeSymbolView.ts";

const doc = (o = {}) => ({
  symbol: "SBIN", available: true, reliable: true, reason: null, coverage_pct: 48.2, intervals: 210, tape_minutes: 31.5, price: 812.4, imbalance_5: -0.35,
  profile: { poc: 808, vah: 811, val: 804, location: "above_value", node: null, hvn_above: 815.5, hvn_above_pct: 0.38, hvn_below: 808, hvn_below_pct: 0.54 },
  signals: { buy_pct_10m: 41, delta_agrees: false, price_move_20m_pct: 0.4, absorption: "buying_absorbed", exhaustion: null, aggressor: "sellers",
    big_prints: { buy: 0, sell: 2 }, stacked: { buy: 0, sell: 3 } },
  checklist: {
    long: { checks: { profile: "for", nodes: "neutral", footprint: "against", imbalance: "against", delta: "against", big_prints: "against" }, against: 4, for_count: 1, against_names: ["footprint", "imbalance", "delta", "big_prints"] },
    short: { checks: { profile: "against", nodes: "neutral", footprint: "for", imbalance: "for", delta: "for", big_prints: "for" }, against: 1, for_count: 4, against_names: ["profile"] },
  }, ...o,
});

// the six rows are the six items of the order-flow checklist, in order
assert.deepEqual(CHECK_ORDER.map((c) => c.key), ["profile", "nodes", "footprint", "imbalance", "delta", "big_prints"]);

// long: 4 against -> bad
const l = symbolTapeView(doc(), "LONG");
assert.equal(l.kind, "ok");
assert.equal(l.tone, "bad");
assert.equal(l.against, 4);
assert.equal(l.forCount, 1);
assert.deepEqual(l.rows.map((r) => r.state), ["for", "neutral", "against", "against", "against", "against"]);
assert.match(l.headline, /4 of 6 checks against a long, 1 for\./);
assert.match(l.rows[0].detail, /above value/);
assert.match(l.rows[0].detail, /VAL 804\.00 to VAH 811\.00, POC 808\.00/);
assert.match(l.rows[1].detail, /nearest HVN above 815\.50 \(0\.38%\), below 808\.00 \(0\.54%\)/);
assert.match(l.rows[2].detail, /aggressor: sellers; stacked imbalances buy 0 \/ sell 3/);
assert.match(l.rows[3].detail, /absorption buying_absorbed; exhaustion none/);
assert.match(l.rows[4].detail, /buy share \(10 min\) 41%; delta disagrees with the 20 min move \(0\.4%\)/);
assert.match(l.rows[5].detail, /buy 0 \/ sell 2/);
assert.equal(l.levels.vah, "811.00");
assert.equal(l.levels.hvnAbove, "815.50");
assert.ok(l.facts.join(" ").includes("coverage 48.2%"));

// short: 1 against -> warn; 0 against -> good
const s = symbolTapeView(doc(), "SHORT");
assert.equal(s.tone, "warn");
assert.match(s.headline, /1 of 6 checks against a short, 4 for\./);
const clean = doc({ checklist: { long: { checks: {}, against: 0, for_count: 0 }, short: { checks: {}, against: 0, for_count: 0 } } });
assert.equal(symbolTapeView(clean, "LONG").tone, "good");
assert.ok(symbolTapeView(clean, "LONG").rows.every((r) => r.state === "neutral"), "missing checks read as neutral");

// unreliable tape: shown, never a hard tone
const u = symbolTapeView(doc({ reliable: false, reason: "tape_too_young" }), "LONG");
assert.equal(u.kind, "unreliable");
assert.equal(u.tone, "warn");
assert.match(u.headline, /Tape not reliable yet \(tape_too_young\); the gate gives no opinion\./);

// no tape / garbage never throws
for (const bad of [null, undefined, {}, { available: false, reason: "no_tape" }, { available: false, reason: "market_data_ConnectError" }, "x", 5]) {
  const v = symbolTapeView(bad, "LONG");
  assert.equal(v.kind, "none");
  assert.equal(v.rows.length, 0);
}
assert.equal(symbolTapeView({ available: false, reason: "no_tape" }, "LONG").headline, "No tape for this symbol yet.");
assert.match(symbolTapeView({ available: false, reason: "market_data_ConnectError" }, "LONG").headline, /market_data_ConnectError/);
assert.equal(symbolTapeView(doc({ price: undefined, profile: null, signals: null }), "LONG").levels.price, "-");

// side helper
assert.equal(sideOf("short"), "SHORT");
assert.equal(sideOf("SHORT"), "SHORT");
assert.equal(sideOf(undefined), "LONG");
assert.equal(sideOf("LONG"), "LONG");

// wiring
const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
assert.match(rd("./components/calibration/TapePanel.tsx"), /<TapeSymbolCard svc=\{svc\} \/>/);
const card = rd("./components/calibration/TapeSymbolCard.tsx");
assert.match(card, /positionStocksApi\.flowSymbol\(symbol\)/);
assert.match(card, /realTradeApi\.flowSymbol\(symbol\)/);
assert.match(card, /setVisibleInterval/);
assert.match(card, /svc === "ps" && <Btn/, "the short toggle only exists where shorts exist");
assert.ok(!/method:\s*"POST"|controlApi\.set/.test(card), "the card never changes anything");
assert.match(rd("./realTradeApi.ts"), /flowSymbol: \(symbol: string\) => rtRequest<SymbolTapeDoc>\(`\/flow\/symbol\/\$\{encodeURIComponent\(symbol\.trim\(\)\.toUpperCase\(\)\)\}`, undefined, true\)/);
assert.match(rd("./positionStocksApi.ts"), /flowSymbol: \(symbol: string\) => psRequest<SymbolTapeDoc>\(`\/flow\/symbol\/\$\{encodeURIComponent\(symbol\.trim\(\)\.toUpperCase\(\)\)\}`, undefined, true\)/);

console.log("tapeSymbol.test.mjs ok");
