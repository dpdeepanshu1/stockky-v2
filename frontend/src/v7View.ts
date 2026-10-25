// frontend/src/v7View.ts  (design v7)
// Pure helpers behind the v7 visuals (no runtime imports, no network, never throws). Every function turns numbers the pages ALREADY hold into
// positions / widths / words for a drawing. Nothing here decides a trade, and nothing here is a forecast: it only restates what exists.
// Covered by v7.test.mjs.

const isNum = (v: unknown): v is number => typeof v === "number" && isFinite(v);
const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));
const r1 = (v: number): number => Math.round(v * 10) / 10;

export type Side = "LONG" | "SHORT";
export const sideFrom = (s: unknown): Side => (String(s ?? "").trim().toUpperCase() === "SHORT" ? "SHORT" : "LONG");

/* ── 1. risk rail: stop ─ entry ─ target, true to scale, long or short ───────────────────────────────────────── */

export type RailState = "unknown" | "at_stop" | "near_stop" | "under" | "flat" | "ahead" | "at_target";
export interface RailView {
  ok: boolean;
  side: Side;
  entryPct: number;      // where the entry tick sits on the rail, 0..100 (stop = 0, target = 100)
  pricePct: number;      // where the live price sits, 0..100 (clamped; the text still shows the real price)
  state: RailState;
  arrow: "▲" | "▼" | "•" | "";
  words: string;         // plain sentence, also used as the accessible name
  riskUsedPct: number;   // share of the way from entry to stop that price has travelled (0 when in profit)
  rewardDonePct: number; // share of the way from entry to target that price has travelled (0 when under water)
}

/**
 * One formula for both directions: position = (x - stop) / (target - stop). A long has stop < entry < target, a short has stop > entry > target,
 * and the same formula puts "stop" on the left and "target" on the right of the rail either way. Profit therefore always moves to the right.
 */
export function railView(p: { side?: unknown; entry?: unknown; stop?: unknown; target?: unknown; price?: unknown }): RailView {
  const side = sideFrom(p.side);
  const bad: RailView = { ok: false, side, entryPct: 50, pricePct: 50, state: "unknown", arrow: "", words: "Stop and target are not both known, so there is no rail to draw.", riskUsedPct: 0, rewardDonePct: 0 };
  const { entry, stop, target } = p as { entry: unknown; stop: unknown; target: unknown };
  if (!isNum(entry) || !isNum(stop) || !isNum(target) || entry <= 0 || stop <= 0 || target <= 0) return bad;
  const span = target - stop;
  if (span === 0) return bad;
  // a long needs stop < entry < target, a short needs target < entry < stop; anything else is a bad level set, not something to draw
  const sane = side === "LONG" ? stop < entry && entry < target : target < entry && entry < stop;
  if (!sane) return { ...bad, words: "The stop, entry and target are not in a sensible order for this direction, so the rail is not drawn." };
  const at = (x: number): number => ((x - stop) / span) * 100;
  const entryPct = clamp(at(entry), 0, 100);
  if (!isNum(p.price) || p.price <= 0) {
    return { ok: true, side, entryPct: r1(entryPct), pricePct: r1(entryPct), state: "unknown", arrow: "", words: "No live price yet; the rail shows the stop, entry and target only.", riskUsedPct: 0, rewardDonePct: 0 };
  }
  const raw = at(p.price);
  const pricePct = clamp(raw, 0, 100);
  const profit = (side === "LONG" ? p.price - entry : entry - p.price);
  const toStop = Math.abs(entry - stop), toTarget = Math.abs(target - entry);
  const riskUsedPct = profit < 0 && toStop > 0 ? clamp((-profit / toStop) * 100, 0, 100) : 0;
  const rewardDonePct = profit > 0 && toTarget > 0 ? clamp((profit / toTarget) * 100, 0, 100) : 0;
  let state: RailState; let arrow: RailView["arrow"]; let words: string;
  if (raw <= 0) { state = "at_stop"; arrow = "▼"; words = "Price is at or through the stop."; }
  else if (raw >= 100) { state = "at_target"; arrow = "▲"; words = "Price is at or past the target."; }
  else if (profit === 0) { state = "flat"; arrow = "•"; words = "Price is at the entry."; }
  else if (profit > 0) { state = "ahead"; arrow = "▲"; words = `In profit: ${Math.round(rewardDonePct)}% of the way from entry to target.`; }
  else if (riskUsedPct >= 70) { state = "near_stop"; arrow = "▼"; words = `Under water: ${Math.round(riskUsedPct)}% of the way from entry to stop.`; }
  else { state = "under"; arrow = "▼"; words = `Under water: ${Math.round(riskUsedPct)}% of the way from entry to stop.`; }
  return { ok: true, side, entryPct: r1(entryPct), pricePct: r1(pricePct), state, arrow, words, riskUsedPct: r1(riskUsedPct), rewardDonePct: r1(rewardDonePct) };
}

/* ── 2. exposure strip: how the open money is spread, long vs short ─────────────────────────────────────────── */

export interface ExposureRow { symbol: string; side?: unknown; qty?: unknown; entry?: unknown; pnl?: unknown; riskUsedPct?: number }
export interface ExposureSegment { symbol: string; side: Side; widthPct: number; value: number; tone: "up" | "down" | "flat"; arrow: "▲" | "▼" | "•" }
export interface ExposureView {
  count: number; longCount: number; shortCount: number;
  longValue: number; shortValue: number; totalValue: number;
  longPct: number; shortPct: number;
  pnlTotal: number | null;
  nearStop: string[];            // symbols that have used 70%+ of their way to the stop
  segments: ExposureSegment[];   // biggest first, widths add up to ~100
}

export function exposureView(rows: ExposureRow[] | null | undefined): ExposureView {
  const out: ExposureView = { count: 0, longCount: 0, shortCount: 0, longValue: 0, shortValue: 0, totalValue: 0, longPct: 0, shortPct: 0, pnlTotal: null, nearStop: [], segments: [] };
  if (!Array.isArray(rows)) return out;
  const items: { symbol: string; side: Side; value: number; pnl: number | null }[] = [];
  for (const r of rows) {
    if (!r || typeof r.symbol !== "string") continue;
    const q = isNum(r.qty) ? Math.abs(r.qty) : 0, e = isNum(r.entry) ? Math.abs(r.entry) : 0;
    const value = q * e;
    const side = sideFrom(r.side);
    items.push({ symbol: r.symbol, side, value, pnl: isNum(r.pnl) ? r.pnl : null });
    if (isNum(r.riskUsedPct) && r.riskUsedPct >= 70) out.nearStop.push(r.symbol);
  }
  out.count = items.length;
  for (const it of items) { if (it.side === "SHORT") { out.shortCount += 1; out.shortValue += it.value; } else { out.longCount += 1; out.longValue += it.value; } }
  out.totalValue = out.longValue + out.shortValue;
  if (out.totalValue > 0) { out.longPct = r1((out.longValue / out.totalValue) * 100); out.shortPct = r1(100 - out.longPct); }
  const withPnl = items.filter((i) => i.pnl != null);
  out.pnlTotal = withPnl.length ? withPnl.reduce((a, i) => a + (i.pnl as number), 0) : null;
  const sorted = [...items].sort((a, b) => b.value - a.value);
  out.segments = sorted.map((i) => ({
    symbol: i.symbol, side: i.side, value: i.value,
    widthPct: out.totalValue > 0 ? r1((i.value / out.totalValue) * 100) : r1(100 / sorted.length),
    tone: i.pnl == null || Math.abs(i.pnl) < 0.5 ? "flat" : i.pnl > 0 ? "up" : "down",
    arrow: i.pnl == null || Math.abs(i.pnl) < 0.5 ? "•" : i.pnl > 0 ? "▲" : "▼",
  }));
  return out;
}

/* ── 3. mode ladder: OFF → SHADOW → LIVE ─────────────────────────────────────────────────────────────────────── */

export type LadderMode = "off" | "shadow" | "live";
export interface LadderStep { id: LadderMode; label: string; note: string; state: "past" | "current" | "future" }
export interface LadderView { mode: LadderMode; steps: LadderStep[]; orders: boolean; tone: "ok" | "warn" | "bad"; words: string }

export function ladderMode(raw: unknown): LadderMode {
  const m = String(raw ?? "").trim().toLowerCase();
  return m === "live" ? "live" : m === "shadow" ? "shadow" : "off";
}

/** `lane` names the thing being laddered ("Short selling", "Swing lane") so the sentence is specific. Anything unknown reads as off (the safe step). */
export function ladderView(raw: unknown, lane = "This lane"): LadderView {
  const mode = ladderMode(raw);
  const order: LadderMode[] = ["off", "shadow", "live"];
  const at = order.indexOf(mode);
  const notes: Record<LadderMode, string> = { off: "does nothing", shadow: "records, no order", live: "real orders" };
  const labels: Record<LadderMode, string> = { off: "OFF", shadow: "SHADOW", live: "LIVE" };
  const steps = order.map((id, i): LadderStep => ({ id, label: labels[id], note: notes[id], state: i < at ? "past" : i === at ? "current" : "future" }));
  const words = mode === "live" ? `${lane} is LIVE: real orders can be placed.` : mode === "shadow" ? `${lane} is in SHADOW: it records what it would do, no order is placed.` : `${lane} is OFF: nothing is recorded and nothing is placed.`;
  return { mode, steps, orders: mode === "live", tone: mode === "live" ? "bad" : mode === "shadow" ? "warn" : "ok", words };
}

/* ── 4. evidence meter: how far a shadow test has got ───────────────────────────────────────────────────────── */

export interface EvidenceView { tradesPct: number; daysPct: number; overallPct: number; ready: boolean; words: string }
export function evidenceView(done: unknown, needed: unknown, days: unknown, daysNeeded: unknown): EvidenceView {
  const d = isNum(done) && done > 0 ? done : 0, n = isNum(needed) && needed > 0 ? needed : 1;
  const dy = isNum(days) && days > 0 ? days : 0, dn = isNum(daysNeeded) && daysNeeded > 0 ? daysNeeded : 1;
  const tradesPct = Math.round(clamp((d / n) * 100, 0, 100)), daysPct = Math.round(clamp((dy / dn) * 100, 0, 100));
  const overallPct = Math.min(tradesPct, daysPct);   // the verdict needs BOTH, so the slower one is the honest overall figure
  const ready = d >= n && dy >= dn;
  return { tradesPct, daysPct, overallPct, ready, words: ready ? "Enough trades and days for a verdict." : `${d} of ${n} trades and ${dy} of ${dn} days so far; the slower of the two decides.` };
}

/* ── 5. capital ring: how the one Dhan balance is shared by the two services ─────────────────────────────────── */

export interface CapitalView {
  ok: boolean;
  psPct: number;       // Position Stocks pool as a share of the whole balance
  rtPct: number;       // the rest (Real Trade's computed reference share)
  psFreePct: number;   // part of the whole balance that is free inside the Position Stocks pool (never more than the pool itself)
  words: string;
}
export function capitalView(total: unknown, psAllocated: unknown, psAvailable: unknown, splitPct: unknown): CapitalView {
  const none: CapitalView = { ok: false, psPct: 0, rtPct: 0, psFreePct: 0, words: "The balance is not known yet, so the ring is not drawn." };
  if (!isNum(total) || total <= 0) return none;
  const flat = isNum(splitPct) ? clamp(splitPct, 0, 100) : 50;
  const alloc = isNum(psAllocated) && psAllocated >= 0 ? psAllocated : (total * flat) / 100;
  const psPct = clamp((alloc / total) * 100, 0, 100);
  const free = isNum(psAvailable) && psAvailable > 0 ? Math.min(psAvailable, alloc) : 0;
  const psFreePct = clamp((free / total) * 100, 0, psPct);
  return { ok: true, psPct: r1(psPct), rtPct: r1(100 - psPct), psFreePct: r1(psFreePct), words: `Position Stocks holds ${Math.round(psPct)}% of the balance, Real Trade the other ${Math.round(100 - psPct)}%.` };
}

/* ── 6. drift beat: the heartbeat line of a reconciliation check ─────────────────────────────────────────────── */

export type BeatKind = "steady" | "warn" | "alarm" | "idle";
export function beatKind(kind: unknown, warnCount: unknown): BeatKind {
  if (kind === "error") return "alarm";
  if (kind === "drift") return isNum(warnCount) && warnCount > 0 ? "warn" : "steady";
  if (kind === "clean") return "steady";
  return "idle";
}
