// round 551 (P2): words for a sector's traded value, share of the tracked market and up / down count (the board's `sectors` rows). Pure.
// TRADED value, not "money flow": every trade has a buyer and a seller, so this says where the trading is, not who is buying. A missing figure is a dash, never 0.
import type { BoardSector } from "./boardTypes";

export interface SectorTradedView { share: string; value: string; updown: string; title: string }
const fin = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** rupees -> "₹1,234 Cr" (crore = 1e7) or "₹45 L" (lakh = 1e5) under one crore; "-" when missing. */
export function inrShort(v: number | null | undefined): string {
  if (!fin(v) || v < 0) return "-";
  if (v >= 1e7) return `₹${Math.round(v / 1e7).toLocaleString("en-IN")} Cr`;
  return `₹${Math.round(v / 1e5).toLocaleString("en-IN")} L`;
}

export function sectorTradedView(s: Pick<BoardSector, "name"> & Partial<BoardSector>): SectorTradedView {
  const share = fin(s.turnover_share_pct) && s.turnover_share_pct >= 0 ? `${s.turnover_share_pct.toFixed(1)}%` : "-";
  const value = inrShort(s.turnover);
  const updown = fin(s.up) && fin(s.down) ? `${s.up}▲ ${s.down}▼` : "-";
  const title = `${s.name}: traded value ${value}, ${share} of the tracked stocks' traded value, ${updown === "-" ? "up / down not reported" : `${s.up} up, ${s.down} down`}. Traded value is not net buying.`;
  return { share, value, updown, title };
}
