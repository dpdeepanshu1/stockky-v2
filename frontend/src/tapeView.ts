// frontend/src/tapeView.ts  (E271)
// Pure helpers for the "Order-flow tape" card of the Real Money Calibration page (no runtime imports, no network, never throws).
// Shape of GET /flow/stats on both trading services (entry gate + exit counters since the last restart, market-data's tape status). Covered by tape.test.mjs.

export interface TapeStatusDoc {
  enabled?: boolean; error?: string; symbols?: number; symbols_tracked?: number; reliable?: boolean; reliable_symbols?: number;
  coverage_pct?: number | null; avg_coverage_pct?: number | null; symbols_today?: number | null; unreliable_reasons?: Record<string, number>; rows?: number; intervals?: number; errors?: number; [k: string]: unknown;
}
export interface FlowGateStats { calls: number; blocks: number; unreliable: number; no_tape: number; errors: number; cache_hits: number; mode: string;
  exit: { calls: number; tighten: number; hold: number; errors: number; applied?: number; exit_mode: string } }
export interface FlowStatsDoc { since_restart: boolean; service: string; gate: FlowGateStats | null; tape: TapeStatusDoc | null; min_against?: number | null; exit_min_against?: number | null }

export type Tone = "good" | "warn" | "bad" | undefined;
const isNum = (v: unknown): v is number => typeof v === "number" && isFinite(v);
const n0 = (v: unknown): number => (isNum(v) ? v : 0);

export interface TapeView {
  kind: "unavailable" | "off" | "warming" | "ok";
  tone: Tone;
  headline: string;
  health: { enabled: boolean | null; reliable: boolean | null; coverage: number | null; symbols: number | null };
  entry: { mode: string; calls: number; blocks: number; unreliable: number; noTape: number; errors: number; blockPct: number | null };
  exit: { mode: string; calls: number; tighten: number; applied: number; hold: number; errors: number };
  warnings: string[];
}

/** Coverage the tape needs before it is trusted (market-data FLOW_MIN_COVERAGE_PCT default). */
export const MIN_COVERAGE_PCT = 35;

export function tapeView(doc: FlowStatsDoc | null | undefined): TapeView {
  const empty: TapeView = {
    kind: "unavailable", tone: "warn", headline: "Tape numbers are not available.",
    health: { enabled: null, reliable: null, coverage: null, symbols: null },
    entry: { mode: "-", calls: 0, blocks: 0, unreliable: 0, noTape: 0, errors: 0, blockPct: null },
    exit: { mode: "-", calls: 0, tighten: 0, applied: 0, hold: 0, errors: 0 }, warnings: [],
  };
  if (!doc || typeof doc !== "object") return empty;
  const g = doc.gate, t = doc.tape;
  const coverage = t ? (isNum(t.coverage_pct) ? t.coverage_pct : isNum(t.avg_coverage_pct) ? t.avg_coverage_pct : null) : null;
  const reliable = t ? (typeof t.reliable === "boolean" ? t.reliable : isNum(t.reliable_symbols) ? t.reliable_symbols > 0 : null) : null;
  const symbols = t ? (isNum(t.symbols_today) ? t.symbols_today : isNum(t.symbols_tracked) ? t.symbols_tracked : isNum(t.symbols) ? t.symbols : null) : null;
  const enabled = t ? (typeof t.enabled === "boolean" ? t.enabled : null) : null;
  const calls = n0(g?.calls), blocks = n0(g?.blocks);
  const entry = { mode: g?.mode ?? "-", calls, blocks, unreliable: n0(g?.unreliable), noTape: n0(g?.no_tape), errors: n0(g?.errors),
    blockPct: calls > 0 ? Math.round((blocks / calls) * 1000) / 10 : null };
  const x = g?.exit;
  const exit = { mode: x?.exit_mode ?? "-", calls: n0(x?.calls), tighten: n0(x?.tighten), applied: n0(x?.applied), hold: n0(x?.hold), errors: n0(x?.errors) };
  const warnings: string[] = [];
  if (!t) warnings.push("market-data did not answer the tape status call");
  else if (t.error) warnings.push(`tape status error: ${t.error}`);
  if (g && entry.calls > 0 && entry.noTape + entry.unreliable >= entry.calls * 0.5) warnings.push("more than half of the entry checks had no usable tape, so the gate was mostly giving no opinion");
  if (entry.errors > 0) warnings.push(`${entry.errors} entry call error${entry.errors === 1 ? "" : "s"} (the entry went on each time)`);
  if (exit.errors > 0) warnings.push(`${exit.errors} exit call error${exit.errors === 1 ? "" : "s"} (the trade was left alone each time)`);
  if (exit.tighten > 0 && exit.applied === 0 && exit.mode === "live") warnings.push("the tape asked to tighten but nothing was applied (a broker refusal or an unconfirmed stop change; see the TAPE_EXIT log lines)");
  let kind: TapeView["kind"] = "ok", tone: Tone = "good", headline: string;
  if (entry.mode === "off" && exit.mode === "off") { kind = "off"; tone = "warn"; headline = "Tape gate and tape exits are both off."; }
  else if (enabled === false) { kind = "off"; tone = "warn"; headline = "The tape is switched off in market-data (FLOW_TAPE_ENABLED)."; }
  else if (reliable === false || (coverage !== null && coverage < MIN_COVERAGE_PCT)) {
    kind = "warming"; tone = "warn";
    headline = `The tape is not reliable yet${coverage !== null ? ` (coverage ${coverage}%, needs ${MIN_COVERAGE_PCT}%)` : ""}; entry and exit checks give no opinion until it is.`;
  } else {
    headline = `Tape reliable${coverage !== null ? ` (coverage ${coverage}%)` : ""}. Entry: ${entry.blocks} refused of ${entry.calls} checks. Exit: ${exit.applied} trade${exit.applied === 1 ? "" : "s"} tightened.`;
  }
  if (warnings.length && kind === "ok") tone = "warn";
  return { kind, tone, headline, health: { enabled, reliable, coverage, symbols }, entry, exit, warnings };
}
