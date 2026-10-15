// group351: pure helpers for the daily market-structure snapshot (real-trade decision.structure, api-gateway structure_summary `ms_*`).
// No React, no I/O, never throws: every function takes unknown / partial input and returns a safe value. Node test: structureView.test.mjs
// Wording rule: say "demand zone" / "imbalance zone" / "break of structure", not trading jargon the dashboard never explained.

export interface StructureSummary {
  ms_quality?: string | null; ms_ver?: string | null; ms_asof?: string | null; ms_trend?: string | null;
  ms_bos_dir?: string | null; ms_bos_bars?: number | null; ms_bos_failed?: boolean | null;
  ms_choch_dir?: string | null; ms_choch_bars?: number | null;
  ms_fvg_side?: string | null; ms_fvg_status?: string | null; ms_ob_side?: string | null; ms_ob_status?: string | null;
  ms_in_golden?: boolean | null; ms_loc_score?: number | null; ms_rr?: number | null; ms_stop?: number | null; ms_target?: number | null;
  ms_chase_atr?: number | null; ms_stretch_atr?: number | null; ms_room_atr?: number | null; ms_atr?: number | null;
  ms_tqs?: number | null; ms_groups?: number | null; ms_rr_flags?: string[] | null; ms_price?: number | null;
}

export interface StructureLive { chase_atr?: number | null; stretch_atr?: number | null; room_atr?: number | null; rr?: number | null }
export interface StructureLevels {
  mode?: string; stop?: number | null; target?: number | null; sane?: boolean; use?: boolean; why?: string | null; risk_mult?: number;
}
/** What real-trade stores per ENTRY decision (trade_decisions.structure_json), exposed as latest_decision.structure. */
export interface StructureSnapshotDoc {
  v?: number; gate_mode?: "off" | "shadow" | "enforce" | string; enforced?: boolean; source?: string | null;
  opinion?: "ok" | "no_opinion" | string; would_reject?: boolean; reasons?: string[]; no_data?: string | null;
  live?: StructureLive; levels?: StructureLevels | null; summary?: StructureSummary;
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const f1 = (v: number | null): string => (v == null ? "—" : v.toFixed(1));

export type Tone = "good" | "bad" | "warn" | "mute";

/** Gate state in one phrase + tone. Shadow is always labelled "not applied" so nobody reads it as a block. */
export function gateStatus(s: StructureSnapshotDoc | null | undefined): { label: string; tone: Tone } {
  if (!s || typeof s !== "object") return { label: "Structure not checked", tone: "mute" };
  if (s.opinion !== "ok") return { label: "No structure opinion", tone: "mute" };
  if (!s.would_reject) return { label: "Structure OK", tone: "good" };
  return s.enforced ? { label: "Blocked by structure", tone: "bad" } : { label: "Would reject — shadow, not applied", tone: "warn" };
}

/** Why there is no opinion, in plain words (empty-state text). */
export function emptyReason(s: StructureSnapshotDoc | null | undefined): string | null {
  if (!s || typeof s !== "object") return "Daily structure was not checked for this decision.";
  if (s.opinion === "ok") return null;
  const q = s.summary?.ms_quality;
  if (q === "insufficient") return "Not enough daily history for a structure read.";
  if (q === "stale") return "The structure data is stale, so it is not used.";
  if (q === "unavailable" || q == null) return s.no_data ? `No structure read: ${s.no_data}.` : "No daily structure available for this stock.";
  return s.no_data ? `No structure opinion: ${s.no_data}.` : "No structure opinion.";
}

const dirWord = (d?: string | null) => (d === "bullish" ? "↑" : d === "bearish" ? "↓" : "");

/** Mobile one-liner: "Trend ↑ · BOS 2d · OB fresh · RR 1.9 · chase 0.3 ATR". Parts with no data are dropped. */
export function oneLine(s: StructureSnapshotDoc | null | undefined): string {
  const sm = s?.summary;
  if (!sm || s?.opinion !== "ok") return "";
  const live = s?.live || {};
  const parts: string[] = [];
  if (sm.ms_trend) parts.push(`Trend ${dirWord(sm.ms_trend) || sm.ms_trend}`);
  const bos = num(sm.ms_bos_bars);
  if (sm.ms_bos_dir && bos != null) parts.push(`BOS${dirWord(sm.ms_bos_dir)} ${bos}d`);
  if (sm.ms_ob_status) parts.push(`OB ${sm.ms_ob_status}`);
  else if (sm.ms_fvg_status) parts.push(`FVG ${sm.ms_fvg_status}`);
  const rr = num(live.rr) ?? num(sm.ms_rr);
  if (rr != null) parts.push(`RR ${rr.toFixed(1)}`);
  const ch = num(live.chase_atr) ?? num(sm.ms_chase_atr);
  if (ch != null) parts.push(`chase ${ch.toFixed(1)} ATR`);
  return parts.join(" · ");
}

export interface CheckLine { ok: boolean | null; text: string }
export interface StructureGroup { title: "Trend" | "Momentum" | "Structure" | "Location" | "Risk"; lines: CheckLine[] }

/** Five groups of ✓ / ✗ / · lines (null = no data, shown neutral). Thresholds come from the saved reasons, not re-derived here. */
export function groups(s: StructureSnapshotDoc | null | undefined): StructureGroup[] {
  const sm = s?.summary;
  if (!sm || s?.opinion !== "ok") return [];
  const live = s?.live || {};
  const reasons = (s?.reasons || []).join(" | ");
  const bad = (needle: string) => reasons.includes(needle);
  const chase = num(live.chase_atr) ?? num(sm.ms_chase_atr), stretch = num(live.stretch_atr) ?? num(sm.ms_stretch_atr);
  const room = num(live.room_atr) ?? num(sm.ms_room_atr), rr = num(live.rr) ?? num(sm.ms_rr);
  const flags = sm.ms_rr_flags || [];
  const bosBars = num(sm.ms_bos_bars);
  return [
    { title: "Trend", lines: [
      { ok: sm.ms_trend == null ? null : sm.ms_trend === "bullish", text: `Daily trend ${sm.ms_trend ?? "unknown"}` },
      { ok: sm.ms_bos_dir == null ? null : sm.ms_bos_dir === "bullish" && !sm.ms_bos_failed,
        text: sm.ms_bos_dir ? `${sm.ms_bos_dir} break of structure${bosBars != null ? ` ${bosBars} bar(s) ago` : ""}${sm.ms_bos_failed ? " (failed)" : ""}` : "No recent break of structure" },
      ...(sm.ms_choch_dir ? [{ ok: sm.ms_choch_dir === "bullish", text: `${sm.ms_choch_dir} change of character${num(sm.ms_choch_bars) != null ? ` ${sm.ms_choch_bars} bar(s) ago` : ""}` }] : []),
    ] },
    { title: "Momentum", lines: [
      { ok: stretch == null ? null : !bad("stretched"), text: `${f1(stretch)} ATR above the 20-day average` },
    ] },
    { title: "Structure", lines: [
      { ok: sm.ms_ob_status ? sm.ms_ob_side === "bull" || sm.ms_ob_side === "bullish" : null,
        text: sm.ms_ob_status ? `Demand zone ${sm.ms_ob_status}` : "No demand zone found" },
      { ok: sm.ms_fvg_status ? sm.ms_fvg_side === "bull" || sm.ms_fvg_side === "bullish" : null,
        text: sm.ms_fvg_status ? `Imbalance zone ${sm.ms_fvg_status}` : "No imbalance zone found" },
      ...(sm.ms_in_golden != null ? [{ ok: sm.ms_in_golden ? true : null, text: sm.ms_in_golden ? "Inside the Fibonacci golden zone" : "Outside the Fibonacci golden zone" }] : []),
    ] },
    { title: "Location", lines: [
      { ok: chase == null ? null : !bad("chas"), text: `${f1(chase)} ATR above the nearest demand/imbalance zone` },
      { ok: room == null ? null : !bad("room"), text: `${f1(room == null ? null : Math.max(0, room))} ATR of room to the next resistance` },
    ] },
    { title: "Risk", lines: [
      { ok: rr == null ? null : !bad("R:R"), text: `Structure R:R ${rr == null ? "—" : rr.toFixed(2)}` },
      { ok: flags.includes("stop_too_wide") ? false : null, text: flags.includes("stop_too_wide") ? "Structural stop is wide" : "Structural stop within limits" },
      ...(flags.includes("no_target") ? [{ ok: null, text: "No structural target found" }] : []),
    ] },
  ];
}

/** Invalidation line: where the read breaks. */
export function invalidation(s: StructureSnapshotDoc | null | undefined): string | null {
  const stop = num(s?.levels?.stop) ?? num(s?.summary?.ms_stop);
  return stop == null ? null : `The structure read is invalid below ₹${stop.toFixed(2)}.`;
}

/** Plain "good stock, bad entry" sentence for a negative decision; null when nothing was against the entry. */
export function badEntryText(s: StructureSnapshotDoc | null | undefined): string | null {
  if (!s || s.opinion !== "ok" || !s.would_reject || !(s.reasons || []).length) return null;
  return `Good stock, bad entry: ${(s.reasons || []).join("; ")}.`;
}

/** Structural vs ATR stop line for the stop options. */
export function stopLine(s: StructureSnapshotDoc | null | undefined, atrStop: number | null | undefined): string | null {
  const lv = s?.levels;
  const st = num(lv?.stop);
  if (!lv || st == null) return null;
  const used = lv.use ? "used" : lv.mode === "shadow" ? "shadow only" : "not used";
  const atr = num(atrStop);
  return `Structural stop ₹${st.toFixed(2)}${atr != null ? ` vs ATR stop ₹${atr.toFixed(2)}` : ""} (${used}${lv.sane === false && lv.why ? `; ${lv.why}` : ""})`;
}

export interface RejectionTally { total: number; blocked: number; shadow: number; noOpinion: number; checked: number }

/** Count structure outcomes across candidate rows (latest_decision.structure + gate_tag), for the rejection-reason summary. */
export function tally(rows: Array<{ latest_decision?: { gate_tag?: string | null; structure?: StructureSnapshotDoc | null } | null }> | null | undefined): RejectionTally {
  const t: RejectionTally = { total: 0, blocked: 0, shadow: 0, noOpinion: 0, checked: 0 };
  for (const r of Array.isArray(rows) ? rows : []) {
    const d = r?.latest_decision;
    if (!d) continue;
    t.total++;
    const s = d.structure;
    if (!s || typeof s !== "object") continue;
    t.checked++;
    if (s.opinion !== "ok") t.noOpinion++;
    else if (s.would_reject && s.enforced) t.blocked++;
    else if (s.would_reject) t.shadow++;
  }
  return t;
}

/** Share of rows whose structure had an opinion, as a whole percent (null when nothing was checked). */
export function coveragePct(t: RejectionTally): number | null {
  return t.checked > 0 ? Math.round(((t.checked - t.noOpinion) / t.checked) * 100) : null;
}

// ── group353: the decision service's own `structure` record (response.structure from /decide) ──────────────────────────────────────
export interface DecisionStructureRule { id: string; to: string; reason: string }
export interface DecisionStructure {
  mode?: "off" | "shadow" | "caps" | "full" | string; applied?: boolean; original_decision?: string; policy_decision?: string;
  rules?: DecisionStructureRule[]; no_opinion?: string | null; summary_line?: string | null;
  wait_for_entry?: { low: number; high: number; kind: string } | null; prepare_triggers?: string[];
  legacy_levels?: { entry_range?: { low: number | null; high: number | null } | null; target?: number | null; stop_loss?: number | null } | null;
  structural_levels?: { stop?: number | null; target?: number | null; rr?: number | null } | null;
  features?: Record<string, any> | null; explain?: { positives?: string[]; warnings?: string[]; summary?: string } | null;
}

/** Adapt the decision record onto the snapshot shape StructurePanel / the helpers above already render. Never throws. */
export function decisionToSnapshot(ds: DecisionStructure | null | undefined): StructureSnapshotDoc | null {
  if (!ds || typeof ds !== "object" || !ds.mode || ds.mode === "off") return null;
  const f = (ds.features && typeof ds.features === "object" ? ds.features : {}) as Record<string, any>;
  const rules = Array.isArray(ds.rules) ? ds.rules : [];
  const noOp = ds.no_opinion || null;
  const sl = ds.structural_levels;
  return {
    v: 1, gate_mode: ds.mode === "shadow" ? "shadow" : "enforce", enforced: !!ds.applied, source: "decision",
    opinion: noOp ? "no_opinion" : "ok", would_reject: rules.length > 0, reasons: rules.map(r => r.reason), no_data: noOp,
    live: { chase_atr: num(f.chase_atr), stretch_atr: num(f.stretch_atr), room_atr: num(f.room_atr), rr: num(f.rr) },
    levels: sl && num(sl.stop) != null ? { mode: ds.mode, stop: num(sl.stop), target: num(sl.target), sane: true, use: ds.mode === "full" && !!ds.applied } : null,
    summary: {
      ms_quality: f.quality ?? null, ms_ver: f.ver ?? null, ms_trend: f.trend ?? null, ms_bos_dir: f.bos_dir ?? null, ms_bos_bars: num(f.bos_bars),
      ms_bos_failed: f.bos_failed ?? null, ms_choch_dir: f.choch_dir ?? null, ms_choch_bars: num(f.choch_bars),
      ms_fvg_side: f.fvg_side ?? null, ms_fvg_status: f.fvg_status ?? null, ms_ob_side: f.ob_side ?? null, ms_ob_status: f.ob_status ?? null,
      ms_in_golden: f.in_golden ?? null, ms_rr: num(f.rr), ms_stop: num(f.stop), ms_target: num(f.target), ms_chase_atr: num(f.chase_atr),
      ms_stretch_atr: num(f.stretch_atr), ms_room_atr: num(f.room_atr), ms_tqs: num(f.tqs), ms_groups: num(f.groups),
      ms_rr_flags: Array.isArray(f.rr_flags) ? f.rr_flags : [], ms_price: num(f.ms_price),
    },
  };
}

/** "BUY NOW → WAIT: chasing …" record line. Shadow is always labelled "shadow — not applied". Null when the policy had nothing to say. */
export function policyRecord(ds: DecisionStructure | null | undefined): { text: string; tone: Tone; shadow: boolean } | null {
  if (!ds || typeof ds !== "object" || !ds.mode || ds.mode === "off") return null;
  const rules = Array.isArray(ds.rules) ? ds.rules : [];
  if (!rules.length || !ds.original_decision || !ds.policy_decision || ds.original_decision === ds.policy_decision) return null;
  const why = rules.map(r => r.reason).join("; ");
  if (ds.applied) return { text: `${ds.original_decision} → ${ds.policy_decision}: ${why}`, tone: "bad", shadow: false };
  return { text: `Structure would change ${ds.original_decision} → ${ds.policy_decision} — shadow, not applied: ${why}`, tone: "warn", shadow: true };
}

/** "Wait for a pullback into the demand zone ₹95.00–₹97.00" when the policy found a sound re-entry area. */
export function waitForEntryText(ds: DecisionStructure | null | undefined): string | null {
  const z = ds?.wait_for_entry;
  if (!z || num(z.low) == null || num(z.high) == null) return null;
  return `Wait for a pullback into the ${z.kind || "zone"} ₹${z.low.toFixed(2)}–₹${z.high.toFixed(2)}.`;
}

const TRIGGER_WORDS: Record<string, string> = {
  bos_near: "a fresh break of structure", choch: "a change of character", sweep: "a liquidity sweep and reclaim",
  entering_fvg: "price entering an imbalance zone", entering_ob: "price entering a demand zone", entering_fib: "price in the Fibonacci golden zone",
};
/** Plain-word list of why a PREPARE TO BUY should be re-checked now. */
export function prepareTriggerText(ds: DecisionStructure | null | undefined): string | null {
  const t = (Array.isArray(ds?.prepare_triggers) ? ds!.prepare_triggers! : []).map(k => TRIGGER_WORDS[k]).filter(Boolean);
  return t.length ? `Re-check now: ${t.join(", ")}.` : null;
}

export interface DecisionTally { checked: number; wouldChange: number; applied: number; noOpinion: number }
/** Across scan rows (decision responses): how many had structure, how many it would / did downgrade, how many had no opinion. */
export function decisionTally(rows: Array<{ structure?: DecisionStructure | null }> | null | undefined): DecisionTally {
  const t: DecisionTally = { checked: 0, wouldChange: 0, applied: 0, noOpinion: 0 };
  for (const r of Array.isArray(rows) ? rows : []) {
    const s = r?.structure;
    if (!s || typeof s !== "object" || !s.mode || s.mode === "off") continue;
    t.checked++;
    if (s.no_opinion) { t.noOpinion++; continue; }
    if (policyRecord(s)) { t.wouldChange++; if (s.applied) t.applied++; }
  }
  return t;
}

/** Counts of which rule fired across scan rows, most common first - for the "why nothing qualified" summary. */
export function ruleCounts(rows: Array<{ structure?: DecisionStructure | null }> | null | undefined): Array<{ id: string; label: string; n: number }> {
  const LABEL: Record<string, string> = { P2_CHASE: "chasing the move", P3_STRETCH: "stretched above average", P4_RR: "poor structure risk/reward",
    P5_TREND: "bearish daily structure", P6_ROOM: "no room to resistance", P7_QUALITY: "thin confirmation", P8_FAILED_BOS: "failed breakout" };
  const m = new Map<string, number>();
  for (const r of Array.isArray(rows) ? rows : []) {
    const s = r?.structure;
    if (!s || !policyRecord(s)) continue;
    for (const x of s.rules || []) m.set(x.id, (m.get(x.id) || 0) + 1);
  }
  return [...m.entries()].map(([id, n]) => ({ id, label: LABEL[id] || id, n })).sort((a, b) => b.n - a.n);
}

// ── group355: gate status view model (GET /structure/status/{mode} on real-trade, GET /structure/status on position-stocks) ──────────
/** Raw reply of either service. Loosely typed on purpose: every field is read defensively by statusView(). */
export interface StructureStatusDoc {
  mode?: string; gate_mode?: string; hours?: number;
  process?: Record<string, any> | null;
  stored?: Record<string, any> | null;
}
export type StructureStatusKind = "real" | "position";
export interface StatusCount { key: string; label: string; n: number }
export interface StatusReject { symbol: string; text: string; sincePct: number | null; enforced: boolean | null }
export interface StructureStatusView {
  kind: StructureStatusKind; gateMode: string; modeLabel: string; modeTone: Tone; hours: number;
  checked: number; stopped: number; shadow: number; noOpinion: number; coveragePct: number | null;
  reasons: StatusCount[]; nodata: StatusCount[]; rejects: StatusReject[];
  avgSincePct: number | null; up: number; down: number; sinceBoot: number; headline: string;
}

const REASON_LABEL: Record<string, string> = {
  chase: "Chasing above the demand zone", stretch: "Stretched above the 20-day average", room: "No room to the next resistance",
  rr: "Poor structure R:R", stop_wide: "Structural stop too wide", bos: "Fresh bearish break of structure", bearish: "Bearish daily trend", other: "Other",
};
const NODATA_LABEL: Record<string, string> = {
  no_summary: "No structure data", quality: "Not enough daily history", stale: "Stale structure data", price_dev: "Price far from structure close",
  no_price: "No live price", no_atr: "No ATR", error: "Evaluation error", other: "Other",
};

export function reasonLabel(key: string): string { return REASON_LABEL[key] ?? key; }
export function nodataLabel(key: string): string { return NODATA_LABEL[key] ?? key; }

function counts(obj: unknown, labels: (k: string) => string): StatusCount[] {
  if (!obj || typeof obj !== "object") return [];
  return Object.entries(obj as Record<string, unknown>)
    .map(([key, v]) => ({ key, label: labels(key), n: num(v) ?? 0 }))
    .filter(c => c.n > 0)
    .sort((a, b) => b.n - a.n || a.key.localeCompare(b.key));
}

export function modeInfo(m: unknown): { label: string; tone: Tone } {
  if (m === "enforce") return { label: "Enforcing — blocks bad entries", tone: "good" };
  if (m === "shadow") return { label: "Shadow — recording only, blocks nothing", tone: "warn" };
  if (m === "off") return { label: "Off", tone: "mute" };
  return { label: "Unknown", tone: "mute" };
}

/** "+3.2% since" / "−1.4% since"; null when there is no comparison price. Sign uses a real minus. */
export function sinceText(pct: number | null | undefined): string | null {
  const v = num(pct);
  if (v == null) return null;
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1)}% since`;
}

/** Tone for a rejected stock's move: it fell (the gate helped) = good, it rose (the gate cost a gain) = warn. */
export function sinceTone(pct: number | null | undefined): Tone {
  const v = num(pct);
  return v == null || v === 0 ? "mute" : v < 0 ? "good" : "warn";
}

function rejectText(reasons: unknown): string {
  const list = Array.isArray(reasons) ? reasons.map(String) : typeof reasons === "string" && reasons ? reasons.split("; ") : [];
  return list.join("; ");
}

/** Normalise either service's reply into one shape the card renders. Never throws; unusable input gives zeros. */
export function statusView(kind: StructureStatusKind, raw: StructureStatusDoc | null | undefined): StructureStatusView {
  const r = raw && typeof raw === "object" ? raw : {};
  const st = (r.stored && typeof r.stored === "object" ? r.stored : {}) as Record<string, any>;
  const pr = (r.process && typeof r.process === "object" ? r.process : {}) as Record<string, any>;
  const mi = modeInfo(r.gate_mode);
  const v: StructureStatusView = {
    kind, gateMode: typeof r.gate_mode === "string" ? r.gate_mode : "unknown", modeLabel: mi.label, modeTone: mi.tone, hours: num(r.hours) ?? 24,
    checked: 0, stopped: 0, shadow: 0, noOpinion: 0, coveragePct: null, reasons: [], nodata: [], rejects: [],
    avgSincePct: null, up: 0, down: 0, sinceBoot: num(pr.evaluated) ?? 0, headline: "",
  };
  if (kind === "real") {
    v.checked = num(st.with_snapshot) ?? 0;
    v.stopped = num(st.enforced) ?? 0;
    v.shadow = num(st.shadow_only) ?? 0;
    v.noOpinion = num(st.no_opinion) ?? 0;
    v.coveragePct = num(st.coverage_pct);
    v.reasons = counts(st.by_reason, reasonLabel);
    v.nodata = counts(st.by_nodata, nodataLabel);
    v.avgSincePct = num(st.avg_since_pct);
    v.up = num(st.rejects_up) ?? 0;
    v.down = num(st.rejects_down) ?? 0;
    v.rejects = (Array.isArray(st.rejects) ? st.rejects : []).map((x: any): StatusReject => ({
      symbol: String(x?.symbol ?? ""), text: rejectText(x?.reasons), sincePct: num(x?.since_pct), enforced: typeof x?.enforced === "boolean" ? x.enforced : null,
    }));
  } else {
    const refused = num(st.refused) ?? 0, badLoc = num(st.refused_bad_location) ?? 0, nodataRef = num(st.refused_nodata) ?? 0;
    const entered = num(st.entered_with_snapshot) ?? 0, enteredOk = num(st.entered_ok) ?? 0;
    v.checked = entered + refused;
    v.stopped = refused;
    v.shadow = num(st.entered_would_reject) ?? 0;
    v.noOpinion = (num(st.entered_no_opinion) ?? 0) + nodataRef;
    v.coveragePct = v.checked > 0 ? Math.round(((enteredOk + badLoc) / v.checked) * 100) : null;
    v.reasons = counts(st.by_reason, reasonLabel);
    for (const c of counts(st.entered_by_reason, reasonLabel)) {
      const hit = v.reasons.find(x => x.key === c.key);
      if (hit) hit.n += c.n; else v.reasons.push(c);
    }
    v.reasons.sort((a, b) => b.n - a.n || a.key.localeCompare(b.key));
    v.nodata = counts(st.by_nodata, nodataLabel);
    const recent = (Array.isArray(pr.recent) ? pr.recent : []).filter((x: any) => x && x.would_reject);
    const moves: number[] = [];
    v.rejects = recent.slice(0, 15).map((x: any): StatusReject => {
      const p = num(x?.since_pct);
      if (p != null) { moves.push(p); if (p > 0) v.up++; else if (p < 0) v.down++; }
      return { symbol: String(x?.symbol ?? ""), text: rejectText(x?.reasons), sincePct: p, enforced: typeof x?.enforced === "boolean" ? x.enforced : null };
    });
    if (!v.rejects.length) {
      v.rejects = (Array.isArray(st.refused_symbols) ? st.refused_symbols : []).map((x: any): StatusReject => ({
        symbol: String(x?.symbol ?? ""), text: rejectText(x?.reason), sincePct: null, enforced: true,
      }));
    }
    if (moves.length) v.avgSincePct = Math.round((moves.reduce((a, b) => a + b, 0) / moves.length) * 100) / 100;
  }
  v.headline = statusHeadline(v);
  return v;
}

/** One sentence for the collapsed card. Plain words; shadow is never described as blocking. */
export function statusHeadline(v: StructureStatusView): string {
  if (v.gateMode === "off") return "Structure gate is off.";
  if (v.checked === 0) return v.sinceBoot > 0 ? `${v.sinceBoot} checked since restart; nothing stored in the last ${windowText(v.hours)} yet.` : `No candidates checked in the last ${windowText(v.hours)}.`;
  const cov = v.coveragePct == null ? "" : ` · ${v.coveragePct}% had a structure read`;
  const act = v.gateMode === "shadow" ? `${v.shadow} would have been rejected (not applied)` : `${v.stopped} blocked`;
  return `${v.checked} checked in ${windowText(v.hours)} · ${act}${cov}`;
}

export function windowText(hours: number): string {
  if (hours >= 48 && hours % 24 === 0) return `${hours / 24} days`;
  return hours === 24 ? "24 hours" : `${hours} hours`;
}

/** What the post-rejection moves say, or null when there is nothing to compare. A hint, not proof: small samples. */
export function outcomeLine(v: StructureStatusView): string | null {
  const n = v.up + v.down;
  if (n === 0 || v.avgSincePct == null) return null;
  const avg = `${v.avgSincePct > 0 ? "+" : v.avgSincePct < 0 ? "−" : ""}${Math.abs(v.avgSincePct).toFixed(1)}%`;
  const lean = v.avgSincePct < 0 ? "the gate mostly kept you out of drops" : v.avgSincePct > 0 ? "the gate mostly turned away stocks that kept rising" : "no clear lean";
  return `Rejected stocks moved ${avg} on average since (${v.down} down, ${v.up} up) — ${lean}. Small sample, a hint only.`;
}

// ── group356/362: "what happened next" edge report (GET /structure/edge[/{mode}] -> {stats, calibration, recent, ...}) ───────────────────
/** Raw reply of the edge report. Loosely typed on purpose: edgeView() / calibrationView() read every field defensively. */
export interface StructureEdgeDoc {
  mode?: string; days?: number; min_n?: number;
  stats?: Record<string, any> | null;
  calibration?: Record<string, any> | null;
  recent?: Array<Record<string, any>> | null;
  process?: Record<string, any> | null;
  error?: string | null;
}

/** "+0.4%" / "−1.2%" / "0.0%" / "—" (no value). Sign uses a real minus, same as sinceText. */
export function pctText(v: unknown): string {
  const n = num(v);
  if (n == null) return "—";
  const r = Math.round(n * 10) / 10;
  return `${r > 0 ? "+" : r < 0 ? "−" : ""}${Math.abs(r).toFixed(1)}%`;
}

const EDGE_HORIZONS = ["1d", "5d", "10d", "20d"];
const CONCLUSION: Record<string, { label: string; tone: Tone }> = {
  gate_helps: { label: "Gate is helping", tone: "good" },
  gate_hurts: { label: "Gate is costing gains", tone: "bad" },
  inconclusive: { label: "No clear edge", tone: "warn" },
  insufficient_sample: { label: "Not enough data yet", tone: "mute" },
};
const VERDICT_LABEL: Record<string, string> = {
  rejected_enforced: "blocked", rejected_shadow: "would have blocked", passed: "let through", no_opinion: "no opinion",
};

export interface EdgeHorizonRow { h: string; rejected: number | null; rejectedN: number; passed: number | null; passedN: number; edge: number | null }
export interface EdgeRecentRow { symbol: string; day: string; verdictLabel: string; text: string | null; tone: Tone; ret: string }
export interface StructureEdgeView {
  tone: Tone; conclusionLabel: string; headline: string; error: string | null;
  horizons: EdgeHorizonRow[]; rejectedN: number; stopFirstRejected: number; targetFirstRejected: number; recent: EdgeRecentRow[];
}

const rec = (v: unknown): Record<string, any> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, any>) : {});
const int0 = (v: unknown): number => { const n = num(v); return n == null ? 0 : Math.max(0, Math.round(n)); };
const clip = (s: unknown, max: number): string | null => {
  if (typeof s !== "string" || !s.trim()) return null;
  const t = s.trim();
  return t.length > max ? `${t.slice(0, max - 1)}…` : t;
};

/** View model of the edge table. Never throws; null / garbage input gives an empty, honest "not enough data" view. */
export function edgeView(raw: StructureEdgeDoc | null | undefined): StructureEdgeView {
  const d = rec(raw);
  const stats = rec(d.stats);
  const byV = rec(stats.by_verdict);
  const rej = rec(byV.rejected);
  const pas = rec(byV.passed);
  const edge = rec(stats.edge);
  const horizons: EdgeHorizonRow[] = EDGE_HORIZONS.map(h => {
    const a = rec(rec(rej.horizons)[h]);
    const b = rec(rec(pas.horizons)[h]);
    return { h, rejected: num(a.mean), rejectedN: int0(a.n), passed: num(b.mean), passedN: int0(b.n), edge: num(rec(edge[h]).edge) };
  });
  const conclusionKey = typeof stats.conclusion === "string" && CONCLUSION[stats.conclusion] ? stats.conclusion : "insufficient_sample";
  const c = CONCLUSION[conclusionKey];
  const total = int0(stats.total);
  const complete = int0(stats.complete);
  const minN = int0(d.min_n);
  const ch = typeof stats.conclusion_horizon === "string" ? stats.conclusion_horizon : null;
  let headline: string;
  if (total === 0) headline = "No observations stored yet.";
  else if (conclusionKey === "insufficient_sample") {
    headline = `${total} observed, ${complete} fully tracked${minN > 0 ? ` — needs ${minN} finished on each side` : ""}.`;
  } else {
    const e = ch ? num(rec(edge[ch]).edge) : null;
    headline = `${total} observed${ch && e != null ? ` · after ${ch.replace("d", " days")}, let-through stocks beat turned-away ones by ${pctText(e)}` : ""}.`;
  }
  const recent: EdgeRecentRow[] = (Array.isArray(d.recent) ? d.recent : []).slice(0, 40).map(r0 => {
    const r = rec(r0);
    const verdict = typeof r.verdict === "string" ? r.verdict : "";
    const got = ["ret_20d", "ret_10d", "ret_5d", "ret_1d"].map(k => num(r[k])).find(x => x != null) ?? null;
    const wasRejected = verdict === "rejected_enforced" || verdict === "rejected_shadow";
    let tone: Tone = "mute";
    if (got != null && got !== 0) tone = wasRejected ? (got < 0 ? "good" : "warn") : (got > 0 ? "good" : "bad");
    const reasons = Array.isArray(r.reasons) ? r.reasons : [];
    return {
      symbol: typeof r.symbol === "string" ? r.symbol : "?", day: typeof r.obs_day === "string" ? r.obs_day : "",
      verdictLabel: VERDICT_LABEL[verdict] ?? (verdict || "unknown"), text: clip(reasons[0], 140), tone, ret: got == null ? "—" : pctText(got),
    };
  });
  const error = clip(d.error, 200);
  return {
    tone: c.tone, conclusionLabel: c.label, headline, error, horizons, rejectedN: int0(rej.n),
    stopFirstRejected: int0(rej.stop_first), targetFirstRejected: int0(rej.target_first), recent,
  };
}

export interface CalibrationRuleView { rule: string; label: string; rejectedN: number; passedN: number; current: string | null; edge: string; advice: string; tone: Tone }
export interface CalibrationView {
  error: string | null; headline: string; note: string | null; rules: CalibrationRuleView[]; score: string | null; suggestions: string[];
}

const ADVICE_TEXT: Record<string, { text: string; tone: Tone }> = {
  keep: { text: "Earning its place — keep.", tone: "good" },
  review_loosen: { text: "Turned away stocks that did fine — worth reviewing.", tone: "warn" },
  no_evidence: { text: "No clear evidence either way.", tone: "mute" },
  collect_more: { text: "Not enough finished observations yet.", tone: "mute" },
};
const RULE_LABEL: Record<string, string> = { ...REASON_LABEL, stop_wide: REASON_LABEL.stop_wide, bearish: REASON_LABEL.bearish };

/** View model of the advisory calibration block, or null when the report carries none. Advisory only: nothing here changes a setting. */
export function calibrationView(raw: StructureEdgeDoc | null | undefined): CalibrationView | null {
  const cal = rec(rec(raw).calibration);
  if (!Object.keys(cal).length) return null;
  const error = clip(cal.error, 200);
  if (error) return { error, headline: "Calibration (advisory)", note: null, rules: [], score: null, suggestions: [] };
  const h = num(cal.horizon);
  const finished = int0(cal.finished);
  const minN = int0(cal.min_n);
  const note = cal.note === "insufficient_sample"
    ? `Only ${finished} observation${finished === 1 ? "" : "s"} finished${h != null ? ` at ${h} days` : ""}; ${minN > 0 ? `${minN} are needed before the advice means much` : "too few for advice"}.` : null;
  const th = rec(cal.thresholds);
  const thKey: Record<string, string> = { chase: "chase_atr", stretch: "stretch_atr", room: "room_atr", rr: "rr" };
  const rules: CalibrationRuleView[] = Object.entries(rec(cal.rules)).map(([rule, r0]) => {
    const r = rec(r0);
    const a = ADVICE_TEXT[typeof r.advice === "string" ? r.advice : ""] ?? ADVICE_TEXT.no_evidence;
    const cur = num(r.current ?? th[thKey[rule] ?? ""]);
    return {
      rule, label: RULE_LABEL[rule] ?? rule, rejectedN: int0(rec(r.rejected).n), passedN: int0(rec(r.passed).n),
      current: cur == null ? null : String(cur), edge: pctText(r.edge), advice: a.text, tone: a.tone,
    };
  }).filter(r => r.rejectedN > 0);
  const sc = rec(cal.score);
  let score: string | null = null;
  if (typeof sc.monotonic === "boolean") {
    score = sc.monotonic ? "A higher structure score did rank better outcomes." : "A higher structure score did not rank outcomes reliably.";
  } else if (int0(sc.n) > 0) score = "Score ranking: not enough observations yet.";
  const suggestions = (Array.isArray(cal.suggestions) ? cal.suggestions : []).map(s0 => {
    const s = rec(s0);
    const from = num(s.from), to = num(s.to);
    if (typeof s.rule !== "string" || to == null) return null;
    return `${RULE_LABEL[s.rule] ?? s.rule}: try ${to}${from == null ? "" : ` instead of ${from}`}${num(s.oos_edge) != null ? ` (out-of-sample ${pctText(s.oos_edge)})` : ""}`;
  }).filter((x): x is string => x != null);
  return { error: null, headline: `Calibration (advisory)${h != null ? ` · ${h}-day horizon` : ""}`, note, rules, score, suggestions };
}

// ---- group 372: Prepare-to-Buy refresh notes (reply of POST /data-feed/refresh-prepare-to-buy -> structure_notes). Log-only in the backend.
export interface PrepareNoteLine { symbol: string; text: string; tone: Tone; flipped: boolean }

/** Lines for the notes list under "Refresh Prepare-to-Buy": flips first (bad now, passed at the last stored price), then other turned-bad rows,
 *  then structure events. Alphabetical inside each group, at most `max` lines, garbage -> []. */
export function prepareNoteLines(notes: unknown, max = 8): PrepareNoteLine[] {
  if (!notes || typeof notes !== "object" || Array.isArray(notes)) return [];
  const rows: Array<PrepareNoteLine & { rank: number }> = [];
  for (const [symbol, raw] of Object.entries(notes as Record<string, unknown>)) {
    if (!raw || typeof raw !== "object") continue;
    const n = raw as Record<string, unknown>;
    const headline = typeof n.headline === "string" ? n.headline.trim() : "";
    if (!headline) continue;
    const bad = n.turned_bad === true;
    const flipped = bad && n.flipped === true;
    rows.push({ symbol, text: headline, tone: bad ? "warn" : "good", flipped, rank: flipped ? 0 : bad ? 1 : 2 });
  }
  rows.sort((a, b) => a.rank - b.rank || a.symbol.localeCompare(b.symbol));
  const cap = Number.isFinite(max) ? Math.max(0, Math.floor(max)) : 8;
  return rows.slice(0, cap).map(({ rank: _r, ...line }) => line);
}

// ---- group 373: Buy Sniper card badge + "held back" list (POST /api/scan/find-buys -> suggestions[].structure, structure_rejected, message)
export interface SniperStructureRecord {
  gate_mode?: string; opinion?: string; would_reject?: boolean; enforced?: boolean; reasons?: string[]; no_data?: string | null;
  live?: { chase_atr?: number | null; stretch_atr?: number | null; room_atr?: number | null; rr?: number | null } | null;
  stop?: number | null; target?: number | null; rr?: number | null;
  /** group 400 (display only): nearest bullish demand / imbalance zone at or below the price, and the "wait for entry" call. */
  entry_zone?: { kind?: string; bottom?: number | null; top?: number | null; status?: string | null; dist_pct?: number | null } | null;
  wait_for_entry?: boolean; wait_reason?: string | null;
}
export interface SniperBadge { label: string; tone: Tone; title: string }

/** Small badge for a surfaced Buy Sniper card. null = the record is missing (older server / gate off): show nothing, never a false "OK".
 *  A card that carries `would_reject` without `enforced` is labelled as a warning, not as blocked. Never throws. */
export function sniperBadge(s: SniperStructureRecord | null | undefined): SniperBadge | null {
  if (!s || typeof s !== "object" || Array.isArray(s)) return null;
  if (s.gate_mode === "off") return null;
  const reasons = (Array.isArray(s.reasons) ? s.reasons : []).filter((x): x is string => typeof x === "string" && x.trim() !== "");
  if (s.opinion !== "ok") {
    const why = typeof s.no_data === "string" && s.no_data.trim() ? s.no_data.trim() : "No daily structure available for this stock.";
    return { label: "No structure read", tone: "mute", title: `Not checked against the daily structure: ${why}` };
  }
  if (s.would_reject === true) {
    return { label: "Structure warns", tone: "warn", title: reasons.length ? reasons.join("; ") : "The daily structure advises against this entry." };
  }
  const live = s.live && typeof s.live === "object" ? s.live : {};
  const bits: string[] = [];
  const ch = num(live.chase_atr), rr = num(live.rr) ?? num(s.rr);
  if (ch != null) bits.push(`chase ${ch.toFixed(1)} ATR`);
  if (rr != null) bits.push(`structure R:R ${rr.toFixed(1)}`);
  return { label: "Structure OK", tone: "good", title: bits.length ? `Passed the daily-structure check (${bits.join(", ")}).` : "Passed the daily-structure check." };
}

export interface SniperPlanRow { label: string; text: string; tone: Tone }

const inr = (n: number): string => `\u20b9${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/** group 400: extra card rows from the structure record: structural stop, structural target (+R:R), entry zone, and a "wait for entry" line.
 *  Only for a record with a real opinion; a missing level is skipped, never shown as 0 or a guess. These are the daily-structure levels,
 *  separate from the card's own Target / Stop loss rows. Never throws; garbage -> []. */
export function sniperPlanRows(s: SniperStructureRecord | null | undefined): SniperPlanRow[] {
  if (!s || typeof s !== "object" || Array.isArray(s) || s.gate_mode === "off" || s.opinion !== "ok") return [];
  const rows: SniperPlanRow[] = [];
  const stop = num(s.stop), tgt = num(s.target), rr = num(s.rr) ?? num((s.live as { rr?: unknown } | null | undefined)?.rr);
  if (stop != null && stop > 0) rows.push({ label: "Structure stop", text: inr(stop), tone: "bad" });
  if (tgt != null && tgt > 0) rows.push({ label: "Structure target", text: rr != null ? `${inr(tgt)} (R:R ${rr.toFixed(1)})` : inr(tgt), tone: "good" });
  const z = s.entry_zone && typeof s.entry_zone === "object" ? s.entry_zone : null;
  const zb = z ? num(z.bottom) : null, zt = z ? num(z.top) : null;
  if (z && zb != null && zt != null && zb > 0 && zt >= zb) {
    const kind = z.kind === "imbalance" ? "imbalance" : "demand";
    rows.push({ label: "Entry zone", text: `${inr(zb)} \u2013 ${inr(zt)} (${kind})`, tone: "mute" });
  }
  if (s.wait_for_entry === true) {
    const why = typeof s.wait_reason === "string" && s.wait_reason.trim() ? s.wait_reason.trim() : "price is extended from the structure";
    rows.push({ label: "Wait for entry", text: why, tone: "warn" });
  }
  return rows;
}

export interface SniperHeldBackLine { symbol: string; text: string }

/** Lines for "held back by the daily-structure check": one per symbol, first two reasons, at most `max` lines. Garbage -> []. */
export function sniperHeldBackLines(rejected: unknown, max = 6): SniperHeldBackLine[] {
  if (!Array.isArray(rejected)) return [];
  const out: SniperHeldBackLine[] = [];
  const seen = new Set<string>();
  for (const r0 of rejected) {
    if (!r0 || typeof r0 !== "object") continue;
    const r = r0 as Record<string, unknown>;
    const symbol = typeof r.symbol === "string" ? r.symbol.trim() : "";
    if (!symbol || seen.has(symbol)) continue;
    seen.add(symbol);
    const reasons = (Array.isArray(r.reasons) ? r.reasons : []).filter((x): x is string => typeof x === "string" && x.trim() !== "");
    const px = num(r.price);
    const text = reasons.slice(0, 2).join("; ") || "daily structure advises against entering now";
    out.push({ symbol, text: px != null ? `${text} (at ₹${px.toFixed(2)})` : text });
  }
  const cap = Number.isFinite(max) ? Math.max(0, Math.floor(max)) : 6;
  return out.slice(0, cap);
}
