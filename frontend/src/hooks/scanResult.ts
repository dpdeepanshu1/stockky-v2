import type { Decision, ScanResult } from "../api";

// E70: the pure part of the scan runner (rows from a live stream -> the same ScanResult shape the server returns), moved out of App.tsx unchanged
// so it can be tested without React.
export function buildScanResultFromRows(rows: Decision[], total: number, partial: boolean): ScanResult {
  const actionable = new Set(["BUY NOW", "PREPARE TO BUY", "SELL"]);
  const recommendations = rows.filter((r) => actionable.has(String(r.decision || "")));
  const buy = rows.filter((r) => r.decision === "BUY NOW" || r.decision === "PREPARE TO BUY").length;
  const sell = rows.filter((r) => r.decision === "SELL").length;
  const hold = rows.filter((r) => r.decision === "HOLD").length;
  const cautious = rows.filter((r) => r.decision === "DO NOT BUY" || r.decision === "WAIT").length;
  return {
    scanned: rows.length,
    universe_size: total || rows.length,
    watchlist_size: 0,
    recommendations,
    watchlist_candidates: [],
    verdict: partial
      ? `Partial stream: ${rows.length}/${total || rows.length}`
      : `Stream complete: ${rows.length} symbols`,
    market_mood: buy > sell ? "bullish" : sell > buy ? "bearish" : "mixed",
    market_stats: {
      buy_signals: buy,
      sell_signals: sell,
      hold_signals: hold,
      cautious,
    },
    all_results: rows,
    errors: rows
      .filter((r) => (r as any).error)
      .map((r) => ({ symbol: r.symbol, error: (r as any).error || "ERROR" })),
    ...(partial ? { partial: true, stopped_early: true } as any : {}),
  };
}
