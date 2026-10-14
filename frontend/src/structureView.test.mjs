// Run: node --experimental-strip-types structureView.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { tradeEntryView, tradeStructureTally, prepareNoteLines, gateStatus, emptyReason, oneLine, groups, invalidation, badEntryText, stopLine, tally, coveragePct } from "./structureView.ts";

const ok = { opinion: "ok", would_reject: false, enforced: false, reasons: [],
  live: { chase_atr: 0.3, stretch_atr: 1.2, room_atr: 3, rr: 1.9 },
  summary: { ms_quality: "ok", ms_trend: "bullish", ms_bos_dir: "bullish", ms_bos_bars: 2, ms_bos_failed: false, ms_ob_side: "bull", ms_ob_status: "fresh",
    ms_fvg_side: "bull", ms_fvg_status: "tested", ms_stop: 96.4, ms_rr_flags: [] } };
const bad = { ...ok, would_reject: true, reasons: ["chase 2.2 ATR above the nearest demand/imbalance zone (max 1.5)"], live: { ...ok.live, chase_atr: 2.2 } };

assert.deepEqual(gateStatus(ok), { label: "Structure OK", tone: "good" });
assert.equal(gateStatus(bad).tone, "warn"); assert.match(gateStatus(bad).label, /shadow, not applied/);
assert.equal(gateStatus({ ...bad, enforced: true }).tone, "bad");
assert.equal(gateStatus({ opinion: "no_opinion" }).tone, "mute");
assert.equal(gateStatus(null).label, "Structure not checked"); assert.equal(gateStatus(undefined).tone, "mute"); assert.equal(gateStatus("x").tone, "mute");

assert.equal(oneLine(ok), "Trend ↑ · BOS↑ 2d · OB fresh · RR 1.9 · chase 0.3 ATR");
assert.equal(oneLine({ opinion: "no_opinion" }), ""); assert.equal(oneLine(null), "");

assert.equal(emptyReason(ok), null);
assert.match(emptyReason({ opinion: "no_opinion", summary: { ms_quality: "insufficient" } }), /Not enough daily history/);
assert.match(emptyReason({ opinion: "no_opinion", summary: { ms_quality: "stale" } }), /stale/);
assert.match(emptyReason({ opinion: "no_opinion", no_data: "no ATR" }), /no ATR/);
assert.match(emptyReason(null), /not checked/);

const g = groups(bad);
assert.deepEqual(g.map(x => x.title), ["Trend", "Momentum", "Structure", "Location", "Risk"]);
const loc = g.find(x => x.title === "Location");
assert.equal(loc.lines[0].ok, false); assert.equal(loc.lines[1].ok, true);       // chase fails, room is fine
assert.equal(groups(null).length, 0); assert.equal(groups({ opinion: "no_opinion" }).length, 0);

assert.equal(invalidation(ok), "The structure read is invalid below ₹96.40.");
assert.equal(invalidation({}), null);
assert.match(badEntryText(bad), /^Good stock, bad entry: chase 2\.2/); assert.equal(badEntryText(ok), null); assert.equal(badEntryText(null), null);
assert.equal(stopLine({ levels: { mode: "shadow", stop: 96.5, sane: true, use: false } }, 97), "Structural stop ₹96.50 vs ATR stop ₹97.00 (shadow only)");
assert.match(stopLine({ levels: { mode: "on", stop: 80, sane: false, use: false, why: "too wide" } }, null), /not used; too wide/);
assert.equal(stopLine({}, 1), null);

const rows = [{ latest_decision: { structure: ok } }, { latest_decision: { structure: bad } }, { latest_decision: { structure: { ...bad, enforced: true } } },
  { latest_decision: { structure: { opinion: "no_opinion" } } }, { latest_decision: {} }, { latest_decision: null }];
const t = tally(rows);
assert.deepEqual(t, { total: 5, blocked: 1, shadow: 1, noOpinion: 1, checked: 4 });
assert.equal(coveragePct(t), 75); assert.equal(coveragePct(tally(null)), null); assert.equal(tally(undefined).total, 0);
console.log("structureView ok");

// ── group353: decision-service record ───────────────────────────────────────────────────────────────────────────────────────────
import { decisionToSnapshot, policyRecord, waitForEntryText, prepareTriggerText, decisionTally, ruleCounts } from "./structureView.ts";
const feat = { quality: "ok", ver: "1.0.0", trend: "bullish", bos_dir: "bullish", bos_bars: 2, chase_atr: 2.4, stretch_atr: 1, room_atr: 3, rr: 1.9, stop: 96, target: 108, ms_price: 100, rr_flags: [] };
const dsShadow = { mode: "shadow", applied: false, original_decision: "BUY NOW", policy_decision: "WAIT", features: feat,
  rules: [{ id: "P2_CHASE", to: "WAIT", reason: "chasing: price 2.4 ATR beyond the structure reference (max 1.5)" }],
  wait_for_entry: { low: 95, high: 97, kind: "demand zone" }, structural_levels: { stop: 96, target: 108, rr: 1.9 } };
const dsApplied = { ...dsShadow, mode: "caps", applied: true };
const snap = decisionToSnapshot(dsShadow);
assert.equal(snap.opinion, "ok"); assert.equal(snap.would_reject, true); assert.equal(snap.enforced, false); assert.equal(snap.gate_mode, "shadow");
assert.match(gateStatus(snap).label, /shadow, not applied/);
assert.equal(gateStatus(decisionToSnapshot(dsApplied)).tone, "bad");
assert.equal(snap.live.chase_atr, 2.4); assert.equal(snap.levels.stop, 96); assert.equal(snap.levels.use, false);
assert.equal(groups(snap).find(x => x.title === "Location").lines[0].ok, false);          // chase fails
assert.equal(decisionToSnapshot(null), null); assert.equal(decisionToSnapshot({ mode: "off" }), null); assert.equal(decisionToSnapshot("x"), null);
assert.equal(decisionToSnapshot({ mode: "shadow", no_opinion: "structure insufficient" }).opinion, "no_opinion");
assert.match(policyRecord(dsShadow).text, /would change BUY NOW → WAIT — shadow, not applied: chasing/); assert.equal(policyRecord(dsShadow).shadow, true);
assert.match(policyRecord(dsApplied).text, /^BUY NOW → WAIT: chasing/); assert.equal(policyRecord(dsApplied).tone, "bad");
assert.equal(policyRecord({ mode: "shadow", rules: [], original_decision: "BUY NOW", policy_decision: "BUY NOW" }), null); assert.equal(policyRecord(null), null);
assert.equal(waitForEntryText(dsShadow), "Wait for a pullback into the demand zone ₹95.00–₹97.00."); assert.equal(waitForEntryText({}), null);
assert.equal(prepareTriggerText({ prepare_triggers: ["bos_near", "entering_fib", "zzz"] }), "Re-check now: a fresh break of structure, price in the Fibonacci golden zone."); assert.equal(prepareTriggerText({}), null);
const drows = [{ structure: dsShadow }, { structure: dsApplied }, { structure: { mode: "shadow", no_opinion: "x" } }, { structure: { mode: "shadow", rules: [], original_decision: "BUY NOW", policy_decision: "BUY NOW" } }, {}, null];
assert.deepEqual(decisionTally(drows), { checked: 4, wouldChange: 2, applied: 1, noOpinion: 1 });
assert.deepEqual(ruleCounts(drows), [{ id: "P2_CHASE", label: "chasing the move", n: 2 }]);
console.log("structureView group353 ok");

// ── group355: status view model ─────────────────────────────────────────────────────────────────────────────────────────────────
import { statusView, statusHeadline, modeInfo, sinceText, sinceTone, outcomeLine, reasonLabel, nodataLabel, windowText } from "./structureView.ts";
const realRaw = { mode: "DEMO", gate_mode: "enforce", hours: 24, process: { evaluated: 12 },
  stored: { decisions: 20, with_snapshot: 10, ok: 8, no_opinion: 2, would_reject: 4, enforced: 3, shadow_only: 1, coverage_pct: 80,
    by_reason: { chase: 2, bearish: 3, rr: 0 }, by_nodata: { no_summary: 2 }, avg_since_pct: -1.5, rejects_up: 1, rejects_down: 3,
    rejects: [{ symbol: "AAA", price: 100, now: 98, since_pct: -2, enforced: true, reasons: ["chase 2.0 ATR above z"] }, { symbol: "BBB", since_pct: null, enforced: false, reasons: [] }] } };
const rv = statusView("real", realRaw);
assert.equal(rv.checked, 10); assert.equal(rv.stopped, 3); assert.equal(rv.shadow, 1); assert.equal(rv.noOpinion, 2); assert.equal(rv.coveragePct, 80);
assert.deepEqual(rv.reasons.map(r => [r.key, r.n]), [["bearish", 3], ["chase", 2]]);       // zero counts dropped, sorted desc
assert.equal(rv.reasons[0].label, "Bearish daily trend"); assert.equal(rv.nodata[0].label, "No structure data");
assert.equal(rv.rejects.length, 2); assert.equal(rv.rejects[0].sincePct, -2); assert.equal(rv.rejects[0].text, "chase 2.0 ATR above z"); assert.equal(rv.rejects[1].sincePct, null);
assert.equal(rv.headline, "10 checked in 24 hours · 3 blocked · 80% had a structure read"); assert.equal(rv.sinceBoot, 12);
assert.match(outcomeLine(rv), /^Rejected stocks moved −1\.5% on average since \(3 down, 1 up\) — the gate mostly kept you out of drops/);

const shadowRv = statusView("real", { ...realRaw, gate_mode: "shadow" });
assert.match(shadowRv.headline, /1 would have been rejected \(not applied\)/); assert.equal(shadowRv.modeTone, "warn");

const psRaw = { gate_mode: "enforce", hours: 168, process: { evaluated: 5, recent: [
    { symbol: "X", would_reject: true, enforced: true, price: 10, now: 11, since_pct: 10, reasons: ["daily structure is bearish"] },
    { symbol: "Y", would_reject: false, reasons: [] }, { symbol: "Z", would_reject: true, enforced: true, since_pct: -4, reasons: [] }] },
  stored: { refused: 3, refused_bad_location: 2, refused_nodata: 1, by_reason: { bearish: 2 }, by_nodata: { quality: 1 }, refused_symbols: [{ symbol: "X", reason: "r" }],
    entered_with_snapshot: 7, entered_ok: 5, entered_no_opinion: 2, entered_would_reject: 1, entered_by_reason: { bearish: 1, room: 1 } } };
const pv = statusView("position", psRaw);
assert.equal(pv.checked, 10); assert.equal(pv.stopped, 3); assert.equal(pv.shadow, 1); assert.equal(pv.noOpinion, 3); assert.equal(pv.coveragePct, 70);   // (5 + 2) / 10
assert.deepEqual(pv.reasons.map(r => [r.key, r.n]), [["bearish", 3], ["room", 1]]);
assert.deepEqual(pv.rejects.map(r => r.symbol), ["X", "Z"]); assert.equal(pv.up, 1); assert.equal(pv.down, 1); assert.equal(pv.avgSincePct, 3);
assert.equal(statusView("position", { ...psRaw, process: {} }).rejects[0].symbol, "X");                                  // falls back to the stored refusals
assert.match(pv.headline, /^10 checked in 7 days · 3 blocked · 70% had a structure read$/);

for (const bad of [null, undefined, "x", 5, {}, { stored: "no", process: [] }]) {
  const v = statusView("real", bad); assert.equal(v.checked, 0); assert.equal(v.coveragePct, null); assert.deepEqual(v.reasons, []);
  assert.equal(statusView("position", bad).stopped, 0);
}
assert.equal(statusHeadline(statusView("real", { gate_mode: "off" })), "Structure gate is off.");
assert.equal(statusView("real", { gate_mode: "enforce", hours: 24 }).headline, "No candidates checked in the last 24 hours.");
assert.match(statusView("real", { gate_mode: "enforce", hours: 24, process: { evaluated: 4 } }).headline, /4 checked since restart/);
assert.equal(modeInfo("enforce").tone, "good"); assert.equal(modeInfo("shadow").tone, "warn"); assert.equal(modeInfo("off").tone, "mute"); assert.equal(modeInfo(7).label, "Unknown");
assert.equal(sinceText(3.24), "+3.2% since"); assert.equal(sinceText(-1.44), "−1.4% since"); assert.equal(sinceText(0), "0.0% since"); assert.equal(sinceText(null), null); assert.equal(sinceText("x"), null);
assert.equal(sinceTone(-1), "good"); assert.equal(sinceTone(2), "warn"); assert.equal(sinceTone(0), "mute"); assert.equal(sinceTone(undefined), "mute");
assert.equal(outcomeLine(statusView("real", {})), null);
assert.equal(reasonLabel("zzz"), "zzz"); assert.equal(nodataLabel("stale"), "Stale structure data");
assert.equal(windowText(24), "24 hours"); assert.equal(windowText(168), "7 days"); assert.equal(windowText(6), "6 hours");
console.log("structureView group355 ok");

// ── group356: edgeView ──────────────────────────────────────────────────────────────────────────────────────────────────────
import { edgeView, pctText } from "./structureView.ts";
assert.equal(pctText(2.34), "+2.3%"); assert.equal(pctText(-1.04), "−1.0%"); assert.equal(pctText(null), "—"); assert.equal(pctText("x"), "—");
const e0 = edgeView(null);
assert.equal(e0.total, 0); assert.equal(e0.conclusion, "insufficient_sample"); assert.equal(e0.tone, "mute"); assert.equal(e0.horizons.length, 4); assert.match(e0.headline, /Nothing observed/);
const e1 = edgeView({
  stats: { total: 60, evaluated: 50, complete: 40, conclusion: "gate_helps", conclusion_horizon: "10d",
    by_verdict: { rejected: { n: 25, target_first: 3, stop_first: 14, horizons: { "10d": { n: 22, mean: -2 } } }, passed: { n: 30, horizons: { "10d": { n: 28, mean: 2 } } } },
    edge: { "10d": { edge: 4, rejected_n: 22, passed_n: 28 } } },
  recent: [{ symbol: "TCS", obs_day: "2026-10-01", verdict: "rejected_shadow", ret_10d: -3.2, reasons: ["chase 2 ATR"] },
           { symbol: "INFY", verdict: "passed", ret_1d: null }],
});
assert.equal(e1.tone, "good"); assert.equal(e1.rejectedN, 25); assert.equal(e1.stopFirstRejected, 14);
assert.deepEqual(e1.horizons.find(h => h.h === "10d"), { h: "10d", rejected: -2, passed: 2, edge: 4, rejectedN: 22, passedN: 28 });
assert.equal(e1.recent[0].ret, "−3.2%"); assert.equal(e1.recent[0].tone, "good"); assert.equal(e1.recent[0].verdictLabel, "Would block (shadow)"); assert.equal(e1.recent[1].ret, "pending");
assert.equal(edgeView({ stats: { conclusion: "gate_hurts" } }).tone, "bad");
assert.equal(edgeView({ stats: { conclusion: "weird" } }).conclusion, "weird"); assert.equal(edgeView({ stats: { conclusion: "weird" } }).tone, "mute");
assert.equal(edgeView({ error: "boom" }).error, "boom");
console.log("structureView edge tests ok");

import { chartOverlay } from "./structureView.ts";
const blk = { quality: "ok", last_close: 100, structure_rr: { stop: 95, target: 112 },
  levels: { supports: [{ price_low: 93, price_high: 94 }, { price_low: 90, price_high: 91 }, { price_low: 80, price_high: 81 }], resistances: [{ price_low: 110, price_high: 111 }, { price_low: 300, price_high: 301 }] },
  order_block: { top: 98, bottom: 96, status: "fresh" }, fvg: null };
const ov = chartOverlay(blk);
assert.equal(ov.empty, false);
assert.deepEqual(ov.lines.map(l => l.kind + ":" + l.y), ["stop:95", "target:112", "support:94", "support:91", "resistance:110"]);   // 300 is >25% away: dropped; 3rd support not taken
assert.equal(ov.zones.length, 1); assert.equal(ov.zones[0].label, "Demand zone fresh");
assert.equal(chartOverlay(null).empty, true); assert.equal(chartOverlay({ quality: "insufficient" }).empty, true); assert.equal(chartOverlay("x").empty, true);
console.log("chartOverlay tests ok");

// ── group357: sniper card structure ─────────────────────────────────────────────────────────────────────────────────────────
import { sniperStructureView, sniperRejectedText } from "./structureView.ts";
assert.equal(sniperStructureView(null), null); assert.equal(sniperStructureView("x"), null); assert.equal(sniperStructureView(undefined), null);
const sv0 = sniperStructureView({ opinion: "no_opinion", no_data: "no structure summary" });
assert.equal(sv0.label, "No structure read for this stock"); assert.equal(sv0.tone, "mute"); assert.equal(sv0.stop, null); assert.equal(sv0.rr, null);
const sv1 = sniperStructureView({ opinion: "ok", would_reject: false, enforced: false, stop: 96, target: 112, rr: 3, reasons: [] });
assert.equal(sv1.tone, "good"); assert.equal(sv1.stop, "₹96.00"); assert.equal(sv1.target, "₹112.00"); assert.equal(sv1.rr, "3.00");
const sv2 = sniperStructureView({ opinion: "ok", would_reject: true, enforced: false, reasons: ["chase 2.5 ATR"], live: { rr: 1.04 } });
assert.equal(sv2.tone, "warn"); assert.match(sv2.label, /shadow, not applied/); assert.deepEqual(sv2.reasons, ["chase 2.5 ATR"]); assert.equal(sv2.rr, "1.04");
assert.equal(sniperStructureView({ opinion: "ok", would_reject: true, enforced: true }).tone, "bad");
assert.equal(sniperStructureView({ opinion: "ok", stop: -3, target: "x", rr: NaN }).stop, null);
assert.equal(sniperRejectedText("enforce", []), null); assert.equal(sniperRejectedText("off", [{ symbol: "A" }]), null); assert.equal(sniperRejectedText("enforce", null), null);
assert.equal(sniperRejectedText("enforce", [{ symbol: "TCS" }]), "1 setup turned away by daily structure (good stock, bad entry): TCS");
assert.match(sniperRejectedText("enforce", ["A", "B", "C", "D", "E"].map(symbol => ({ symbol }))), /^5 setups .*: A, B, C \+2 more$/);
console.log("sniperStructureView tests ok");

// group358: PREPARE refresh notes
assert.deepEqual(prepareNoteLines(null), []); assert.deepEqual(prepareNoteLines([1]), []); assert.deepEqual(prepareNoteLines("x"), []);
const pn = prepareNoteLines({ EVT: { headline: "Structure event: bullish change of character", turned_bad: false },
  OLD: { headline: "Structure now blocks entry: bearish", turned_bad: true, flipped: false },
  FLIP: { headline: "Structure now blocks entry: chase (it passed at the last stored price)", turned_bad: true, flipped: true },
  EMPTY: { headline: "  " }, NUL: null });
assert.deepEqual(pn.map(x => x.symbol), ["FLIP", "OLD", "EVT"]); assert.equal(pn[0].tone, "warn"); assert.equal(pn[2].tone, "good");
assert.equal(prepareNoteLines({ A: { headline: "a" }, B: { headline: "b" } }, 1).length, 1); assert.equal(prepareNoteLines({ A: { headline: "a" } }, 0).length, 0);
console.log("prepareNoteLines tests ok");

// group359: entry-time structure on paper trades
const entrySnap = { v: 1, mode: "shadow", applied: false, original_decision: "BUY NOW", policy_decision: "WAIT", rules: [{ reason: "chasing: 2.1 ATR" }],
  levels: { stop: 96, target: 112, rr: 1.9 }, summary_line: "Trend up · BOS up 2d" };
for (const bad of [null, undefined, {}, "x", 5, { mode: "off" }]) { const v = tradeEntryView(bad); assert.equal(v.tone, "mute"); assert.equal(v.flagged, null); assert.match(v.label, /before snapshots/); }
let entryV = tradeEntryView(entrySnap);
assert.equal(entryV.tone, "warn"); assert.equal(entryV.flagged, true); assert.match(entryV.label, /would have changed BUY NOW → WAIT at entry \(shadow, not applied\)/);
assert.equal(entryV.levels, "Structural stop ₹96.00 · target ₹112.00 · R:R 1.90"); assert.equal(entryV.line, "Trend up · BOS up 2d"); assert.deepEqual(entryV.reasons, ["chasing: 2.1 ATR"]);
entryV = tradeEntryView({ ...entrySnap, applied: true }); assert.equal(entryV.tone, "bad"); assert.doesNotMatch(entryV.label, /shadow/); assert.equal(entryV.short, "flagged");
entryV = tradeEntryView({ mode: "shadow", rules: [] }); assert.equal(entryV.tone, "good"); assert.equal(entryV.flagged, false); assert.equal(entryV.levels, null);
entryV = tradeEntryView({ mode: "full", no_opinion: "structure stale", rules: [{ reason: "x" }] }); assert.equal(entryV.tone, "mute"); assert.equal(entryV.flagged, null); assert.match(entryV.label, /structure stale/);
assert.equal(tradeEntryView({ mode: "shadow", rules: [{ reason: " " }, null, { reason: 5 }] }).flagged, false);   // blank reasons are not a flag
assert.equal(tradeEntryView({ mode: "shadow", rules: [], levels: { stop: -1, target: "x", rr: NaN } }).levels, null);

const closedT = (structure, pnl_pct, status = "CLOSED") => ({ status, pnl_pct, structure });
const tallyR = tradeStructureTally([closedT(entrySnap, -2), closedT(entrySnap, -4), closedT({ mode: "shadow", rules: [] }, 3), closedT(null, 9), closedT({ mode: "shadow", no_opinion: "stale" }, 1),
  closedT(entrySnap, null), closedT(entrySnap, 5, "OPEN"), null, "x"], 20);
assert.deepEqual(tallyR, { closed: 6, withSnapshot: 4, flagged: 2, supported: 1, flaggedAvgPct: -3, supportedAvgPct: 3, smallSample: true });
assert.equal(tradeStructureTally(undefined).closed, 0); assert.equal(tradeStructureTally("x").flaggedAvgPct, null);
const manyT = [...Array(20)].map(() => closedT(entrySnap, -1)).concat([...Array(20)].map(() => closedT({ mode: "shadow", rules: [] }, 1)));
assert.equal(tradeStructureTally(manyT).smallSample, false); assert.equal(tradeStructureTally(manyT, 21).smallSample, true);
console.log("tradeEntryView tests ok");

// ── group362: calibrationView ────────────────────────────────────────────────────────────────────────────────────────────────
import { calibrationView } from "./structureView.ts";
assert.equal(calibrationView(null), null); assert.equal(calibrationView({}), null); assert.equal(calibrationView({ calibration: null }), null);
assert.equal(calibrationView({ calibration: { error: "boom" } }).error, "boom"); assert.equal(calibrationView({ calibration: { error: "boom" } }).available, false);
const cal0 = calibrationView({ calibration: { samples: 0, finished: 0 } });
assert.equal(cal0.available, false); assert.match(cal0.headline, /starts once/);
const calSmall = calibrationView({ calibration: { samples: 12, finished: 5, min_n: 20, horizon: 5, note: "insufficient_sample", rules: {}, suggestions: [] } });
assert.equal(calSmall.available, true); assert.match(calSmall.headline, /5 of 12 rows finished at 5 days · needs 20 to judge/); assert.equal(calSmall.note, "No rule has turned anything away yet.");
const calFull = calibrationView({ calibration: {
  advisory: true, samples: 120, finished: 90, min_n: 20, horizon: 10, note: null,
  rules: {
    chase: { rejected: { n: 40, mean: -2 }, passed: { n: 80, mean: 1.5 }, edge: 3.5, verdict: "helps", advice: "keep", current: 1.5, suggested: 2, walk_forward: { oos_edge: 1.2, stable: true } },
    rr: { rejected: { n: 0 }, passed: { n: 120 }, edge: null, verdict: "insufficient_sample", advice: "collect_more", current: 1.2 },
    room: { rejected: { n: 25, mean: 2 }, passed: { n: 95, mean: 0.5 }, edge: -1.5, verdict: "hurts", advice: "review_loosen", current: 0.5 },
    stretch: { rejected: { n: 3 }, passed: { n: 117 }, edge: null, advice: "weird-value", current: 3 },
  },
  score: { monotonic: true, spread: 2.34, n: 90 },
  suggestions: [{ rule: "chase", from: 1.5, to: 2, oos_edge: 1.234 }],
} });
assert.equal(calFull.available, true); assert.equal(calFull.error, null); assert.equal(calFull.note, null);
assert.match(calFull.headline, /90 of 120 rows finished at 10 days$/);
assert.deepEqual(calFull.rules.map(r => r.rule), ["chase", "room", "stretch"]);                 // rr never tripped, so it is not listed
const ch = calFull.rules[0];
assert.equal(ch.label, "Chasing past the zone"); assert.equal(ch.edge, "+3.5%"); assert.equal(ch.tone, "good"); assert.equal(ch.current, "1.5"); assert.equal(ch.suggested, "2");
assert.equal(ch.rejectedN, 40); assert.equal(ch.passedN, 80); assert.match(ch.advice, /^Keep/);
assert.equal(calFull.rules[1].tone, "warn"); assert.equal(calFull.rules[1].edge, "−1.5%"); assert.match(calFull.rules[1].advice, /^Review/);
assert.equal(calFull.rules[2].tone, "mute"); assert.equal(calFull.rules[2].advice, "No clear effect yet"); assert.equal(calFull.rules[2].edge, "—");    // unknown advice -> neutral
assert.equal(calFull.suggestions[0], "Chasing past the zone: 1.5 → 2 (out-of-sample +1.2%)");
assert.match(calFull.score, /higher structure score went with better outcomes \(top vs bottom third \+2\.3%\)/i);
assert.match(calibrationView({ calibration: { samples: 50, finished: 50, score: { monotonic: false, spread: -0.4 }, rules: {} } }).score, /did not rank outcomes cleanly \(top vs bottom third −0\.4%\)/);
assert.equal(calibrationView({ calibration: { samples: 50, finished: 50, score: { note: "insufficient_sample" }, rules: {} } }).score, null);
assert.equal(calibrationView({ calibration: { samples: 5, finished: 5, rules: "junk", suggestions: "junk", score: "junk" } }).rules.length, 0);   // garbage never throws
console.log("structureView group362 calibration ok");
