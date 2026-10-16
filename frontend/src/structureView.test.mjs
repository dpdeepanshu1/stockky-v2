// Run: node --experimental-strip-types structureView.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { gateStatus, emptyReason, oneLine, groups, invalidation, badEntryText, stopLine, tally, coveragePct } from "./structureView.ts";

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
assert.deepEqual(ruleCounts([{ structure: { mode: "full", applied: true, original_decision: "BUY NOW", policy_decision: "WAIT", rules: [{ id: "P5_BREAK", to: "WAIT", reason: "x" }, { id: "P4_REWARD", to: "PREPARE TO BUY", reason: "y" }] } }]).map((x) => x.label).sort(),
  ["fresh bearish break", "target too close"]);   // group407: every rule id the policy can fire has a plain label
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

// ── group356/362: edge report view model ──
import { edgeView, calibrationView, pctText } from "./structureView.ts";
assert.equal(pctText(0.44), "+0.4%"); assert.equal(pctText(-1.24), "−1.2%"); assert.equal(pctText(0), "0.0%"); assert.equal(pctText(null), "—"); assert.equal(pctText("x"), "—");
const ev0 = edgeView(null);
assert.equal(ev0.conclusionLabel, "Not enough data yet"); assert.equal(ev0.horizons.length, 4); assert.equal(ev0.recent.length, 0); assert.equal(calibrationView(null), null); assert.equal(calibrationView({}), null);
const edoc = { min_n: 100, stats: { total: 300, complete: 120, conclusion: "gate_helps", conclusion_horizon: "5d",
  by_verdict: { rejected: { n: 40, stop_first: 9, target_first: 3, horizons: { "5d": { n: 30, mean: -1.2 } } }, passed: { n: 200, horizons: { "5d": { n: 150, mean: 0.4 } } } },
  edge: { "5d": { edge: 1.6, rejected_n: 30, passed_n: 150 } } },
  recent: [{ symbol: "ABC", obs_day: "2026-10-01", verdict: "rejected_shadow", ret_5d: -2.5, reasons: ["chase 2.2 ATR"] }, { symbol: "XYZ", obs_day: "2026-10-02", verdict: "passed", ret_1d: -0.5 }],
  calibration: { advisory: true, horizon: 5, min_n: 100, finished: 120, thresholds: { chase_atr: 1.5 },
    rules: { chase: { rejected: { n: 30 }, passed: { n: 90 }, edge: 0.9, advice: "keep" }, rr: { rejected: { n: 0 }, passed: { n: 5 }, edge: null, advice: "collect_more" } },
    score: { n: 120, monotonic: true }, suggestions: [{ rule: "chase", from: 1.5, to: 2, oos_edge: 0.4 }] } };
const ev = edgeView(edoc);
assert.equal(ev.tone, "good"); assert.equal(ev.conclusionLabel, "Gate is helping"); assert.match(ev.headline, /\+1\.6%/);
assert.deepEqual(ev.horizons[1], { h: "5d", rejected: -1.2, rejectedN: 30, passed: 0.4, passedN: 150, edge: 1.6 });
assert.equal(ev.rejectedN, 40); assert.equal(ev.stopFirstRejected, 9); assert.equal(ev.targetFirstRejected, 3);
assert.equal(ev.recent[0].tone, "good"); assert.equal(ev.recent[0].ret, "−2.5%"); assert.equal(ev.recent[0].verdictLabel, "would have blocked"); assert.equal(ev.recent[1].tone, "bad");
const cv = calibrationView(edoc);
assert.equal(cv.rules.length, 1); assert.equal(cv.rules[0].current, "1.5"); assert.equal(cv.rules[0].edge, "+0.9%"); assert.equal(cv.rules[0].tone, "good");
assert.match(cv.score, /did rank/); assert.match(cv.suggestions[0], /try 2 instead of 1\.5/);
assert.equal(calibrationView({ calibration: { advisory: true, error: "boom" } }).error, "boom");
assert.equal(edgeView({ stats: { total: 5, conclusion: "weird" } }).tone, "mute");

import { prepareNoteLines } from "./structureView.ts";
const pn = prepareNoteLines({
  ZED: { headline: "Structure event: fresh bullish break of structure", turned_bad: false, flipped: false },
  BAD: { headline: "Structure now blocks entry: chase 1.9 ATR", turned_bad: true, flipped: false },
  FLIP: { headline: "Structure now blocks entry: chase 1.9 ATR (passed at the last stored price)", turned_bad: true, flipped: true },
  EMPTY: { headline: null, turned_bad: true }, JUNK: 5, NUL: null,
});
assert.deepEqual(pn.map((x) => x.symbol), ["FLIP", "BAD", "ZED"]);
assert.deepEqual(pn.map((x) => x.tone), ["warn", "warn", "good"]); assert.equal(pn[0].flipped, true); assert.equal(pn[1].flipped, false);
assert.equal(prepareNoteLines({ A: { headline: "x" }, B: { headline: "y" }, C: { headline: "z" } }, 2).length, 2);
assert.equal(prepareNoteLines({ A: { headline: "x" } }, 0).length, 0);
for (const g of [null, undefined, [], "x", 3, {}]) assert.deepEqual(prepareNoteLines(g), []);
console.log("prepareNoteLines ok");

// ---- group 373: sniper badge + held-back lines
import { sniperBadge, sniperHeldBackLines, sniperPlanRows } from "./structureView.ts";
{
  const okRec = { gate_mode: "enforce", opinion: "ok", would_reject: false, enforced: true, reasons: [], live: { chase_atr: 0.34, rr: 1.94 } };
  const b = sniperBadge(okRec);
  assert.equal(b.label, "Structure OK"); assert.equal(b.tone, "good"); assert.match(b.title, /chase 0\.3 ATR, structure R:R 1\.9/);
  assert.equal(sniperBadge({ gate_mode: "enforce", opinion: "ok", would_reject: false }).title, "Passed the daily-structure check.");
  const nb = sniperBadge({ gate_mode: "enforce", opinion: "no_opinion", no_data: "no structure summary" });
  assert.equal(nb.label, "No structure read"); assert.equal(nb.tone, "mute"); assert.match(nb.title, /no structure summary/);
  assert.match(sniperBadge({ opinion: "no_opinion" }).title, /No daily structure available/);
  const wb = sniperBadge({ opinion: "ok", would_reject: true, enforced: false, reasons: ["chase 2.2 ATR"] });
  assert.equal(wb.label, "Structure warns"); assert.equal(wb.tone, "warn"); assert.equal(wb.title, "chase 2.2 ATR");
  assert.equal(sniperBadge(null), null); assert.equal(sniperBadge(undefined), null); assert.equal(sniperBadge("x"), null);
  assert.equal(sniperBadge([]), null); assert.equal(sniperBadge({ gate_mode: "off", opinion: "ok" }), null);

  assert.deepEqual(sniperHeldBackLines(null), []); assert.deepEqual(sniperHeldBackLines("x"), []); assert.deepEqual(sniperHeldBackLines([null, 1, {}, { symbol: " " }]), []);
  const hl = sniperHeldBackLines([
    { symbol: "ABC", price: 101.5, reasons: ["chase 2.2 ATR", "stretch 3.4 ATR", "third"] },
    { symbol: "ABC", reasons: ["dup"] }, { symbol: "XYZ", reasons: [] },
  ]);
  assert.equal(hl.length, 2);
  assert.deepEqual(hl[0], { symbol: "ABC", text: "chase 2.2 ATR; stretch 3.4 ATR (at ₹101.50)" });
  assert.equal(hl[1].text, "daily structure advises against entering now");
  assert.equal(sniperHeldBackLines([{ symbol: "A" }, { symbol: "B" }], 1).length, 1);
  assert.equal(sniperHeldBackLines([{ symbol: "A" }], 0).length, 0);
}

// ---- group 400: sniper plan rows (structure stop / target / entry zone / wait for entry)
{
  const rec = { gate_mode: "enforce", opinion: "ok", would_reject: false, stop: 96, target: 112, rr: 3,
    entry_zone: { kind: "demand", bottom: 97, top: 99, status: "fresh", dist_pct: 1.01 }, wait_for_entry: false, wait_reason: null };
  const rows = sniperPlanRows(rec);
  assert.deepEqual(rows.map((r) => r.label), ["Structure stop", "Structure target", "Entry zone"]);
  assert.equal(rows[0].text, "\u20b996.00"); assert.equal(rows[1].text, "\u20b9112.00 (R:R 3.0)");
  assert.equal(rows[2].text, "\u20b997.00 \u2013 \u20b999.00 (demand)");
  const w = sniperPlanRows({ ...rec, wait_for_entry: true, wait_reason: "0.9 ATR above the demand zone, better entry near \u20b999" });
  assert.equal(w.at(-1).label, "Wait for entry"); assert.equal(w.at(-1).tone, "warn"); assert.match(w.at(-1).text, /better entry near/);
  assert.equal(sniperPlanRows({ ...rec, wait_for_entry: true }).at(-1).text, "price is extended from the structure");
  // no invention: missing / bad levels are skipped, never shown as 0
  assert.deepEqual(sniperPlanRows({ opinion: "ok", stop: null, target: 0, entry_zone: { kind: "demand", bottom: 99, top: 97 } }), []);
  assert.deepEqual(sniperPlanRows({ opinion: "ok", stop: "x", target: NaN, rr: 2 }), []);
  assert.equal(sniperPlanRows({ opinion: "ok", target: 112 })[0].text, "\u20b9112.00");
  assert.equal(sniperPlanRows({ opinion: "ok", entry_zone: { kind: "imbalance", bottom: 98, top: 99 } })[0].text, "\u20b998.00 \u2013 \u20b999.00 (imbalance)");
  // no opinion / gate off / garbage -> nothing
  for (const g of [null, undefined, [], "x", 3, {}, { opinion: "no_opinion", stop: 96 }, { gate_mode: "off", opinion: "ok", stop: 96 }]) assert.deepEqual(sniperPlanRows(g), []);
  console.log("sniperPlanRows ok");
}
