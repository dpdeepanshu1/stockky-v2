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

// ── group356 (WS-6): did what the gate turned away really do worse? (GET /structure/edge/{mode} real-trade, GET /structure/edge position-stocks) ──
export interface EdgeHorizonStats { n?: number; mean?: number | null; median?: number | null; win_pct?: number | null }
export interface EdgeVerdictStats {
  n?: number; evaluated?: number; horizons?: Record<string, EdgeHorizonStats>; mfe_mean?: number | null; mae_mean?: number | null;
  target_first?: number; stop_first?: number; untouched?: number;
}
export interface EdgeRow {
  symbol?: string; obs_day?: string; price?: number; verdict?: string; reasons?: string[]; first_touch?: string | null;
  ret_1d?: number | null; ret_5d?: number | null; ret_10d?: number | null; ret_20d?: number | null; bars_seen?: number; complete?: boolean;
}
export interface StructureEdgeDoc {
  mode?: string; days?: number; error?: string;
  stats?: {
    total?: number; evaluated?: number; complete?: number; conclusion?: string; conclusion_horizon?: string | null;
    by_verdict?: Record<string, EdgeVerdictStats>; edge?: Record<string, { edge?: number; rejected_n?: number; passed_n?: number }>;
  } | null;
  recent?: EdgeRow[]; process?: Record<string, any> | null;
  calibration?: CalibrationDoc | null;     // group362: advisory calibration from the same observations (never applied automatically)
}
export interface CalibrationRuleDoc {
  rejected?: { n?: number; mean?: number | null }; passed?: { n?: number; mean?: number | null }; edge?: number | null; verdict?: string; advice?: string;
  current?: number | null; suggested?: number | null; walk_forward?: { oos_edge?: number | null; stable?: boolean | null } | null;
}
export interface CalibrationDoc {
  advisory?: boolean; horizon?: number; min_n?: number; samples?: number; finished?: number; note?: string | null; error?: string;
  rules?: Record<string, CalibrationRuleDoc>; score?: { monotonic?: boolean | null; spread?: number | null; n?: number; note?: string } | null;
  suggestions?: Array<{ rule?: string; from?: number | null; to?: number | null; oos_edge?: number | null }>;
}
export interface EdgeHorizonView { h: string; rejected: number | null; passed: number | null; edge: number | null; rejectedN: number; passedN: number }
export interface EdgeView {
  total: number; evaluated: number; complete: number; conclusion: string; conclusionLabel: string; tone: Tone; headline: string;
  horizons: EdgeHorizonView[]; rejectedN: number; passedN: number; targetFirstRejected: number; stopFirstRejected: number;
  recent: Array<{ symbol: string; day: string; verdict: string; verdictLabel: string; ret: string; tone: Tone; text: string }>; error: string | null;
}

export const pctText = (v: unknown): string => {
  const n = num(v);
  return n == null ? "—" : `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(1)}%`;
};
const VERDICT_LABEL: Record<string, string> = { passed: "Passed", rejected_enforced: "Blocked", rejected_shadow: "Would block (shadow)", no_opinion: "No opinion" };

/** Normalise an edge reply. The conclusion stays "not enough data yet" until the backend says otherwise; nothing here invents a verdict. */
export function edgeView(raw: StructureEdgeDoc | null | undefined): EdgeView {
  const r = raw && typeof raw === "object" ? raw : {};
  const st = (r.stats && typeof r.stats === "object" ? r.stats : {}) as NonNullable<StructureEdgeDoc["stats"]>;
  const bv = (st.by_verdict && typeof st.by_verdict === "object" ? st.by_verdict : {}) as Record<string, EdgeVerdictStats>;
  const rej = bv.rejected || {}, pas = bv.passed || {};
  const horizons: EdgeHorizonView[] = ["1d", "5d", "10d", "20d"].map(h => ({
    h, rejected: num(rej.horizons?.[h]?.mean), passed: num(pas.horizons?.[h]?.mean), edge: num(st.edge?.[h]?.edge),
    rejectedN: num(rej.horizons?.[h]?.n) ?? 0, passedN: num(pas.horizons?.[h]?.n) ?? 0,
  }));
  const concl = typeof st.conclusion === "string" ? st.conclusion : "insufficient_sample";
  const label: Record<string, { t: string; tone: Tone }> = {
    gate_helps: { t: "The gate is turning away the weaker entries", tone: "good" },
    gate_hurts: { t: "The gate is turning away entries that did better", tone: "bad" },
    inconclusive: { t: "No clear difference yet", tone: "warn" },
    insufficient_sample: { t: "Not enough finished observations yet", tone: "mute" },
  };
  const c = label[concl] || label.insufficient_sample;
  const total = num(st.total) ?? 0, complete = num(st.complete) ?? 0;
  const hz = typeof st.conclusion_horizon === "string" ? st.conclusion_horizon : null;
  const headline = total === 0 ? "Nothing observed yet — verdicts are followed for 20 trading days."
    : `${total} verdicts followed · ${complete} finished (T+20)${hz ? ` · judged at ${hz}` : ""}`;
  const recent = (Array.isArray(r.recent) ? r.recent : []).slice(0, 15).map(x => {
    const ret = num(x?.ret_20d) ?? num(x?.ret_10d) ?? num(x?.ret_5d) ?? num(x?.ret_1d);
    const rejected = typeof x?.verdict === "string" && x.verdict.startsWith("rejected");
    return {
      symbol: String(x?.symbol ?? ""), day: String(x?.obs_day ?? ""), verdict: String(x?.verdict ?? ""), verdictLabel: VERDICT_LABEL[String(x?.verdict)] ?? String(x?.verdict ?? ""),
      ret: ret == null ? "pending" : pctText(ret),
      tone: (ret == null ? "mute" : rejected ? (ret < 0 ? "good" : "warn") : (ret > 0 ? "good" : "warn")) as Tone,
      text: Array.isArray(x?.reasons) ? x.reasons.join("; ") : "",
    };
  });
  return {
    total, evaluated: num(st.evaluated) ?? 0, complete, conclusion: concl, conclusionLabel: c.t, tone: c.tone, headline, horizons,
    rejectedN: num(rej.n) ?? 0, passedN: num(pas.n) ?? 0, targetFirstRejected: num(rej.target_first) ?? 0, stopFirstRejected: num(rej.stop_first) ?? 0,
    recent, error: typeof r.error === "string" ? r.error : null,
  };
}

// ── group362: calibration view (advisory) ──────────────────────────────────────────────────────────────────────────────────────────
export interface CalibrationRuleView { rule: string; label: string; rejectedN: number; passedN: number; edge: string; advice: string; tone: Tone; current: string | null; suggested: string | null }
export interface CalibrationView {
  available: boolean; headline: string; rules: CalibrationRuleView[]; score: string | null; suggestions: string[]; note: string | null; error: string | null;
}
const RULE_LABEL: Record<string, string> = {
  chase: "Chasing past the zone", stretch: "Stretched above EMA20", room: "Little room to resistance", rr: "Reward : risk too low",
  stop_wide: "Structural stop too wide", bearish: "Bearish structure",
};
const ADVICE: Record<string, { t: string; tone: Tone }> = {
  keep: { t: "Keep — it turns away the weaker entries", tone: "good" },
  review_loosen: { t: "Review — what it turned away did better", tone: "warn" },
  no_evidence: { t: "No clear effect yet", tone: "mute" },
  collect_more: { t: "Needs more finished observations", tone: "mute" },
};
const thText = (v: unknown): string | null => { const n = num(v); return n == null ? null : String(Math.round(n * 100) / 100); };

/** The advisory calibration block of an edge reply, or null when the backend did not send one. Nothing here is a setting change: it only words
 *  what the observations say, and a threshold hint appears only when the backend's out-of-sample test supports it. */
export function calibrationView(raw: StructureEdgeDoc | null | undefined): CalibrationView | null {
  const c = raw && typeof raw === "object" ? raw.calibration : null;
  if (!c || typeof c !== "object") return null;
  const empty: CalibrationView = { available: false, headline: "", rules: [], score: null, suggestions: [], note: null, error: null };
  if (typeof c.error === "string") return { ...empty, error: c.error };
  const samples = num(c.samples) ?? 0, finished = num(c.finished) ?? 0, minN = num(c.min_n) ?? 20, h = num(c.horizon);
  if (samples === 0) return { ...empty, headline: "Calibration starts once verdicts have a finished outcome." };
  const enough = c.note !== "insufficient_sample";
  const headline = `Calibration (advisory) · ${finished} of ${samples} rows finished${h != null ? ` at ${h} days` : ""}`
    + (enough ? "" : ` · needs ${minN} to judge`);
  const rules: CalibrationRuleView[] = [];
  for (const [rule, r] of Object.entries(c.rules && typeof c.rules === "object" ? c.rules : {})) {
    const rejectedN = num(r?.rejected?.n) ?? 0, passedN = num(r?.passed?.n) ?? 0;
    if (rejectedN === 0) continue;                       // a rule that never tripped has nothing to judge
    const a = ADVICE[String(r?.advice)] ?? ADVICE.no_evidence;
    const sug = thText(r?.suggested);
    rules.push({ rule, label: RULE_LABEL[rule] ?? rule, rejectedN, passedN, edge: pctText(r?.edge), advice: a.t, tone: a.tone, current: thText(r?.current), suggested: sug });
  }
  const suggestions = (Array.isArray(c.suggestions) ? c.suggestions : []).map(x => {
    const gain = num(x?.oos_edge);
    return `${RULE_LABEL[String(x?.rule)] ?? String(x?.rule ?? "")}: ${thText(x?.from) ?? "?"} → ${thText(x?.to) ?? "?"}${gain != null ? ` (out-of-sample ${pctText(gain)})` : ""}`;
  });
  const sc = c.score && typeof c.score === "object" ? c.score : null;
  const spread = sc ? num(sc.spread) : null;
  const score = sc && typeof sc.monotonic === "boolean"
    ? (sc.monotonic ? "A higher structure score went with better outcomes" : "The structure score did not rank outcomes cleanly") + (spread != null ? ` (top vs bottom third ${pctText(spread)})` : "")
    : null;
  const note = rules.length === 0 ? "No rule has turned anything away yet." : null;
  return { available: true, headline, rules, score, suggestions, note, error: null };
}

// ── group356: chart overlay (StockChart) built from the gateway's market_structure block ──────────────────────────────────────────
export interface OverlayLine { y: number; label: string; kind: "stop" | "target" | "support" | "resistance" }
export interface OverlayZone { y1: number; y2: number; label: string; kind: "demand" | "imbalance" }
export interface ChartOverlay { lines: OverlayLine[]; zones: OverlayZone[]; empty: boolean }

/** Structural stop / target, the two nearest supports and resistances, and the active demand / imbalance zone, from `market_structure`.
 *  Only levels within `spanPct` of the last close are kept so a far-away level cannot squash the price axis. Never throws. */
export function chartOverlay(block: any, spanPct = 25): ChartOverlay {
  const out: ChartOverlay = { lines: [], zones: [], empty: true };
  if (!block || typeof block !== "object" || !["ok", "limited"].includes(block.quality)) return out;
  const last = num(block.last_close);
  const near = (y: number | null): y is number => y != null && y > 0 && (last == null || Math.abs(y - last) / last * 100 <= spanPct);
  const rr = block.structure_rr || {};
  const stop = num(rr.stop), target = num(rr.target);
  if (near(stop)) out.lines.push({ y: stop, label: `Structural stop ₹${stop.toFixed(2)}`, kind: "stop" });
  if (near(target)) out.lines.push({ y: target, label: `Structural target ₹${target.toFixed(2)}`, kind: "target" });
  const lv = block.levels || {};
  for (const [key, kind, word] of [["supports", "support", "Support"], ["resistances", "resistance", "Resistance"]] as const) {
    for (const z of (Array.isArray(lv[key]) ? lv[key] : []).slice(0, 2)) {
      const y = num(kind === "support" ? z?.price_high : z?.price_low);
      if (near(y)) out.lines.push({ y, label: `${word} ₹${y.toFixed(2)}`, kind });
    }
  }
  for (const [z, kind, word] of [[block.order_block, "demand", "Demand zone"], [block.fvg, "imbalance", "Imbalance zone"]] as const) {
    const top = num(z?.top), bottom = num(z?.bottom);
    if (top != null && bottom != null && near(top) && near(bottom)) out.zones.push({ y1: bottom, y2: top, label: `${word} ${z?.status ?? ""}`.trim(), kind });
  }
  out.empty = out.lines.length === 0 && out.zones.length === 0;
  return out;
}

// ── group357 (WS-4): the structure record on a Buy Sniper card (buy_sniper.py `structure`) ──────────────────────────────────────────
export interface SniperStructure {
  gate_mode?: string; opinion?: string; would_reject?: boolean; enforced?: boolean; reasons?: string[]; no_data?: string | null;
  live?: StructureLive; stop?: number | null; target?: number | null; rr?: number | null;
}
export interface SniperStructureView { label: string; tone: Tone; stop: string | null; target: string | null; rr: string | null; reasons: string[] }

/** Plain-word view of a sniper card's structure record; null when the gate was off / the card carries none. Shadow is never described as applied. */
export function sniperStructureView(st: SniperStructure | null | undefined): SniperStructureView | null {
  if (!st || typeof st !== "object") return null;
  const inr = (v: unknown) => { const n = num(v); return n != null && n > 0 ? `₹${n.toFixed(2)}` : null; };
  const rr = num(st.rr) ?? num(st.live?.rr);
  const reasons = Array.isArray(st.reasons) ? st.reasons.map(String) : [];
  let label: string, tone: Tone;
  if (st.opinion !== "ok") { label = "No structure read for this stock"; tone = "mute"; }
  else if (!st.would_reject) { label = "Daily structure supports the entry"; tone = "good"; }
  else if (st.enforced) { label = "Blocked by daily structure"; tone = "bad"; }
  else { label = "Would be blocked — shadow, not applied"; tone = "warn"; }
  return { label, tone, stop: inr(st.stop), target: inr(st.target), rr: rr == null ? null : rr.toFixed(2), reasons };
}

/** One line for the modal footer: how many candidates the daily-structure gate turned away. null when none / the gate was off. */
export function sniperRejectedText(mode: unknown, rejected: Array<{ symbol?: string; reasons?: string[] }> | null | undefined): string | null {
  if (mode === "off" || !Array.isArray(rejected) || rejected.length === 0) return null;
  const syms = rejected.map(r => String(r?.symbol ?? "").trim()).filter(Boolean);
  const n = syms.length;
  const shown = syms.slice(0, 3).join(", ") + (n > 3 ? ` +${n - 3} more` : "");
  return `${n} setup${n === 1 ? "" : "s"} turned away by daily structure (good stock, bad entry): ${shown}`;
}

// ── group358: PREPARE-to-Buy refresh notes (api-gateway /data-feed/refresh-prepare-to-buy `structure_notes`) ────────────────────────
export interface PrepareStructureNote { headline?: string | null; turned_bad?: boolean; flipped?: boolean }
export interface PrepareNoteLine { symbol: string; text: string; tone: Tone }

/** The refresh reasons as display lines: structure turned bad first (a flip, i.e. it passed at the old price, leads), then events. Log-only
 *  information: nothing here changes a decision. [] for anything that is not a usable notes object. */
export function prepareNoteLines(notes: unknown, max = 8): PrepareNoteLine[] {
  if (!notes || typeof notes !== "object" || Array.isArray(notes)) return [];
  const out: Array<PrepareNoteLine & { rank: number }> = [];
  for (const [symbol, raw] of Object.entries(notes as Record<string, unknown>)) {
    const n = raw as PrepareStructureNote | null;
    const text = n && typeof n.headline === "string" ? n.headline.trim() : "";
    if (!symbol || !text) continue;
    const bad = n?.turned_bad === true;
    out.push({ symbol, text, tone: bad ? "warn" : "good", rank: bad ? (n?.flipped === true ? 0 : 1) : 2 });
  }
  out.sort((a, b) => a.rank - b.rank || a.symbol.localeCompare(b.symbol));
  return out.slice(0, Math.max(0, max)).map(({ symbol, text, tone }) => ({ symbol, text, tone }));
}

// ── group359: the structure snapshot stored when a paper trade was entered (decision-prediction GET /api/trades `structure`) ──────────
export interface TradeEntryStructure {
  v?: number; mode?: string; applied?: boolean; original_decision?: string | null; policy_decision?: string | null;
  rules?: Array<{ id?: string | null; to?: string | null; reason?: string | null }>; no_opinion?: string | null;
  levels?: { stop?: number | null; target?: number | null; rr?: number | null } | null; features?: Record<string, any> | null; summary_line?: string | null;
}
export interface TradeEntryView { label: string; short: string; tone: Tone; reasons: string[]; levels: string | null; line: string | null; flagged: boolean | null }

/** Plain-word view of the entry snapshot. null / unusable = the trade was opened before snapshots were stored: say so, never invent a read.
 *  `flagged` is true when the policy objected at entry (rules fired), false when it supported the entry, null when there is no opinion. */
export function tradeEntryView(e: TradeEntryStructure | null | undefined): TradeEntryView {
  if (!e || typeof e !== "object" || !e.mode || e.mode === "off") {
    return { label: "No structure snapshot (trade opened before snapshots were stored)", short: "—", tone: "mute", reasons: [], levels: null, line: null, flagged: null };
  }
  const reasons = (Array.isArray(e.rules) ? e.rules : []).map(r => (typeof r?.reason === "string" ? r.reason.trim() : "")).filter(Boolean);
  const l = e.levels && typeof e.levels === "object" ? e.levels : null;
  const parts = [num(l?.stop) != null && num(l!.stop)! > 0 ? `stop ₹${num(l!.stop)!.toFixed(2)}` : null,
                 num(l?.target) != null && num(l!.target)! > 0 ? `target ₹${num(l!.target)!.toFixed(2)}` : null,
                 num(l?.rr) != null ? `R:R ${num(l!.rr)!.toFixed(2)}` : null].filter(Boolean) as string[];
  const levels = parts.length ? `Structural ${parts.join(" · ")}` : null;
  const line = typeof e.summary_line === "string" && e.summary_line.trim() ? e.summary_line.trim() : null;
  const from = e.original_decision || "the call", to = e.policy_decision || "a more cautious call";
  if (e.no_opinion) return { label: `No structure read at entry: ${e.no_opinion}`, short: "no read", tone: "mute", reasons: [], levels, line, flagged: null };
  if (reasons.length === 0) return { label: "Daily structure supported the entry", short: "supported", tone: "good", reasons, levels, line, flagged: false };
  if (e.applied) return { label: `Structure changed ${from} → ${to} at entry`, short: "flagged", tone: "bad", reasons, levels, line, flagged: true };
  return { label: `Structure would have changed ${from} → ${to} at entry (shadow, not applied)`, short: "flagged", tone: "warn", reasons, levels, line, flagged: true };
}

export interface TradeStructureTally {
  closed: number; withSnapshot: number; flagged: number; supported: number; flaggedAvgPct: number | null; supportedAvgPct: number | null; smallSample: boolean;
}
/** How closed trades did, split by what structure said at entry. Only trades with a read (supported / flagged) and a numeric pnl_pct count.
 *  Descriptive, not evidence: `smallSample` stays true until BOTH groups have at least `minEach` trades. */
export function tradeStructureTally(trades: unknown, minEach = 20): TradeStructureTally {
  const t: TradeStructureTally = { closed: 0, withSnapshot: 0, flagged: 0, supported: 0, flaggedAvgPct: null, supportedAvgPct: null, smallSample: true };
  if (!Array.isArray(trades)) return t;
  let fSum = 0, sSum = 0;
  for (const raw of trades) {
    const tr = raw as { status?: string; pnl_pct?: unknown; structure?: TradeEntryStructure | null } | null;
    if (!tr || tr.status !== "CLOSED") continue;
    t.closed++;
    const v = tradeEntryView(tr.structure);
    if (v.flagged == null) continue;
    t.withSnapshot++;
    const p = num(tr.pnl_pct);
    if (p == null) continue;
    if (v.flagged) { t.flagged++; fSum += p; } else { t.supported++; sSum += p; }
  }
  t.flaggedAvgPct = t.flagged ? Math.round(fSum / t.flagged * 100) / 100 : null;
  t.supportedAvgPct = t.supported ? Math.round(sSum / t.supported * 100) / 100 : null;
  t.smallSample = t.flagged < minEach || t.supported < minEach;
  return t;
}
