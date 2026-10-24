// frontend/src/positionSide.ts  (short selling step 8-11)
// Pure helpers (no imports) so every place that shows a Position Stocks trade reads its direction the same way. A LONG buys first and sells later; a SHORT
// sells first and buys back later, so its profit is (entry - exit). Legacy rows (side missing / "" / "LONG") are longs. Covered by positionSide.test.mjs.

export type PositionSide = "LONG" | "SHORT";

export function sideOf(p: { side?: string | null } | null | undefined): PositionSide {
  return String(p?.side ?? "").trim().toUpperCase() === "SHORT" ? "SHORT" : "LONG";
}

export const isShort = (p: { side?: string | null } | null | undefined): boolean => sideOf(p) === "SHORT";

/** Label of the opening and the closing price: a long Buys then Sells; a short Sells (short) then Buys (cover). */
export function legLabels(p: { side?: string | null } | null | undefined): { entry: string; exit: string } {
  return isShort(p) ? { entry: "Sell", exit: "Cover" } : { entry: "Buy", exit: "Sell" };
}

/** P&L % of a closed trade from its prices when the service did not send one. Short = (entry - exit) / entry. null when it cannot be computed. */
export function pnlPctFromPrices(p: { side?: string | null; entry_price?: number | null; exit_price?: number | null }): number | null {
  const e = p.entry_price, x = p.exit_price;
  if (typeof e !== "number" || typeof x !== "number" || !isFinite(e) || !isFinite(x) || !e || !x) return null;
  const d = isShort(p) ? e - x : x - e;
  return (d / e) * 100;
}

/** Badge text for a short, "" for a long (longs stay unmarked, as before). */
export const sideBadge = (p: { side?: string | null } | null | undefined): string => (isShort(p) ? "SHORT" : "");

/** Mode text for the calibration page. live = real orders, shadow = records only, off = nothing. Anything else reads as off. */
export function shortModeView(mode: string | null | undefined): { mode: "off" | "shadow" | "live"; title: string; chip: string; tone: "ok" | "warn" | "bad"; orders: boolean } {
  const m = String(mode ?? "").trim().toLowerCase();
  if (m === "live") return { mode: "live", title: "Short selling · LIVE", chip: "real orders", tone: "bad", orders: true };
  if (m === "shadow") return { mode: "shadow", title: "Short selling · shadow only", chip: "no order", tone: "warn", orders: false };
  return { mode: "off", title: "Short selling · off", chip: "off", tone: "ok", orders: false };
}
