// E543 (redesign round 7d): the shared card face of the Markets pages as plain data (pure, no React, no import: tested by plain node in markets.test.mjs).
// It only arranges text the page already has (the same formatted values its table row shows), so a card and a table row can never disagree.
// A missing value is a dash, never "NaN" or a silent 0; direction and tier are spelled with an arrow / glyph and a word, never colour only.
export type CardDir = "up" | "down" | "flat" | null;
export interface MarketCardData {
  symbol: string;
  badge: { label: string; glyph: string; tone: "buy" | "warn" | "flat" };
  change: { text: string; dir: CardDir };
  score: number | null;                 // 0..100 for the ring, null = no score (no ring is drawn)
  facts: { label: string; value: string }[];
  tag: string | null;                   // e.g. the trigger type
}

// structurally the same as SurpriseRowView (surpriseView.ts), so the page passes surpriseRowView(s) straight in
interface SurpriseViewLike {
  score: { text: string } | null;
  tier: { label: string; glyph: string; tone: "buy" | "warn" };
  change: { text: string; dir: CardDir };
  rvol: string; slope: string | null; target: string; stop: string;
}
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export function surpriseCard(s: { symbol?: unknown; score?: unknown; trigger_type?: unknown }, v: SurpriseViewLike): MarketCardData {
  const sc = num(s.score);
  const trig = typeof s.trigger_type === "string" && s.trigger_type.trim() ? s.trigger_type.trim() : null;
  return {
    symbol: typeof s.symbol === "string" ? s.symbol : "—",
    badge: { label: v.tier.label, glyph: v.tier.glyph, tone: v.tier.tone },
    change: v.change,
    score: sc == null ? null : Math.max(0, Math.min(100, Math.round(sc))),
    facts: [
      { label: "RVOL", value: v.slope ? `${v.rvol} ${v.slope}` : v.rvol },
      { label: "Target +5%", value: v.target },
      { label: "Trail (VWAP)", value: v.stop },
    ],
    tag: trig,
  };
}

/** The arrow in front of a change figure ("▲ " / "▼ " / ""), so the direction is in the text as well as the colour. */
export function changeArrow(dir: CardDir): string { return dir === "up" ? "▲ " : dir === "down" ? "▼ " : ""; }

export type SurpriseView = "cards" | "table";
export const SURPRISE_VIEWS: { id: SurpriseView; label: string }[] = [{ id: "table", label: "Table" }, { id: "cards", label: "Cards" }];
