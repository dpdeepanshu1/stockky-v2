// E557 (round 539, B3): a small trend line for one row, and the hook that loads the lines for a page of rows in one batched read.
// Presentation only: nothing here changes a score, a decision or an order. A name without history shows nothing.
import { useEffect, useState } from "react";
import { api } from "../api";
import { Spark } from "./ui/Spark";
import { loadSparklines, sparkFor, sparkPercents, sparkSummary, wantedSymbols } from "./rowSpark";

/** Loads the lines for these names (once per 30 minutes each) and returns a lookup. Re-renders when a read finishes. */
export function useSparklines(symbols: Array<string | undefined | null>): (symbol: string) => number[] | null {
  const [, bump] = useState(0);
  const key = wantedSymbols(symbols).sort().join(",");
  useEffect(() => {
    if (!key) return;
    let alive = true;
    const t = window.setTimeout(() => {
      void loadSparklines(key.split(","), (names) => api.marketSparklines(names)).then((changed) => { if (alive && changed) bump((n) => n + 1); });
    }, 150);   // let the page settle, and merge quick re-renders into one read
    return () => { alive = false; window.clearTimeout(t); };
  }, [key]);
  return (symbol: string) => sparkFor(symbol);
}

export function RowSpark({ symbol, closes }: { symbol: string; closes: number[] | null | undefined }) {
  const pts = sparkPercents(closes);
  if (!pts) return null;
  const label = sparkSummary(symbol, closes);
  return (
    <span className="mk-rowspark" title={label}>
      <Spark points={pts} width={88} height={28} label={label} />
    </span>
  );
}
