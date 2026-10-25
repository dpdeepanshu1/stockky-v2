// group 442: the written run plan shown on the Real Money Calibration page. Pure data, no network.
// Job / pack ids are the ones of services/calibration-runner-service/jobs.py (a test, calibration.test.mjs, checks they still exist there).
import type { JobDef, Svc } from "./calibrationApi";

export interface PlanRow { when: string; what: string; pack?: string; jobs?: string[]; note?: string }

export const PLAN: { id: string; title: string; blurb: string; rows: PlanRow[] }[] = [
  {
    id: "monday", title: "Monday (and after any restart or deploy)", blurb: "Before the first trade of the week: is everything up, is every switch what you think it is, are quotes fresh.",
    rows: [
      { when: "08:45 - 09:15 IST", what: "Health of every service, switch snapshot, wiring check, Time Machine gate (scheduled Monday 08:50; Tuesday to Friday the Morning open pack does the same)", pack: "monday_open", note: "Fix any PENDING RESTART line first (restart outside market hours)." },
      { when: "after every deploy", what: "Market Weather wiring check: both services take the stamp, every dial is live, the multipliers match the switches", jobs: ["wiring_check"], note: "WIRING: PASS is the proof; a FAIL line names the missing link. Never touches an overlay that is already active." },
      { when: "09:35 IST (scheduled)", what: "Live check: depth-20 / order feed layout, quote age, then the short shadow and ORB bar counters (T5)", pack: "live_check_open", note: "Runs by itself every trading day and tells Telegram. An exchange holiday is skipped." },
      { when: "Once, 09:30 - 14:30 IST, before SHORT_MODE goes live", what: "Dhan SELL probe (T6): ONE real 1-share SELL Super Order, legs read back, covered. Tick the box to run it", jobs: ["dhan_sell_probe"], note: "PASS is what lets a live short be placed (SHORT_LIVE_REQUIRES_PROBE). FAIL: the 'dhan' lines say what Dhan refused. If the cover cannot be completed it becomes a normal short and Telegram alerts." },
      { when: "10:20 IST (scheduled)", what: "Live check again: the session verdict must read `open`", pack: "live_check_1015" },
      { when: "15:05 IST (scheduled)", what: "T7: no short may be open after the 14:55 cover", pack: "shorts_flat_close", note: "FAIL names the symbol: check Dhan and cover by hand if needed." },
      { when: "15:35 IST (scheduled)", what: "The Market Weather overlay must be gone in both services after 15:30", pack: "overlay_clear_close", note: "FAIL: use 'Revert to saved' on the Market Weather card." },
    ],
  },
  {
    id: "daily", title: "Every trading day", blurb: "Small, fast, and they feed everything else: the records and the ledger are only kept for a few days.",
    rows: [
      { when: "08:50 IST Tue to Fri (scheduled)", what: "Health, switch snapshot, Market Weather wiring check", pack: "morning_open" },
      { when: "09:20 IST (scheduled)", what: "Time Machine morning scan and gate check", pack: "time_machine_open" },
      { when: "09:30 IST", what: "Live check (also scheduled at 09:35; run it by hand only if you changed something, or the day looks odd)", jobs: ["monday_check_0930"] },
      { when: "After 15:45 IST", what: "Daily trades with entry / exit time and P&L, save records, save the position-stocks ledger, score outcomes, structure refresh, TimesFM cache", pack: "daily_close", note: "Retrain, outcome scoring and structure refresh also run by GitHub Actions: running them here too is harmless (the service skips what is already fresh)." },
      { when: "After any switch change", what: "Switch snapshot, then compare tomorrow's daily trades with the days before", jobs: ["control_snapshot", "daily_trades"] },
    ],
  },
  {
    id: "friday", title: "Friday after the close", blurb: "The weekly review of what the real money actually did.",
    rows: [
      { when: "16:40 IST", what: "Closed-trade report, entry slippage review, hold buckets, opening-window verdict, board parity, changed switches", pack: "friday_review", note: "board parity GO two Fridays in a row = the legacy board maths may be retired (scripts/retire_legacy_board.py, dry run first)." },
      { when: "Friday, after the pack", what: "Short selling shadow report (counters agree; net after charges once there is enough data)", jobs: ["short_shadow_report"], note: "NOT_ENOUGH_DATA is normal for the first weeks. It never touches SHORT_MODE." },
      { when: "Friday, after the pack", what: "Short selling bar replay (mirrored short rule on recent 5-minute bars, with a long control)", jobs: ["short_history_replay"], note: "NOT_ENOUGH_DATA means market-data did not serve enough 5-minute history. Evidence only; it gates nothing." },
      { when: "Friday, after the pack", what: "Swing lane shadow report (the screened stocks after the screening day, net of delivery cost, against all the others; 5, 10 or 20 sessions)", jobs: ["swing_shadow_report"], note: "Also part of the Friday review pack. NOT_ENOUGH_DATA is normal: about 30 saved trading days for 5 sessions, 40+ for the 20-session answer (late December 2026). It never touches SWING_MODE." },
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
      { when: "on demand, after fetch_history (not scheduled)", what: "Swing momentum replay: top-ranked stocks held up to 5 / 20 / 40 sessions, net of delivery cost. 25 Oct 2026 result: NO_EDGE at all three, so SWING_MODE stays shadow", jobs: ["swing_universe", "swing_backtest"], note: "swing_universe prints the SWING_UNIVERSE_SYMBOLS and SWING_UNIVERSE_MAX lines for .env. Do not rebuild the runner while a run is active." },
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
  wiring_check: ["rt", "ps"], dhan_sell_probe: ["ps"], short_shadow_report: ["ps"], short_history_replay: ["ps"], opening_replay: ["ps"], board_parity: ["ps"], intraday_gates: ["ps"], p8_retest: ["ps", "rt"], ml_v2: ["rt", "ps"],
  swing_backtest: ["rt"], swing_shadow_report: ["rt"], swing_universe: ["rt"],   // E142: the swing lane lives in real-trade-service only
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
