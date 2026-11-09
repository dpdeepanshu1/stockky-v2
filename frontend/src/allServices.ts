// Round 266, plan slice 4: pure helpers of Real Money Calibration > All services (Real Trade, Position Stocks and F&O side by side).
// Read-only: every value comes from a status endpoint the service pages already call; nothing here changes a switch or places an order.
// F&O in DEMO mode is SIMULATED money, so it is never added to the real-money total.

export type SvcId = "rt" | "ps" | "fno";
/** LIVE = the service can send real orders right now; the other words say which part is missing. */
export type LiveState = "live" | "armed_off" | "not_armed" | "engine_off" | "demo" | "kill_switch" | "unreachable";

export interface SvcSnap {
  id: SvcId;
  label: string;
  ok: boolean;                 // false = the status call failed (error says why)
  error: string | null;
  mode: "REAL" | "DEMO" | null;
  engineOn: boolean | null;    // auto-pilot (rt, ps) or the F&O engine
  armed: boolean | null;
  realOrdersPossible: boolean | null;   // null = the service does not say
  killSwitch: boolean | null;  // daily-loss kill switch tripped
  pnlToday: number | null;     // realized today, rupees
  pnlBasis: "net" | "gross" | null;   // net = after charges. Real Trade / Position Stocks book GROSS; net comes from their charges endpoint
  openCount: number | null;
  note: string | null;
}

export const SVC_LABEL: Record<SvcId, string> = { rt: "Real Trade", ps: "Position Stocks", fno: "F&O Options" };
/** Where the person changes the switches (this page never does). */
export const SVC_WHERE: Record<SvcId, string> = {
  rt: "Auto Trade tab (auto-pilot, arm) and Calibration > Real Trade > Switches",
  ps: "Position Stocks tab (auto-pilot, arm) and Calibration > Position Stocks Service > Switches",
  fno: "F&O Options > Overview > Control card, and Calibration > F&O Options > Switches",
};

const bool = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null);
function money(v: unknown): number | null {
  const n = typeof v === "number" ? v : typeof v === "string" && v.trim() !== "" ? Number(v) : NaN;
  return Number.isFinite(n) ? n : null;
}
const blank = (id: SvcId, error: string | null): SvcSnap => ({ id, label: SVC_LABEL[id], ok: error == null, error, mode: null, engineOn: null, armed: null, realOrdersPossible: null, killSwitch: null, pnlToday: null, pnlBasis: null, openCount: null, note: null });

/** A failed or junk answer becomes an "unreachable" snapshot, never a crash and never a zero. */
export function failed(id: SvcId, err: unknown): SvcSnap {
  const text = typeof err === "string" ? err : (err as any)?.message || "no answer";
  return blank(id, String(text));
}

/** Net today from a cumulative-charges answer (`pnl.net_realized_today`, both trading services); null when absent or junk. */
export function netToday(charges: any): number | null { return money(charges?.pnl?.net_realized_today); }

/** Sets the P&L: net when the charges endpoint gave it, else the gross the status gave (basis says which). */
function setPnl(s: SvcSnap, net: number | null, gross: number | null) {
  if (net != null) { s.pnlToday = net; s.pnlBasis = "net"; }
  else if (gross != null) { s.pnlToday = gross; s.pnlBasis = "gross"; }
}

/** Real Trade: GET /status/REAL (GateStatus) and, for the net figure, GET /charges/REAL/cumulative. */
export function fromRealTrade(gate: any, charges?: any): SvcSnap {
  if (!gate || typeof gate !== "object") return failed("rt", "Real Trade answered with an unexpected shape.");
  const s = blank("rt", null);
  s.mode = gate.mode === "DEMO" ? "DEMO" : "REAL";
  s.engineOn = bool(gate.auto_pilot_enabled);
  s.armed = bool(gate.armed);
  setPnl(s, netToday(charges), money(gate.account?.realized_pnl_today));
  s.note = typeof gate.disarmed_reason === "string" && gate.disarmed_reason ? gate.disarmed_reason : null;
  return s;
}

/** Position Stocks: GET /status, GET /ledger (gross today) and GET /charges/cumulative (net today); any may be missing, a missing ledger and charges leave the P&L unknown. */
export function fromPositionStocks(status: any, ledger: any, charges?: any): SvcSnap {
  if (!status || typeof status !== "object") return failed("ps", "Position Stocks answered with an unexpected shape.");
  const s = blank("ps", null);
  s.mode = "REAL";
  s.engineOn = bool(status.auto_pilot_enabled);
  s.armed = bool(status.armed);
  const kill = bool(ledger?.daily_loss_kill_switch_tripped) ?? bool(status.daily_loss_kill_switch);
  s.killSwitch = kill;
  setPnl(s, netToday(charges), money(ledger?.realized_pnl_today));
  if (status.service_enabled === false) s.note = "service switched off (SCALP service flag)";
  return s;
}

/** F&O: GET /fno/overview. DEMO mode means simulated money. */
export function fromFno(ov: any): SvcSnap {
  if (!ov || typeof ov !== "object" || !ov.locks) return failed("fno", "F&O service answered with an unexpected shape.");
  const s = blank("fno", null);
  s.mode = String(ov.locks.mode).toLowerCase() === "real" ? "REAL" : "DEMO";
  s.engineOn = bool(ov.locks.engine_enabled);
  s.armed = bool(ov.locks.armed_today);
  s.realOrdersPossible = bool(ov.locks.real_orders_possible);
  setPnl(s, money(ov.day?.realized_net), null);
  s.openCount = Array.isArray(ov.open_positions) ? ov.open_positions.length : null;
  const closed = Array.isArray(ov.locks.closed_locks) ? ov.locks.closed_locks : [];
  if (closed.length) s.note = `closed locks: ${closed.join(", ")}`;
  return s;
}

/** The one word for a service. Order matters: a tripped kill switch or an unreachable service is said before anything friendlier. */
export function liveState(s: SvcSnap): LiveState {
  if (!s.ok) return "unreachable";
  if (s.killSwitch === true) return "kill_switch";
  if (s.mode === "DEMO") return "demo";
  if (s.engineOn === false) return "engine_off";
  if (s.realOrdersPossible === false) return "armed_off";     // F&O: a lock other than the arm is closed
  if (s.armed === false) return "not_armed";
  if (s.engineOn === true && (s.armed === true || s.realOrdersPossible === true)) return "live";
  return "unreachable";                                         // flags missing: do not guess LIVE
}

export const STATE_TEXT: Record<LiveState, string> = {
  live: "LIVE: can send real orders",
  armed_off: "Armed, but a lock is closed",
  not_armed: "ON, not armed",
  engine_off: "OFF",
  demo: "DEMO (simulated)",
  kill_switch: "Daily-loss kill switch tripped",
  unreachable: "No usable answer",
};
export const STATE_TONE: Record<LiveState, "good" | "warn" | "bad" | undefined> = {
  live: "bad",          // red on purpose: this is the state that can lose money
  armed_off: "warn", not_armed: "warn", engine_off: undefined, demo: undefined, kill_switch: "bad", unreachable: "warn",
};

export interface Totals { real: number | null; simulated: number | null; realCount: number; missing: SvcId[]; live: SvcId[]; gross: SvcId[] }

/** Real-money total = Real Trade + Position Stocks + F&O only when F&O is in REAL mode. Services that did not answer are listed, not counted as 0. */
export function totals(snaps: SvcSnap[]): Totals {
  let real = 0, realCount = 0, sim = 0, simCount = 0;
  const missing: SvcId[] = [], live: SvcId[] = [], gross: SvcId[] = [];
  for (const s of snaps) {
    if (liveState(s) === "live") live.push(s.id);
    if (!s.ok || s.pnlToday == null) { missing.push(s.id); continue; }
    if (s.pnlBasis === "gross") gross.push(s.id);
    if (s.mode === "DEMO") { sim += s.pnlToday; simCount++; } else { real += s.pnlToday; realCount++; }
  }
  return { real: realCount ? Math.round(real * 100) / 100 : null, simulated: simCount ? Math.round(sim * 100) / 100 : null, realCount, missing, live, gross };
}

/** One sentence for the top of the page. */
export function headline(snaps: SvcSnap[]): string {
  const t = totals(snaps);
  const bad = snaps.filter((s) => liveState(s) === "kill_switch").map((s) => s.label);
  const parts: string[] = [];
  parts.push(t.live.length ? `${t.live.length} of ${snaps.length} services can send real orders now (${t.live.map((i) => SVC_LABEL[i]).join(", ")}).` : "No service can send real orders right now.");
  if (bad.length) parts.push(`Kill switch tripped: ${bad.join(", ")}.`);
  if (t.missing.length) parts.push(`No P&L from: ${t.missing.map((i) => SVC_LABEL[i]).join(", ")} (left out of the total, not counted as 0).`);
  if (t.gross.length) parts.push(`${t.gross.map((i) => SVC_LABEL[i]).join(", ")}: figure is GROSS (before charges), the charges answer was missing.`);
  return parts.join(" ");
}

/** What a P&L figure is, in words, for the line under it. */
export function basisText(s: SvcSnap): string {
  if (s.pnlToday == null) return "not reported";
  if (s.mode === "DEMO") return "simulated net today (not real money)";
  return s.pnlBasis === "net" ? "net realized today, after charges" : "GROSS realized today, before charges";
}

export const rupees = (n: number | null | undefined): string => (n == null || Number.isNaN(n) ? "-" : `${n < 0 ? "-" : n > 0 ? "+" : ""}Rs ${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`);
