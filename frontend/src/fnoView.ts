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
export const inr = (v: number | null | undefined, dp = 0): string => (v == null || Number.isNaN(v) ? "-" : `${v < 0 ? "-" : ""}₹${Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp })}`);
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
export function tradesCsv(ps: FnoPosition[]): string {
  const rows = ps.map((p) => [p.id ?? "", istDay(p.entry_ts), p.underlying, p.expiry, p.strike, p.opt_type, p.lots, istHm(p.entry_ts), p.entry_premium, istHm(p.exit_ts), p.exit_premium ?? "", p.exit_reason, p.gross_pnl ?? "", p.charges ?? "", p.net_pnl ?? "", p.mode].join(","));
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
  if (!p.live) return { tone: "off", text: "The newest probe was a dry run: nothing was sent, so there are no charges to compare." };
  return { tone: p.exit_code === 0 ? "good" : "warn", text: `Newest probe: ${p.outcome}.` };
}
