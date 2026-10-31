// Run: node --experimental-strip-types tape.test.mjs  (Node >= 22.6). E271: helpers of the "Order-flow tape" card (tapeView.ts) and its wiring.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { tapeView, MIN_COVERAGE_PCT } from "./tapeView.ts";

const gate = (o = {}) => ({ calls: 40, blocks: 6, unreliable: 2, no_tape: 1, errors: 0, cache_hits: 10, mode: "live",
  exit: { calls: 30, tighten: 4, hold: 26, errors: 0, applied: 3, exit_mode: "live", ...(o.exit || {}) }, ...o, exit: { calls: 30, tighten: 4, hold: 26, errors: 0, applied: 3, exit_mode: "live", ...(o.exit || {}) } });
const tape = (o = {}) => ({ enabled: true, symbols: 120, symbols_today: 100, reliable: true, reliable_symbols: 80, avg_coverage_pct: 52.5, errors: 0, ...o });
const doc = (o = {}) => ({ since_restart: true, service: "position-stocks", gate: gate(), tape: tape(), min_against: 2, exit_min_against: 3, ...o });

// unavailable
assert.equal(tapeView(null).kind, "unavailable");
assert.equal(tapeView(undefined).tone, "warn");
assert.ok(["ok", "off", "warming", "unavailable"].includes(tapeView({}).kind));   // an empty object never throws

// healthy
const ok = tapeView(doc());
assert.equal(ok.kind, "ok");
assert.equal(ok.tone, "good");
assert.equal(ok.entry.blockPct, 15);
assert.equal(ok.exit.applied, 3);
assert.equal(ok.health.symbols, 100, "today's symbols win over the all-time count");
assert.match(ok.headline, /Tape reliable \(coverage 52\.5%\)\. Entry: 6 refused of 40 checks\. Exit: 3 trades tightened\./);
assert.equal(tapeView(doc({ gate: gate({ exit: { applied: 1 } }) })).headline.includes("1 trade tightened"), true);

// warming: not reliable, or coverage under the floor
const w = tapeView(doc({ tape: tape({ reliable: false, reliable_symbols: 0, avg_coverage_pct: 12 }) }));
assert.equal(w.kind, "warming");
assert.equal(w.tone, "warn");
assert.match(w.headline, new RegExp(`coverage 12%, needs ${MIN_COVERAGE_PCT}%`));
assert.equal(tapeView(doc({ tape: tape({ reliable: undefined, reliable_symbols: undefined, avg_coverage_pct: 20 }) })).kind, "warming");

// off: both switches off, or the tape itself off
assert.equal(tapeView(doc({ gate: gate({ mode: "off", exit: { exit_mode: "off" } }) })).kind, "off");
assert.match(tapeView(doc({ tape: tape({ enabled: false }) })).headline, /FLOW_TAPE_ENABLED/);

// warnings
const nodata = tapeView(doc({ tape: null }));
assert.ok(nodata.warnings.some((t) => /market-data did not answer/.test(t)));
assert.equal(nodata.kind, "ok", "missing market-data status alone does not claim the tape is bad");
assert.equal(nodata.tone, "warn");
assert.ok(tapeView(doc({ gate: gate({ calls: 10, no_tape: 4, unreliable: 2 }) })).warnings.some((t) => /more than half of the entry checks/.test(t)));
assert.ok(tapeView(doc({ gate: gate({ errors: 2 }) })).warnings.some((t) => /2 entry call errors/.test(t)));
assert.ok(tapeView(doc({ gate: gate({ exit: { errors: 1 } }) })).warnings.some((t) => /1 exit call error \(/.test(t)));
assert.ok(tapeView(doc({ gate: gate({ exit: { tighten: 3, applied: 0 } }) })).warnings.some((t) => /nothing was applied/.test(t)));
assert.equal(tapeView(doc({ gate: gate({ exit: { tighten: 3, applied: 0, exit_mode: "log" } }) })).warnings.some((t) => /nothing was applied/.test(t)), false, "log mode never applies, so no warning");
assert.equal(tapeView(doc({ tape: tape({ error: "boom" }) })).warnings.some((t) => /tape status error: boom/.test(t)), true);

// zero checks: no division by zero
assert.equal(tapeView(doc({ gate: gate({ calls: 0, blocks: 0 }) })).entry.blockPct, null);

// wiring (source text, as the other panel tests do)
const rd = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const tab = rd("./components/CalibrationControlTab.tsx");
assert.match(tab, /section === "tape" && <TapePanel svc=\{svc\} \/>/);
assert.match(tab, /\{ id: "tape", label: "Order-flow tape" \}/);
assert.match(tab, /section !== "tape"/, "no Run-all bar on the tape section");
const panel = rd("./components/calibration/TapePanel.tsx");
assert.match(panel, /positionStocksApi\.flowStats\(\)/);
assert.match(panel, /realTradeApi\.flowStats\(\)/);
assert.match(panel, /setVisibleInterval/);
assert.ok(!/method:\s*"POST"|controlApi\.set/.test(panel), "the card never changes anything");
assert.match(rd("./realTradeApi.ts"), /flowStats: \(\) => rtRequest<FlowStatsDoc>\("\/flow\/stats", undefined, true\)/);
assert.match(rd("./positionStocksApi.ts"), /flowStats: \(\) => psRequest<FlowStatsDoc>\("\/flow\/stats", undefined, true\)/);

console.log("tape.test.mjs ok");
