// E-net: net-of-charges P&L for the paper (dummy-money) Trades page, using the same Dhan delivery rate card as Real Trade / Position Stocks
// (chargesRates.ts). Paper trades are delivery swings: both legs pay delivery STT, the buy pays stamp duty, and a sale of shares bought on an
// earlier day pays the demat (DP) fee. Estimates only, like every other charges figure in the app. Pure, never throws.
import { DP_CHARGE_INCL_GST, legCharges } from "./chargesRates.ts";

export interface PaperLike {
  quantity?: number | null; entry_price?: number | null; exit_price?: number | null; current_price?: number | null;
  pnl_amount?: number | null; status?: string | null; entry_date?: string | null; exit_date?: string | null;
}
export interface PaperNet { gross: number; charges: number; net: number; estimated: boolean }

const num = (v: unknown): number | null => { const x = Number(v); return v != null && v !== "" && Number.isFinite(x) ? x : null; };
const day = (v: unknown): string => (typeof v === "string" ? v.slice(0, 10) : "");

/** Gross, charges and net for one paper trade. Closed = charged on its real exit; open = what it would cost to close at the last price (estimated = true).
 *  null when the trade has no usable quantity / entry price / (exit or last) price. */
export function paperNet(t: PaperLike | null | undefined): PaperNet | null {
  try {
    if (!t) return null;
    const qty = num(t.quantity), entry = num(t.entry_price);
    const closed = String(t.status ?? "").toUpperCase() === "CLOSED";
    const out = closed ? num(t.exit_price) : (num(t.current_price) ?? null);
    if (qty == null || qty <= 0 || entry == null || entry <= 0 || out == null || out <= 0) return null;
    const grossCalc = (out - entry) * qty;
    const g = num(t.pnl_amount);
    const gross = g != null ? g : grossCalc;
    const buy = legCharges(qty * entry, "BUY", true).total;
    const sell = legCharges(qty * out, "SELL", true).total;
    const sameDay = day(t.entry_date) !== "" && day(t.entry_date) === day(t.exit_date);
    const charges = buy + sell + (sameDay ? 0 : DP_CHARGE_INCL_GST);
    return { gross, charges, net: gross - charges, estimated: !closed };
  } catch { return null; }
}

/** Sum over closed trades: realized gross, charges and net (trades with no usable prices are skipped; `n` = trades counted). */
export function paperNetTotals(trades: PaperLike[] | null | undefined): { gross: number; charges: number; net: number; n: number } {
  const t = { gross: 0, charges: 0, net: 0, n: 0 };
  for (const x of Array.isArray(trades) ? trades : []) {
    if (String(x?.status ?? "").toUpperCase() !== "CLOSED") continue;
    const r = paperNet(x);
    if (r) { t.gross += r.gross; t.charges += r.charges; t.net += r.net; t.n += 1; }
  }
  return t;
}
