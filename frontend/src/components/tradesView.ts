// E156: summary tiles for the Paper Trades tab (pure, no React: tested by plain node in tradesView.test.mjs).
// Read from the portfolio summary the tab already holds (dummy money). A value of exactly 0 stays 0; only absent or non-numeric
// values are shown as missing. Profit and loss carry a sign and words, not colour alone. Nothing animates a number.
export type TradesKpi = { label: string; value: string; sub?: string; tone?: "up" | "down" | "warn" | "info" | "flat" };
export type PortfolioLike = {
  cash_balance?: number | null; open_positions_value?: number | null; open_positions_pnl?: number | null;
  open_positions?: number | null; closed_positions?: number | null; win_rate?: number | null;
  avg_r_multiple?: number | null; avg_pnl_pct?: number | null; max_drawdown_pct?: number | null;
};

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const rupee = (n: number) => `₹${Math.abs(Math.round(n)).toLocaleString("en-IN")}`;

export function signedRupee(n: number): string {
  const r = Math.round(n);
  return r === 0 ? "₹0" : `${r > 0 ? "+" : "-"}${rupee(r)}`;
}

export function tradesKpis(s: PortfolioLike | null | undefined): TradesKpi[] {
  if (!s) return [];
  const cash = num(s.cash_balance), inPos = num(s.open_positions_value), pnl = num(s.open_positions_pnl);
  const open = num(s.open_positions), closed = num(s.closed_positions), win = num(s.win_rate);
  const r = num(s.avg_r_multiple), pct = num(s.avg_pnl_pct), dd = num(s.max_drawdown_pct);
  const pnlRounded = pnl == null ? null : Math.round(pnl);
  return [
    { label: "Cash", value: cash != null ? rupee(cash) : "—", sub: "dummy money, not invested", tone: "flat" },
    { label: "In positions", value: inPos != null ? rupee(inPos) : "—", sub: open != null ? `${open} open position${open === 1 ? "" : "s"}` : "open count not reported", tone: "info" },
    {
      label: "Open P&L", value: pnl != null ? signedRupee(pnl) : "—",
      sub: pnl == null ? "not reported" : pnlRounded === 0 ? "unchanged on open positions" : pnl > 0 ? "up on open positions" : "down on open positions",
      tone: pnlRounded == null || pnlRounded === 0 ? "flat" : pnl! > 0 ? "up" : "down",
    },
    { label: "Win rate", value: win != null ? `${win}%` : "—", sub: closed != null ? `${closed} closed trade${closed === 1 ? "" : "s"}` : "closed count not reported", tone: "flat" },
    r != null ? { label: "Avg R", value: `${r.toFixed(2)}R`, sub: "average R multiple", tone: "flat" }
      : pct != null ? { label: "Avg trade", value: `${pct > 0 ? "+" : ""}${pct}%`, sub: "average P&L per trade", tone: "flat" }
      : { label: "Avg R", value: "—", sub: "no closed trade with a figure", tone: "flat" },
    { label: "Max drawdown", value: dd != null ? `${dd.toFixed(1)}%` : "—", sub: dd != null ? "as reported, not re-computed" : "not reported yet", tone: "flat" },
  ];
}
