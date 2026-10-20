// group 442: the written run plan shown on the Real Money Calibration page. Pure data, no network.
// Job / pack ids are the ones of services/calibration-runner-service/jobs.py (a test, calibration.test.mjs, checks they still exist there).
import type { JobDef, Svc } from "./calibrationApi";

export interface PlanRow { when: string; what: string; pack?: string; jobs?: string[]; note?: string }

export const PLAN: { id: string; title: string; blurb: string; rows: PlanRow[] }[] = [
  {
    id: "monday", title: "Monday (and after any restart or deploy)", blurb: "Before the first trade of the week: is everything up, is every switch what you think it is, are quotes fresh.",
    rows: [
      { when: "08:45 - 09:15 IST", what: "Health of every service, switch snapshot, 09:30 live check", pack: "monday_open", note: "Fix any PENDING RESTART line first (restart outside market hours)." },
      { when: "09:30 IST", what: "Live check: depth-20 / order feed layout, quote age", jobs: ["monday_check_0930"] },
      { when: "10:15 IST", what: "Live check again: the session verdict must read `open`", jobs: ["monday_check_1015"] },
    ],
  },
  {
    id: "daily", title: "Every trading day", blurb: "Small, fast, and they feed everything else: the records and the ledger are only kept for a few days.",
    rows: [
      { when: "09:30 IST", what: "Live check (only if you changed something, or the day looks odd)", jobs: ["monday_check_0930"] },
      { when: "After 15:45 IST", what: "Daily trades with entry / exit time and P&L, save records, save the position-stocks ledger, score outcomes, structure refresh, TimesFM cache", pack: "daily_close", note: "Retrain, outcome scoring and structure refresh also run by GitHub Actions: running them here too is harmless (the service skips what is already fresh)." },
      { when: "After any switch change", what: "Switch snapshot, then compare tomorrow's daily trades with the days before", jobs: ["control_snapshot", "daily_trades"] },
    ],
  },
  {
    id: "friday", title: "Friday after the close", blurb: "The weekly review of what the real money actually did.",
    rows: [
      { when: "16:40 IST", what: "Closed-trade report, entry slippage review, hold buckets, opening-window verdict, board parity, changed switches", pack: "friday_review", note: "board parity GO two Fridays in a row = the legacy board maths may be retired (scripts/retire_legacy_board.py, dry run first)." },
    ],
  },
  {
    id: "saturday", title: "Saturday morning (market closed)", blurb: "The heavy evidence run. Needs the machine quiet: it is refused during market hours unless you confirm.",
    rows: [
      { when: "08:00 IST", what: "Full calibration: history for 3 seeds, A/B replay, G1, walk-forward, G2 / G3 / G4, env-line pack, board parity, then a switches + metrics snapshot and its comparison with the last run", pack: "weekly_calibration", note: "1 to 3 hours. A seed whose download mostly failed is not counted (calibration_status says which to re-run)." },
      { when: "after it", what: "G2 / G3 / G4 verdict on every complete seed", jobs: ["gate_check"] },
      { when: "after it, and before / after any settings change", what: "Snapshot the switches + A/B metrics, then compare the last two snapshots", jobs: ["calibration_snapshot", "calibration_compare"], note: "The weekly pack already does both. Changes nothing." },
    ],
  },
  {
    id: "monthly", title: "First Saturday of the month", blurb: "Evidence for switches that are still off or log-only. None of it changes a setting.",
    rows: [
      { when: "13:00 IST", what: "Legacy gates, entry rules, P8 retest, ML V2, intraday gates, TimesFM probe, Kronos backtest", pack: "monthly_evidence", note: "Needs the weekly calibration's history on disk and, for Kronos / TimesFM, those containers running." },
      { when: "when you re-test G1", what: "Save the /analyze latency baseline with STRUCTURE_ENABLED=0, switch it back on, then compare (p95 within 15%)", jobs: ["latency_save", "latency_compare"] },
    ],
  },
];

export const AUTOMATIC_ELSEWHERE = [
  { what: "Model retrain", when: "00:30 IST every night", how: "GitHub Actions: Retrain Prediction Model (pred_train.py). The Train button here starts an extra run on the VM's training service." },
  { what: "Outcome scoring (T+1 / T+5 / structure)", when: "after the close", how: "GitHub Actions: evaluate-outcomes." },
  { what: "Structure refresh", when: "after the close", how: "GitHub Actions: structure-refresh." },
];

/** Which service a job mostly concerns. Anything not listed shows on both tabs. */
export const JOB_SERVICES: Record<string, Svc[]> = {
  daily_trades: ["rt", "ps"], export_records: ["rt"], records_report: ["rt"], entry_slippage: ["rt"], hold_bucket: ["rt"], strategy_readout: ["rt", "ps"], legacy_gate: ["rt"], entry_rules: ["rt"],
  kronos_backtest: ["rt"], tsfm_nightly: ["rt"], tsfm_probe: ["rt"], tsfm_combine: ["rt"], latency_save: ["rt", "ps"], latency_compare: ["rt", "ps"],
  opening_replay: ["ps"], board_parity: ["ps"], intraday_gates: ["ps"], p8_retest: ["ps", "rt"], ml_v2: ["rt", "ps"],
};

export function jobAppliesTo(id: string, svc: Svc): boolean {
  const s = JOB_SERVICES[id];
  return !s || s.includes(svc);
}

/** What the gates called in the brief map to in this repo. G5 to G8 and "F3" do not exist: the page says so instead of inventing them. */
export const GATE_GLOSSARY: { gate: string; means: string; job: string; exists: boolean }[] = [
  { gate: "G1", means: "Structure engine is fast enough (offline time budget, and live /analyze p95 within 15%)", job: "g1_check / latency_save + latency_compare", exists: true },
  { gate: "G2", means: "Daily-structure policy beats a technical-only baseline out of sample, after costs", job: "ab_report -> gate_check", exists: true },
  { gate: "G3", means: "That answer is stable: every seed, both history halves, every regime, no rule that hurts", job: "gate_check", exists: true },
  { gate: "G4", means: "Live observations agree (20+ sessions, 30+ scored rows per group)", job: "gate_check (live structure report)", exists: true },
  { gate: "Gate 4f", means: "Kronos forecast gate: would it have helped?", job: "kronos_backtest", exists: true },
  { gate: "Gate 4g / A", means: "TimesFM range / volume forecast beats ATR / HAR out of sample", job: "tsfm_probe + tsfm_combine", exists: true },
  { gate: "A/B board", means: "Engine board maths = legacy board maths, so the legacy code may go", job: "board_parity", exists: true },
  { gate: "G5 - G8, F3", means: "Not defined anywhere in this repo. Nothing is run or claimed for them.", job: "-", exists: false },
];

/** group 471: which jobs "Run all test cases" starts for one service tab, in a safe order.
 *  - the "model" group (retrain / score outcomes / structure refresh) is NOT a test: it changes data, so it is never part of "run all";
 *  - heavy jobs (hours of replay / downloads, held back in market hours) only when asked for;
 *  - a job runs after the jobs it needs (export_records before records_report ...), otherwise the catalog order is kept. */
export function planRunAll(jobs: Pick<JobDef, "id" | "group" | "heavy" | "needs">[], svc: Svc, includeHeavy: boolean): { ids: string[]; heavySkipped: string[] } {
  const heavySkipped: string[] = [];
  const picked = jobs.filter((j) => j.group !== "model" && jobAppliesTo(j.id, svc) && (includeHeavy || !j.heavy || (heavySkipped.push(j.id), false)));
  const inSet = new Set(picked.map((j) => j.id));
  const out: string[] = [];
  const seen = new Set<string>();
  const visit = (j: Pick<JobDef, "id" | "needs">) => {
    if (seen.has(j.id)) return;
    seen.add(j.id);
    for (const n of j.needs || []) { const d = picked.find((x) => x.id === n); if (d && inSet.has(n)) visit(d); }
    out.push(j.id);
  };
  picked.forEach(visit);
  return { ids: out, heavySkipped };
}
