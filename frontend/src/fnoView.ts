// frontend/src/fnoView.ts - pure helpers of the F&O pages (no network, no React). Covered by fnoView.test.mjs.
import type { FnoPosition, FnoLocks, FnoReadiness, FnoReadinessItem, FnoStage, FnoProbeDoc } from "./fnoApi";

export const IST_OFFSET_MIN = 330;
/** epoch seconds -> "YYYY-MM-DD" in IST (whatever the browser's timezone is). */
export function istDay(ts: number | null | undefined): string {
  if (!ts) return "-";
  return new Date(ts * 1000 + IST_OFFSET_MIN * 60000).toISOString().slice(0, 10);
}
export function istHm(ts: number | null | undefined): string {
  if (!ts) return "-";
  return new Date(ts * 1000 + IST_OFFSET_MIN * 60000).toISOString().slice(11, 16);
}
/** Rupees. Round 340 (E342): an amount that rounds to zero ("-0.4" at 0 decimals) is shown as ₹0, not "-₹0". */
export function inr(v: number | null | undefined, dp = 0): string {
  if (v == null || Number.isNaN(v)) return "-";
  const body = Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp });
  const zero = Number(body.replace(/,/g, "")) === 0;
  return `${v < 0 && !zero ? "-" : ""}₹${body}`;
}
export const signed = (v: number | null | undefined, dp = 2): string => (v == null || Number.isNaN(v) ? "-" : `${v > 0 ? "+" : ""}${v.toFixed(dp)}`);
export const pctOf = (a: number, b: number): number | null => (b ? ((a - b) / b) * 100 : null);

/** The one-line plain-language state of the four REAL locks (nothing is ever sent unless all four are open). */
export function lockSummary(l: FnoLocks | null | undefined): { tone: "off" | "demo" | "real-blocked" | "real"; text: string } {
  if (!l) return { tone: "off", text: "Waiting for the service" };
  if (!l.engine_enabled) return { tone: "off", text: "Engine OFF: no signals, no trades" };
  if (l.mode !== "REAL") return { tone: "demo", text: "DEMO: virtual trades only" };
  if (l.real_orders_possible) return { tone: "real", text: "REAL: orders go to Dhan today" };
  return { tone: "real-blocked", text: `REAL mode, but blocked: ${l.closed_locks.join(", ") || "a lock is closed"}` };
}

/** Rows for the open-position cards: where the premium sits between stop and target (0..100), and the P&L in rupees. */
export function positionProgress(p: FnoPosition): { pct: number; pnl: number | null } {
  const prem = p.premium ?? null;
  const span = p.target_price - p.sl_price;
  const pct = prem == null || span <= 0 ? 0 : Math.max(0, Math.min(100, ((prem - p.sl_price) / span) * 100));
  const pnl = prem == null ? (p.unrealized ?? null) : (prem - p.entry_premium) * p.lots * p.lot_size;
  return { pct: Math.round(pct), pnl: pnl == null ? null : Math.round(pnl * 100) / 100 };
}

/** Round 289: an exit SELL whose call failed (network / HTTP 5xx) may be live at Dhan. The engine then sends no second SELL and no stop until Dhan's positions settle it (round 282).
 *  Null = nothing in doubt. `limitPx` is the price the SELL was sent at; `correlationId` is what to look for in the Dhan app. Reads the overview row only: it places nothing. */
export function exitDoubtView(p: FnoPosition): { correlationId: string; reason: string; limitPx: number | null; text: string } | null {
  const ids = p.broker_order_ids || {};
  if (!p.exit_doubt && !ids.exit_doubt) return null;
  const correlationId = String(ids.exit_doubt || "?");
  const reason = String(ids.exit_doubt_reason || "exit");
  const px = Number(ids.exit_doubt_px);
  const limitPx = Number.isFinite(px) && ids.exit_doubt_px !== undefined && ids.exit_doubt_px !== "" ? px : null;
  const text = `The ${reason} SELL for ${p.label} failed with a network / server error and may still be live at Dhan` + (limitPx != null ? ` (limit ${limitPx.toFixed(2)})` : "") +
    `. The engine sends no second SELL and no stop until Dhan's positions settle it. Open the Dhan app` + (correlationId !== "?" ? ` and look for correlationId ${correlationId}` : "") +
    `: if the SELL is there, leave this alone or close it in Dhan; only if it is NOT there, clear the mark so the engine may sell again.`;
  return { correlationId, reason, limitPx, text };
}

/** Round 292: a stop-loss order whose call failed (network / HTTP 5xx) may be resting at Dhan. The engine places no second stop (two could sell twice and leave a short) and asks Dhan for the order by the
 *  correlationId it sent, every cycle. Null = nothing in doubt. The button only matters when that lookup cannot answer: it places nothing. */
export function stopDoubtView(p: FnoPosition): { correlationId: string; text: string } | null {
  const ids = p.broker_order_ids || {};
  if (!p.stop_doubt && !ids.sl_doubt) return null;
  const correlationId = String(ids.sl_doubt || "?");
  const text = `The broker-side stop for ${p.label} failed with a network / server error and may be resting at Dhan` + (correlationId !== "?" ? ` (correlationId ${correlationId})` : "") +
    `. The engine asks Dhan about it every cycle and places no second stop meanwhile; the software stop keeps watching the premium. If Dhan's order lookup cannot answer, look in the Dhan app: ` +
    `if a stop for this contract is there, leave it; only if none is there, clear the mark so the engine may place a stop again.`;
  return { correlationId, text };
}

/** How many of these positions have a stop order in doubt. */
export function stopDoubtCount(ps: FnoPosition[] | undefined | null): number {
  return (ps || []).filter((p) => stopDoubtView(p) !== null).length;
}

/** Round 293: an entry whose fill is not known (the BUY call failed with a network / 5xx error, or its state could not be read after the cancel). The engine keeps the row flagged: no stop, never sold,
 *  no second BUY; it asks Dhan by the correlationId each cycle and settles it by itself. Nothing to click: it places and clears nothing. Null = nothing unknown. */
export function entryDoubtView(p: FnoPosition): { correlationId: string | null; text: string } | null {
  const ids = p.broker_order_ids || {};
  if (ids.entry_state !== "UNKNOWN") return null;
  const correlationId = ids.entry_cid ? String(ids.entry_cid) : null;
  const text = `The entry BUY for ${p.label} may be live or filled at Dhan and its state could not be read yet` + (correlationId ? ` (correlationId ${correlationId})` : "") +
    `. The engine keeps it flagged: no stop, no sale by the engine and no second BUY, and asks Dhan again every cycle. Look in the Dhan app: if you hold the contract, protect or close it there until this settles.`;
  return { correlationId, text };
}

/** How many of these positions have an entry in doubt. */
export function entryDoubtCount(ps: FnoPosition[] | undefined | null): number {
  return (ps || []).filter((p) => entryDoubtView(p) !== null).length;
}

/** Round 290: the warning for settings that cannot work together. Empty list / missing = nothing to show (null). */
export function settingsProblemText(problems: string[] | undefined | null): string | null {
  const list = (problems || []).filter((x) => typeof x === "string" && x.trim());
  if (!list.length) return null;
  return `The engine refuses every new entry until this is fixed: ${list.join("; ")}. Fix it on Real Money Calibration > F&O (or in .env and restart).`;
}

/** How many of these positions have an exit SELL in doubt (the badge on the State card). */
export function exitDoubtCount(ps: FnoPosition[] | undefined | null): number {
  return (ps || []).filter((p) => exitDoubtView(p) !== null).length;
}

/** Day totals of closed positions, grouped by IST day, newest first. Net is used when present, else gross. */
export function dayTotals(ps: FnoPosition[]): { day: string; trades: number; wins: number; net: number }[] {
  const by = new Map<string, { day: string; trades: number; wins: number; net: number }>();
  for (const p of ps) {
    if (p.status !== "CLOSED") continue;
    const d = istDay(p.exit_ts ?? p.entry_ts);
    const net = p.net_pnl ?? p.gross_pnl ?? 0;
    const r = by.get(d) || { day: d, trades: 0, wins: 0, net: 0 };
    r.trades += 1; r.wins += net > 0 ? 1 : 0; r.net = Math.round((r.net + net) * 100) / 100;
    by.set(d, r);
  }
  return [...by.values()].sort((a, b) => (a.day < b.day ? 1 : -1));
}

export const CSV_HEAD = "id,day,underlying,expiry,strike,type,lots,entry_time,entry,exit_time,exit,reason,gross,charges,net,mode";
/** Round 340 (E342): one CSV cell. A comma, quote or line break in a text field (the exit reason is free text) used to shift every column after it; such a cell is now quoted. A text that starts with = + @ gets a leading ' so a spreadsheet does not run it as a formula. Numbers are written as they are. */
export function csvCell(v: unknown): string {
  let s = v == null ? "" : String(v);
  if (typeof v === "string" && /^[=+@]/.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
export function tradesCsv(ps: FnoPosition[]): string {
  const rows = ps.map((p) => [p.id ?? "", istDay(p.entry_ts), p.underlying, p.expiry, p.strike, p.opt_type, p.lots, istHm(p.entry_ts), p.entry_premium, istHm(p.exit_ts), p.exit_premium ?? "", p.exit_reason, p.gross_pnl ?? "", p.charges ?? "", p.net_pnl ?? "", p.mode].map(csvCell).join(","));
  return [CSV_HEAD, ...rows].join("\n");
}

/** Reliability label per backtest kind - the page must never present a SIM result like a replay of recorded data. */
export const BT_RELIABILITY: Record<string, { label: string; text: string }> = {
  fno_replay: { label: "RECORDED", text: "Real recorded chains run through the live engine. Most trustworthy; needs recorded days." },
  fno_eod: { label: "COARSE", text: "Real end-of-day prices only: entries and exits are approximated from the daily range." },
  fno_rolling: { label: "REAL PREMIUMS", text: "Dhan's recorded expired-option candles (5-minute path) with the strategies run on the index candles. Real prices, synthetic spread; the limits are in the result's notes." },
  fno_sim: { label: "SIMULATED", text: "Black-Scholes prices, not real quotes. Shows the logic, not the money." },
};
export function btLabel(kind: string | undefined): { label: string; text: string } {
  const k = (kind || "").toLowerCase();
  return BT_RELIABILITY[k] || BT_RELIABILITY[`fno_${k}`] || { label: "UNKNOWN", text: "Unknown kind of backtest." };   // the service writes kind as SIM / EOD / REPLAY
}

/** One journal event split into the strategy that raised it (a column of its own on the Signals page) and the remaining detail. */
export function splitEvent(e: Record<string, any>): { strategy: string; rest: Record<string, any> } {
  const { id: _id, ts: _ts, kind: _kind, underlying: _u, strategy, ...rest } = e || {};
  const sg = typeof strategy === "string" && strategy ? strategy : "";
  return { strategy: sg, rest };
}

/** Put/call ratio style reading for the chain header. */
export function pcrReading(pcr: number | null | undefined): string {
  if (pcr == null || Number.isNaN(pcr)) return "-";
  return pcr >= 1.3 ? "bullish tilt (many puts written)" : pcr <= 0.7 ? "bearish tilt (many calls written)" : "balanced";
}

// ---- Round 262, slice 3: the readiness checklist (GET /fno/readiness) ----
/** Words, not colour alone: the chip text says what the colour says. */
export function readinessChip(ok: boolean | null | undefined): { tone: "good" | "bad" | "warn"; text: string } {
  return ok === true ? { tone: "good", text: "DONE" } : ok === false ? { tone: "bad", text: "NOT MET" } : { tone: "warn", text: "NOT SHOWN YET" };
}

/** The items in the order the service sent them, grouped by their `group` (first appearance decides the group order). */
export function readinessGroups(items: FnoReadinessItem[] | null | undefined): { group: string; items: FnoReadinessItem[] }[] {
  const out: { group: string; items: FnoReadinessItem[] }[] = [];
  for (const it of Array.isArray(items) ? items : []) {
    let g = out.find((x) => x.group === it.group);
    if (!g) { g = { group: it.group, items: [] }; out.push(g); }
    g.items.push(it);
  }
  return out;
}

/** One plain sentence for the top of the card. It never says "safe": green only means the listed evidence exists. */
export function readinessHeadline(r: FnoReadiness | null | undefined): { tone: "off" | "warn" | "good"; text: string } {
  if (!r) return { tone: "off", text: "Waiting for the service" };
  const n = r.counts.hard_open;
  if (n === 0) return { tone: "good", text: "Every hard item is done. This is evidence the list asked for, not a promise of profit; the other REAL locks still apply." };
  const open = `${n} hard item${n === 1 ? " is" : "s are"} not done yet`;
  return r.real_waits_for_it
    ? { tone: "warn", text: `${open}: REAL orders stay closed until they are.` }
    : { tone: "warn", text: `${open}. The wait is switched OFF, so this list only advises.` };
}

/** The question asked before the person confirms (or withdraws) a check only Dhan can show. */
export function confirmQuestion(it: FnoReadinessItem, ok: boolean): string {
  return ok ? `Confirm: ${it.label}?\n\nOnly press OK if you have checked this yourself in Dhan. Nothing verifies it for you.` : `Withdraw your confirmation: ${it.label}?`;
}

// ---- Round 264, slice 2: the pipeline as ordered stages (GET /fno/pipeline > stages) ----
/** Words next to the colour. idle is neutral, never green: nothing happened there, which proves nothing. */
export function stageChip(status: string | null | undefined): { tone: "good" | "bad" | "neutral"; text: string } {
  return status === "OK" ? { tone: "good", text: "OK" } : status === "PROBLEM" ? { tone: "bad", text: "PROBLEM" } : { tone: "neutral", text: "idle" };
}

/** "20 s ago" / "3 min ago" / "2 h ago"; "no run yet" when the stage has not done anything today. */
export function stageAge(age_s: number | null | undefined): string {
  if (age_s == null || Number.isNaN(age_s)) return "no run yet";
  const s = Math.max(0, Math.round(age_s));
  return s < 90 ? `${s} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}

/** The first stage that has a PROBLEM is where to look first (data flows left to right); null when none. */
export function firstProblem(stages: FnoStage[] | null | undefined): FnoStage | null {
  return (Array.isArray(stages) ? stages : []).find((s) => s.status === "PROBLEM") || null;
}

/** One sentence above the row of boxes. */
export function stageHeadline(stages: FnoStage[] | null | undefined): { tone: "off" | "bad" | "good"; text: string } {
  if (!Array.isArray(stages) || stages.length === 0) return { tone: "off", text: "Stages are not available (the flat checks below still are)." };
  const p = stages.filter((s) => s.status === "PROBLEM");
  if (p.length) return { tone: "bad", text: `${p.length} stage${p.length === 1 ? "" : "s"} with a problem; start with ${p[0].label}: ${p[0].detail}` };
  const ok = stages.filter((s) => s.status === "OK").length;
  return { tone: "good", text: `No stage reports a problem (${ok} OK, ${stages.length - ok} idle). Idle means nothing happened there today, not that it works.` };
}

// ---- Round 265: the real-order probe result and its charges check (GET /fno/probe) ----
export const PROBE_LINES = [["brokerage", "Brokerage"], ["stt", "STT"], ["exchange", "Exchange transaction"], ["sebi", "SEBI fee"], ["gst", "GST"], ["stamp", "Stamp duty"], ["total", "Total (optional)"]] as const;

/** Words next to the colour. MATCHED is one trade's evidence, never "the model is right". */
export function probeVerdictChip(v: string | null | undefined): { tone: "good" | "bad" | "warn" | "neutral"; text: string } {
  return v === "MATCHED" ? { tone: "good", text: "MATCHED" } : v === "DIFFERS" ? { tone: "bad", text: "DIFFERS" } : v === "INCOMPLETE" ? { tone: "warn", text: "INCOMPLETE" }
    : v === "NO_NOTE" ? { tone: "warn", text: "NO NOTE YET" } : { tone: "neutral", text: "NOTHING TO COMPARE" };
}

/** What the box fields hold -> the numbers to send. Blank fields are left out; "Rs 1,234.50" is read; text or a negative number is an error and nothing is sent. */
export function parseNoteInputs(raw: Record<string, string>): { lines: Record<string, number>; errors: string[] } {
  const lines: Record<string, number> = {};
  const errors: string[] = [];
  for (const [k, label] of PROBE_LINES) {
    const t = String(raw?.[k] ?? "").replace(/[₹,]|rs\.?/gi, "").trim();
    if (!t) continue;
    const n = Number(t);
    if (!Number.isFinite(n) || n < 0) errors.push(`${label}: "${raw[k]}" is not an amount in rupees`);
    else lines[k] = n;
  }
  return { lines, errors };
}

/** One sentence for the top of the probe card. */
export function probeHeadline(d: FnoProbeDoc | null | undefined): { tone: "off" | "warn" | "bad" | "good"; text: string } {
  if (!d) return { tone: "off", text: "Waiting for the service" };
  if (!d.probe) return { tone: "off", text: d.dir_exists ? "No probe result yet. Run scripts/fno_real_order_probe.py (a dry run first), then come back." : "The probe folder does not exist yet: run mkdir -p data/fno_real_probe on the server, restart the service, then run the probe." };
  const p = d.probe;
  if (p.still_open) return { tone: "bad", text: `A POSITION IS STILL OPEN (${p.contract || "see the probe file"}): close it in the Dhan app now.` };
  if (!p.live) return { tone: "off", text: "The latest probe was a dry run: nothing was sent, so there are no charges to compare." };
  const why = p.exit_code !== 0 && p.reason ? ` ${p.reason}` : "";                      // Round 269: say WHY a run stopped (the refusal text is saved in the result)
  return { tone: p.exit_code === 0 ? "good" : "warn", text: `Probe: ${p.outcome}.${why}` };
}

/** Round 303: the strategies the "Quick simulation" can run. tests/test_round303_backtest_ui.py compares this list with fno/strategies.py ORDER, so a new strategy cannot be missing from the page (the EXPIRY_* two were). */
export const BT_STRATEGIES: string[] = ["CORE", "TREND_RIDER", "MOMENTUM_SURGE", "BEAR_SWEEP", "BEAR_PIVOT", "DRIFT_EDGE", "WEEKLY_OPENER", "EXPIRY_MOMENTUM", "EXPIRY_ALPHA"];

/** Round 303: what the result page must say about a saved backtest besides the numbers: the verdict AND its reasons (they were only in the .txt), the data-quality line of a rolling run, and the replay's day warnings. */
export function btVerdictView(doc: Record<string, any> | null | undefined): { status: string; tone: "warn" | "bad" | "neutral"; reasons: string[]; quality: string | null; warnings: string[] } {
  const v = (doc && doc.verdict) || {};
  const status = typeof v.status === "string" && v.status ? v.status : "-";
  const reasons = Array.isArray(v.reasons) ? v.reasons.map(String) : [];
  const dq = doc && doc.data_quality;
  const quality = dq && typeof dq === "object"
    ? `${dq.ok ? "Data quality OK" : "Data quality NOT OK"}: held bars without a candle ${dq.unmanaged_pct ?? "-"}%, exits at the last known price ${dq.unpriced_exit_pct ?? "-"}%, days without a usable VIX ${dq.vix_unknown_pct ?? "-"}%${Array.isArray(dq.labels) && dq.labels.length ? `, strikes fetched ${dq.labels.join(", ")}` : ""}`
    : null;
  const warnings = Array.isArray(doc && doc.warnings) ? (doc as any).warnings.map(String) : [];
  return { status, tone: status === "NO_EDGE" ? "bad" : status === "-" ? "neutral" : "warn", reasons, quality, warnings };   // the service never says "safe": even a pass is only "this test only", so no green
}
