// E538 (redesign round 5b): the "how to read the board" legend as plain data, so it can be tested and drawn by BoardLegend.tsx.
// Every line is taken from what BoardTile.tsx / MarketBoard.tsx really draw: side colour, heat from the percentile, size from strength x turnover,
// the amber ring for an open position, the dashed ring for a marked symbol, "·F", "EXT" and the rank arrow. No colour stands alone: each swatch has words beside it.

export type LegendSwatch = "bull" | "bear" | "heat-lo" | "heat-hi" | "size-s" | "size-l" | "held" | "marked" | "text";
export interface LegendItem { key: string; swatches: LegendSwatch[]; mark?: string; label: string }

export function legendItems(opts: { marked?: boolean } = {}): LegendItem[] {
  const items: LegendItem[] = [
    { key: "bull", swatches: ["bull"], label: "Green: price rising (bullish)" },
    { key: "bear", swatches: ["bear"], label: "Red: price falling (bearish)" },
    { key: "heat", swatches: ["heat-lo", "heat-hi"], label: "Brighter: stronger than more of the other stocks" },
    { key: "size", swatches: ["size-s", "size-l"], label: "Bigger: stronger move on more money traded" },
    { key: "held", swatches: ["held"], label: "Amber ring: you hold it" },
  ];
  if (opts.marked) items.push({ key: "marked", swatches: ["marked"], label: "Dashed ring: on your watchlist or candidate list" });
  items.push(
    { key: "fno", swatches: ["text"], mark: "·F", label: "Has F&O contracts" },
    { key: "ext", swatches: ["text"], mark: "EXT", label: "Price already stretched" },
    { key: "rank", swatches: ["text"], mark: "▲3", label: "Places gained (▼ lost) in the ranking" },
  );
  return items;
}
