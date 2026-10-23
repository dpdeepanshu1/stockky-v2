// E31: pure formatting / picking helpers of the Real Trade page (moved out of RealAutoTrade.tsx, code unchanged).
import type { PriceLevel } from "../../chartContext";

// group 259: "2026-09-01" -> "01 Sep" for the charges date ranges.
export const _CHG_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
export function fmtChargeDay(d: string | null | undefined): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d || "");
  return m ? `${m[3]} ${_CHG_MONTHS[Number(m[2]) - 1] || m[2]}` : (d || "—");
}


// ── Formatting helpers ───────────────────────────────────────────────────────
export function fmtInr(n: number | null | undefined, decimals = 0): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1_00_00_000) return `${sign}₹${(abs / 1_00_00_000).toFixed(1)}Cr`;
  if (abs >= 1_00_000) return `${sign}₹${(abs / 1_00_000).toFixed(1)}L`;
  return `${sign}₹${abs.toLocaleString("en-IN", { maximumFractionDigits: decimals })}`;
}

export function fmtHms(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// 2026-09-11 fix: these used to call toLocaleTimeString/toLocaleDateString
// with no `timeZone` option, so the displayed clock time depended on the
// VIEWER'S BROWSER/OS timezone, not IST — wrong for anyone not physically
// set to IST, and silently "correct-looking but wrong" for most everyone
// else too, since a browser set to e.g. UTC would show a 5.5h-off time
// with no indication anything was off. This is a NSE trading dashboard —
// every timestamp on it means IST or it means nothing. Pinning
// `timeZone: "Asia/Kolkata"` explicitly makes the displayed time correct
// regardless of the viewer's own device settings.
export function fmtTime(iso: string): string {
  try { return new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }); } catch { return iso; }
}

export function fmtDate(iso: string): string {
  try { return new Date(iso).toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: "Asia/Kolkata" }); } catch { return iso; }
}

// Combined date + time + explicit "IST" label — for anywhere the person
// needs to know WHICH DAY a catalyst/signal/order was picked up, not just
// the time (a signal detected 11:58pm yesterday and one detected 12:02am
// today show the same clock time otherwise).
export function fmtDateTimeIst(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const date = d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: "Asia/Kolkata" });
    const time = d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" });
    return `${date}, ${time} IST`;
  } catch { return iso; }
}

// 2026-09-11 fix — user ask: "in live dhan tab today order details block if
// something sell something when do final sell mention the final sell price
// and originally buy price and based on that calculate total profit or loss
// should be display in one line ... after that stock in one line."
//
// 2026-09-11 follow-up fix — the first version only matched a SELL against
// a BUY placed *today*. In practice most sells are of shares bought on an
// earlier day (already sitting in the demat holdings), so buyQty was 0,
// pnl came out null, and the summary line silently never appeared for
// exactly the case in the screenshot (SELL-only orders for CFEL/JMA/
// MEDICAPQ). Now falls back to the demat holding's avg cost price for the
// buy side whenever today's orders don't contain a filled BUY for that
// symbol, so a SELL-only day still gets its buy/sell/P&L line.
// Also: a PENDING sell has no averageTradedPrice yet, so it's shown as a
// normal row with no P&L line until it actually fills.
export function groupDhanOrdersBySymbol(orders: any[], holdings: any[] = []): Array<{
  symbol: string;
  orders: any[];
  buyAvgPrice: number | null;
  sellAvgPrice: number | null;
  matchedQty: number;
  pnl: number | null;
  buySource: "today" | "holding" | null;
}> {
  const bySymbol = new Map<string, any[]>();
  for (const o of orders) {
    const sym = o.tradingSymbol || o.symbol || "—";
    if (!bySymbol.has(sym)) bySymbol.set(sym, []);
    bySymbol.get(sym)!.push(o);
  }

  const holdingBySymbol = new Map<string, number>();
  for (const h of holdings) {
    const sym = h.tradingSymbol || h.symbol || "—";
    const avg = Number(h.avgCostPrice || h.averageBuyPrice || 0);
    if (avg > 0) holdingBySymbol.set(sym, avg);
  }

  const FILLED = new Set(["TRADED", "FILLED", "COMPLETE"]);
  const out: ReturnType<typeof groupDhanOrdersBySymbol> = [];
  for (const [symbol, symOrders] of bySymbol.entries()) {
    let buyQty = 0, buyValue = 0, sellQty = 0, sellValue = 0;
    for (const o of symOrders) {
      const side = (o.transactionType || o.side || "").toUpperCase();
      const status = (o.orderStatus || o.status || "").toUpperCase();
      if (!FILLED.has(status)) continue;
      const qty = Number(o.quantity || o.tradedQuantity || 0);
      const price = Number(o.averageTradedPrice || o.price || 0);
      if (qty <= 0 || price <= 0) continue;
      if (side === "BUY") { buyQty += qty; buyValue += qty * price; }
      else if (side === "SELL") { sellQty += qty; sellValue += qty * price; }
    }

    let buyAvgPrice = buyQty > 0 ? buyValue / buyQty : null;
    let buySource: "today" | "holding" | null = buyAvgPrice != null ? "today" : null;

    // No BUY today but there was a fill on the SELL side — the shares came
    // from an existing holding bought earlier, so use its avg cost price.
    if (buyAvgPrice == null && sellQty > 0 && holdingBySymbol.has(symbol)) {
      buyAvgPrice = holdingBySymbol.get(symbol)!;
      buySource = "holding";
    }

    const sellAvgPrice = sellQty > 0 ? sellValue / sellQty : null;
    const matchedQty = buySource === "holding" ? sellQty : Math.min(buyQty, sellQty);
    const pnl = (buyAvgPrice != null && sellAvgPrice != null && matchedQty > 0)
      ? (sellAvgPrice - buyAvgPrice) * matchedQty
      : null;
    out.push({ symbol, orders: symOrders, buyAvgPrice, sellAvgPrice, matchedQty, pnl, buySource });
  }
  return out;
}

// Safe number extraction from Dhan SDK fund object (handles typos and casing)
export function pickNum(obj: any, ...keys: string[]): number | null {
  for (const k of keys) {
    const v = obj?.[k];
    if (typeof v === "number") return v;
    if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))) return Number(v);
  }
  return null;
}

/** Entry / stop / target as price lines on the candle chart; values that are missing or not numbers are left out. */
export function positionLevels(entry: unknown, stop: unknown, target: unknown): PriceLevel[] {
  const out: PriceLevel[] = [];
  const add = (label: string, v: unknown, tone: PriceLevel["tone"]) => { const n = Number(v); if (v != null && Number.isFinite(n) && n > 0) out.push({ label, price: n, tone }); };
  add("Entry", entry, "mute"); add("Stop", stop, "bad"); add("Target", target, "good");
  return out;
}

export function pnlColor(n: number | null | undefined): string {
  if (n == null) return "text-mist";
  return n >= 0 ? "text-signal-buy" : "text-signal-sell";
}

export function msFmt(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}
