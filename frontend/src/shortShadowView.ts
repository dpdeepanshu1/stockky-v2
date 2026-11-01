// frontend/src/shortShadowView.ts  (short selling)
// Pure helpers for the "Short selling" panel on the calibration page (no imports, no network). Shape of GET /short/shadow on position-stocks-service
// (orders/short_entry.summary): the mode, the gate refusal counters, the latest shortlist and the last SELL probe. E285 round 94: the shadow-short history
// (rows, totals, S2 progress) is gone - nothing writes it any more. Covered by shortShadow.test.mjs.

export interface ShortShadowDoc {
  mode: string;
  gate_counters: { cycles?: number; scanned?: number; recorded?: number; refused?: number; by_code?: Record<string, number> };
  shortlist?: Record<string, unknown>;
  sell_probe?: SellProbeDoc;
}

/** E108 (T6): the last Dhan SELL probe, as position-stocks sends it under GET /short/shadow. */
export interface SellProbeDoc { ran: boolean; verdict: string; line: string; at?: string; symbol?: string; passed: boolean; rules?: string[]; required_for_live: boolean }

/** One plain line for the Short selling panel: what the probe says and what it means for going live. */
export function sellProbeView(p: SellProbeDoc | null | undefined): { tone: "good" | "bad" | "warn"; text: string } {
  if (!p) return { tone: "warn", text: "Dhan SELL probe (T6): status not available." };
  if (!p.ran) return { tone: "warn", text: p.required_for_live ? "Dhan SELL probe (T6): not run yet. A live short is refused until it passes (job: Dhan SELL probe)." : "Dhan SELL probe (T6): not run yet (not required: SHORT_LIVE_REQUIRES_PROBE is off)." };
  if (p.passed) return { tone: "good", text: `${p.line}${p.symbol ? ` (${p.symbol})` : ""}. A live short is allowed by this check.` };
  return { tone: "bad", text: `${p.line}. ${p.required_for_live ? "A live short is refused until the probe passes." : ""}`.trim() };
}

export type ShortKind = "unavailable" | "off" | "live";
export interface ShortShadowView {
  kind: ShortKind;
  headline: string;
  refusals: { code: string; count: number }[];   // gate refusal counters since the service started, biggest first (at most 6)
}

export function shortShadowView(doc: ShortShadowDoc | null | undefined): ShortShadowView {
  if (!doc) return { kind: "unavailable", headline: "Short selling data is not available.", refusals: [] };
  const by = doc.gate_counters?.by_code || {};
  const refusals = Object.entries(by).filter(([, n]) => typeof n === "number" && n > 0).map(([code, count]) => ({ code, count: count as number }))
    .sort((a, b) => b.count - a.count || (a.code < b.code ? -1 : 1)).slice(0, 6);
  if (doc.mode === "live") {
    return { kind: "live", refusals, headline: "LIVE. Real short orders are allowed, at most SHORT_LIVE_QTY_CAP shares each. Live shorts show under Positions, not here." };
  }
  return { kind: "off", refusals, headline: "Off. SHORT_MODE is off, so no short is considered. Switch it to live on the Switches page to allow real shorts (capped by SHORT_LIVE_QTY_CAP)." };
}

/** Short exposure line of the Market Weather card: what the profile does to SHORTS in a state (the short dial, position-stocks only). */
export function shortExposureLine(state: string | null | undefined, dials: Record<string, number | string | null> | undefined): string {
  if (!state) return "";
  const v = dials ? dials["MARKET_STATE_SHORT_MULT"] : undefined;
  if (v == null) return "Shorts: left at the saved short size (position-stocks only; longs are not touched).";
  const n = Number(v);
  if (!isFinite(n)) return "";
  if (n <= 0) return "Shorts: OFF today (position-stocks only; longs are not touched).";
  return `Shorts: ${Math.round(n * 100)}% of the saved short size (position-stocks only; longs are not touched).`;
}


/** The "Short selling" switches of position-stocks for the calibration page: every flag in the catalog group "Short selling", SHORT_MODE first. */
export interface ShortSwitch { key: string; title: string; value: string; recommended: string | null; pending: boolean }
export function shortSwitchRows(flags: { key: string; title: string; group: string; effective: string | null; running: string | null; recommended: string | null; pending_restart?: boolean }[] | null | undefined): ShortSwitch[] {
  const rows = (flags || []).filter((f) => f && f.group === "Short selling")
    .map((f) => ({ key: f.key, title: f.title, value: String(f.effective ?? f.running ?? "-"), recommended: f.recommended ?? null, pending: !!f.pending_restart }));
  return rows.sort((a, b) => (a.key === "SHORT_MODE" ? -1 : b.key === "SHORT_MODE" ? 1 : a.key.localeCompare(b.key)));
}
/** One-click off is offered only while SHORT_MODE is not already off. */
export const canSwitchShortOff = (rows: ShortSwitch[]): boolean => rows.some((r) => r.key === "SHORT_MODE" && r.value !== "off");
