// frontend/src/timeMachineView.ts  (E111)
// Pure helpers for the Position Stocks "Time Machine" tab (no imports, no network). Shapes match POST /time-machine/run, /time-machine/compare and
// GET /time-machine/status on position-stocks-service (timemachine/service.py). EVIDENCE ONLY: nothing here places, sizes or blocks a trade.
// Covered by timeMachine.test.mjs.

export type TmSide = "LONG" | "SHORT";
export type TmVerdictCode = "FAVOURS" | "AGAINST" | "NEAR_BREAKEVEN" | "MIXED" | "NOT_ENOUGH_DATA";

export interface TmForm {
  symbol: string; side: TmSide; stopPct: string; targetPct: string; entryTime: string; flatBy: string;
  valueRs: string; sims: string; lookback: "1mo" | "2mo"; compareSymbols: string;
}
export interface TmSummary {
  n: number; sessions?: number; target?: number; stop?: number; flat?: number; p_target?: number; p_stop?: number; p_flat?: number;
  ci_low?: number | null; ci_high?: number | null; avg_r_gross?: number; avg_r_net?: number; avg_net_pct?: number; win_rate_net?: number;
}
export interface TmHistBin { lo: number; hi: number; n: number; share: number }
export interface TmHistoryRow { day: string; entry: string; outcome: string; exit: string; gross_pct: number; net_pct: number; r_net: number; r_gross: number }
export interface TmTableRow { time: string; is_selected: boolean; sim_p_target: number | null; sim_avg_r_net: number | null; hist_n: number; hist_p_target: number | null; hist_avg_r_net: number | null }
export interface TmRunDoc {
  ok: boolean; symbol: string;
  params: { side: TmSide; stop_pct: number; target_pct: number; rr: number; entry_time: string; flat_by: string; sims: number; seed: number; slippage_pct: number; value_rs: number; lookback: string };
  data: { sessions: number; first_day: string; last_day: string; last_close: number; lookback_used: string; cached: boolean; window_drift_pct: number | null; source: string };
  charges_pct: Record<string, number>; breakeven: { win_gross: number; win_net: number };
  history: TmSummary; history_rows: TmHistoryRow[];
  simulation: TmSummary & { r_spread: Record<string, number | null>; r_histogram: TmHistBin[] };
  history_vs_simulation_pts: number | null;
  control: { side: TmSide; simulation: TmSummary; history: TmSummary };
  verdict: { code: TmVerdictCode; line: string };
  by_entry_time: TmTableRow[]; warnings: string[]; limits: string[]; elapsed_ms: number;
}
export interface TmCompareRow { symbol: string; verdict: TmVerdictCode; sim_p_target: number | null; sim_avg_r_net: number | null; hist_n: number; hist_p_target: number | null; hist_avg_r_net: number | null; control_sim_avg_r_net: number | null; sessions: number; last_close: number; window_drift_pct: number | null }
export interface TmCompareDoc { ok: boolean; rows: TmCompareRow[]; failed: { symbol: string; code: string; error: string }[]; params: Record<string, any>; limits: string[] }
export interface TmStatusDoc { enabled: boolean; sims_default: number; slippage_pct: number; lookback: string; default_value_rs: number; defaults: { side: TmSide; stop_pct: number; target_pct: number; entry_time: string; flat_by_long: string; flat_by_short: string }; max_compare_symbols: number; recent: { at: number; symbol: string; side: string; verdict: string; ms: number }[] }

export const MAX_COMPARE = 15;
const SYMBOL_RE = /^[A-Z0-9&-]{1,20}$/;
const TIME_RE = /^([01]\d|2[0-3]):([0-5]\d)$/;

export function defaultForm(st?: TmStatusDoc | null): TmForm {
  const d = st?.defaults;
  return {
    symbol: "", side: d?.side ?? "LONG", stopPct: String(d?.stop_pct ?? 1), targetPct: String(d?.target_pct ?? 2), entryTime: d?.entry_time ?? "10:00",
    flatBy: "", valueRs: String(st?.default_value_rs ?? 20000), sims: String(st?.sims_default ?? 10000), lookback: (st?.lookback === "2mo" ? "2mo" : "1mo"), compareSymbols: "",
  };
}

/** "reliance.ns, tcs ,TCS" -> ["RELIANCE","TCS"] (upper-case, .NS/.BO dropped, duplicates removed, invalid names dropped). */
export function parseSymbols(text: string): string[] {
  const out: string[] = [];
  for (const raw of String(text || "").split(/[\s,;]+/)) {
    const s = raw.trim().toUpperCase().replace(/\.(NS|BO)$/, "");
    if (SYMBOL_RE.test(s) && !out.includes(s)) out.push(s);
  }
  return out;
}

const mins = (t: string) => { const m = TIME_RE.exec(t.trim()); return m ? Number(m[1]) * 60 + Number(m[2]) : null; };

/** The first thing wrong with the form (a sentence), or null when it can be sent. Mirrors the server's own limits so a bad value never costs a round trip. */
export function formProblem(f: TmForm, mode: "run" | "compare"): string | null {
  if (mode === "run") { if (!SYMBOL_RE.test(f.symbol.trim().toUpperCase().replace(/\.(NS|BO)$/, ""))) return "Type one NSE symbol, for example RELIANCE."; }
  else {
    const n = parseSymbols(f.compareSymbols).length;
    if (n < 2) return "Type at least two symbols to compare, separated by commas.";
    if (n > MAX_COMPARE) return `Compare takes at most ${MAX_COMPARE} stocks.`;
  }
  const stop = Number(f.stopPct), target = Number(f.targetPct);
  if (!(stop >= 0.1 && stop <= 10)) return "Stop % must be between 0.1 and 10.";
  if (!(target >= 0.1 && target <= 30)) return "Target % must be between 0.1 and 30.";
  const e = mins(f.entryTime);
  if (e === null) return "Entry time must look like 10:00.";
  if (e < 555 || (e - 555) % 5 !== 0) return "Entry time must be a 5-minute mark from 09:15.";
  if (f.flatBy.trim()) {
    const fl = mins(f.flatBy);
    if (fl === null || fl <= 560 || fl > 930) return "Flat-by time must look like 15:00, between 09:25 and 15:30.";
    if (e >= fl) return "Entry time must be before the flat-by time.";
  } else if (e >= (f.side === "SHORT" ? 14 * 60 + 55 : 15 * 60)) return "Entry time must be before the square-off (14:55 for a short, 15:00 for a long).";
  const v = Number(f.valueRs);
  if (!(v >= 1000 && v <= 10_000_000)) return "Trade value must be between 1,000 and 1,00,00,000 rupees.";
  const s = Number(f.sims);
  if (!(Number.isInteger(s) && s >= 1000 && s <= 50000)) return "Made-up sessions must be a whole number from 1,000 to 50,000.";
  return null;
}

/** The JSON body for POST /time-machine/run (or /compare). Empty optional fields are left out so the server's defaults apply. */
export function buildBody(f: TmForm, mode: "run" | "compare"): Record<string, unknown> {
  const body: Record<string, unknown> = {
    side: f.side, stop_pct: Number(f.stopPct), target_pct: Number(f.targetPct), entry_time: f.entryTime.trim(), value_rs: Number(f.valueRs),
    sims: Number(f.sims), lookback: f.lookback,
  };
  if (f.flatBy.trim()) body.flat_by = f.flatBy.trim();
  if (mode === "run") body.symbol = f.symbol.trim().toUpperCase().replace(/\.(NS|BO)$/, "");
  else body.symbols = parseSymbols(f.compareSymbols);
  return body;
}

/** The server answers errors as {"code","error"} (psRequest turns that into JSON text). Gives the sentence, or the text as it came. */
export function errorText(e: unknown): string {
  const raw = String((e as any)?.message ?? e ?? "");
  try { const j = JSON.parse(raw); if (j && typeof j === "object" && j.error) return String(j.error); } catch { /* plain text */ }
  return raw || "Time Machine failed";
}

export function verdictView(code: string | undefined): { tone: "good" | "bad" | "warn" | "mute"; label: string } {
  switch (code) {
    case "FAVOURS": return { tone: "good", label: "Odds favour it" };
    case "AGAINST": return { tone: "bad", label: "Odds are against it" };
    case "NEAR_BREAKEVEN": return { tone: "warn", label: "Near break-even" };
    case "MIXED": return { tone: "warn", label: "Real history and simulation disagree" };
    case "NOT_ENOUGH_DATA": return { tone: "mute", label: "Not enough data" };
    default: return { tone: "mute", label: "No verdict" };
  }
}

export const pct = (v: number | null | undefined, d = 0): string => (typeof v === "number" && isFinite(v) ? `${(v * 100).toFixed(d)}%` : "-");
export const rText = (v: number | null | undefined): string => (typeof v === "number" && isFinite(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(2)} R` : "-");
export const toneOfR = (v: number | null | undefined, band = 0.05): "good" | "bad" | "warn" | "mute" =>
  typeof v !== "number" || !isFinite(v) ? "mute" : v > band ? "good" : v < -band ? "bad" : "warn";

/** Bars for the net-R histogram: width as a share of the tallest bin (0-100) and whether the bin sits at a stop (-1 R or worse) or a target (the RR level or better). */
export function histBars(bins: TmHistBin[] | undefined, rr: number): { label: string; n: number; widthPct: number; kind: "stop" | "target" | "mid" }[] {
  if (!bins || !bins.length) return [];
  const max = Math.max(...bins.map((b) => b.n), 1);
  return bins.map((b) => ({
    label: `${b.lo.toFixed(2)} to ${b.hi.toFixed(2)}`, n: b.n, widthPct: Math.round((b.n / max) * 100),
    kind: b.hi <= -0.9 ? "stop" : b.lo >= rr - 0.5 ? "target" : "mid",
  }));
}

/** One plain line under the verdict about how far the real history sits from the simulation (percentage points of "target first"). */
export function gapLine(pts: number | null | undefined): string {
  if (typeof pts !== "number" || !isFinite(pts)) return "No real history at this entry time to compare with.";
  if (Math.abs(pts) < 5) return `Real history is within ${Math.abs(pts).toFixed(0)} points of the simulation: mostly the stock's volatility and the break-even arithmetic.`;
  return pts > 0
    ? `Real history reached the target ${pts.toFixed(0)} points more often than the simulation: the stock recently trended in the trade's favour (or got lucky).`
    : `Real history reached the target ${Math.abs(pts).toFixed(0)} points less often than the simulation: the stock recently reversed against this rule.`;
}

/** The control line: the opposite side on the same made-up sessions. If it earns and this side does not, the window rose (or fell), not the rule failed. */
export function controlLine(doc: Pick<TmRunDoc, "control" | "simulation" | "params"> | null | undefined): string {
  if (!doc) return "";
  const mine = doc.simulation.avg_r_net, other = doc.control.simulation?.avg_r_net;
  if (typeof mine !== "number" || typeof other !== "number") return "";
  const side = doc.control.side;
  if (mine <= 0 && other > 0.05) return `The ${side} mirror on the same made-up sessions earns ${rText(other)}: a drifting window, not proof that this side is bad.`;
  return `The ${side} mirror on the same made-up sessions: ${rText(other)} (this side ${rText(mine)}).`;
}
