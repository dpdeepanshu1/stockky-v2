// group 262 — the ONE Dhan NSE-equity charge card used by every dashboard (Real Auto Trade + Position Stocks).
// Mirrors services/real-trade-service/config.py + charges_ledger.py (and position-stocks-service's ledger), which in
// turn follow Dhan's published rate card (dhan.co/pricing). Before this, each tab carried its own hard-coded copy
// with the wrong rates: delivery STT charged on the SELL only (it is 0.1 % on BUY and SELL), intraday STT charged on
// BOTH legs (it is the SELL leg only), exchange 0.00345 % (NSE is 0.00297 % + 0.0001 % IPFT), GST left off the SEBI
// fee, and a flat 13.5 DP with no GST on every delivery sell (Dhan: Rs 12.50 + GST, once per scrip per day, and not
// when the shares were bought the same day). Estimates only: a contract note rounds STT and stamp duty to the rupee.

export const BROKERAGE_CAP = 20;                      // Rs per executed intraday order
export const BROKERAGE_PCT = 0.03 / 100;              // intraday: lower of cap and pct; delivery brokerage is 0
export const STT_INTRA_SELL_PCT = 0.025 / 100;        // intraday: SELL leg only
export const STT_DELIVERY_PCT = 0.1 / 100;            // delivery: BUY and SELL
export const EXCHANGE_PCT = (0.00297 + 0.0001) / 100; // NSE transaction charge + IPFT levy, both legs
export const SEBI_PCT = 0.0001 / 100;
export const GST_RATE = 0.18;                         // on brokerage + exchange + IPFT + SEBI (not STT, not stamp)
export const STAMP_DELIVERY_PCT = 0.015 / 100;        // BUY leg
export const STAMP_INTRA_PCT = 0.003 / 100;           // BUY leg
export const DP_BASE = 12.5;                          // per scrip sold from demat per day
export const DP_CHARGE_INCL_GST = DP_BASE * (1 + GST_RATE); // 14.75

export interface ChargeBreakdown {
  brokerage: number; stt: number; exchange: number; sebi: number; gst: number; stamp: number; dp: number; total: number;
}

/** Charges of ONE executed order EXCEPT DP (which depends on the day's other orders, see chargesForLegs). */
export function legCharges(value: number, side: string, isDelivery: boolean): ChargeBreakdown {
  const zero: ChargeBreakdown = { brokerage: 0, stt: 0, exchange: 0, sebi: 0, gst: 0, stamp: 0, dp: 0, total: 0 };
  if (!(value > 0)) return zero;
  const isBuy = side.toUpperCase() === "BUY";
  const brokerage = isDelivery ? 0 : Math.min(value * BROKERAGE_PCT, BROKERAGE_CAP);
  const stt = isDelivery ? value * STT_DELIVERY_PCT : (isBuy ? 0 : value * STT_INTRA_SELL_PCT);
  const stamp = isBuy ? value * (isDelivery ? STAMP_DELIVERY_PCT : STAMP_INTRA_PCT) : 0;
  const exchange = value * EXCHANGE_PCT;
  const sebi = value * SEBI_PCT;
  const gst = (brokerage + exchange + sebi) * GST_RATE;
  return { brokerage, stt, exchange, sebi, gst, stamp, dp: 0, total: brokerage + stt + exchange + sebi + gst + stamp };
}

export interface ChargeLeg {
  symbol: string; side: string; qty: number; price: number; isDelivery: boolean;
  time?: string;   // Dhan createTime/updateTime; orders are processed in time order (list order when unparsable)
}

function legTime(l: ChargeLeg, idx: number): number {
  const t = l.time ? Date.parse(l.time.replace(" ", "T")) : NaN;
  return Number.isFinite(t) ? t : idx;
}

/** Per-leg charges, DP included, returned in the SAME order as `legs`.
 *  DP: delivery SELL only, once per scrip per day, and only for the part sold from demat (shares bought earlier than
 *  today); a sell that only closes shares bought the same day debits nothing from demat. `legs` is one trading day. */
export function chargesForLegs(legs: ChargeLeg[]): ChargeBreakdown[] {
  const out: ChargeBreakdown[] = legs.map(l => legCharges(l.qty * l.price, l.side, l.isDelivery));
  const order = legs.map((_, i) => i).sort((a, b) => legTime(legs[a], a) - legTime(legs[b], b) || a - b);
  const boughtToday: Record<string, number> = {};
  const dpDone = new Set<string>();
  for (const i of order) {
    const l = legs[i];
    if (!l.isDelivery) continue;
    if (l.side.toUpperCase() === "BUY") {
      boughtToday[l.symbol] = (boughtToday[l.symbol] ?? 0) + l.qty;
    } else if (l.qty * l.price > 0) {
      const sameDay = Math.min(l.qty, boughtToday[l.symbol] ?? 0);
      boughtToday[l.symbol] = (boughtToday[l.symbol] ?? 0) - sameDay;
      if (l.qty - sameDay > 0 && !dpDone.has(l.symbol)) {
        dpDone.add(l.symbol);
        out[i] = { ...out[i], dp: DP_CHARGE_INCL_GST, total: out[i].total + DP_CHARGE_INCL_GST };
      }
    }
  }
  return out;
}

export function sumCharges(list: ChargeBreakdown[]): ChargeBreakdown {
  const t: ChargeBreakdown = { brokerage: 0, stt: 0, exchange: 0, sebi: 0, gst: 0, stamp: 0, dp: 0, total: 0 };
  for (const c of list) {
    t.brokerage += c.brokerage; t.stt += c.stt; t.exchange += c.exchange; t.sebi += c.sebi;
    t.gst += c.gst; t.stamp += c.stamp; t.dp += c.dp; t.total += c.total;
  }
  return t;
}
