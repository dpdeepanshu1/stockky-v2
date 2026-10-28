// frontend/src/sellOrdersView.ts  (E243)
// Pure helpers (no imports, no network) for the "SELL orders" card on the Position Stocks Positions tab. Shape of GET /short/orders on position-stocks-service
// (orders/sell_orders.build). Read-only: every row is a SELL Stockky sent to Dhan (a SELL probe run or a live short), so an order that rests unfilled is visible.
// Covered by sellOrders.test.mjs.

export interface BrokerSellRow {
  order_id: string; symbol?: string | null; status: string; state: "FILLED" | "PARTIAL" | "WAITING" | "CANCELLED" | "REJECTED" | "UNKNOWN" | string;
  order_type?: string | null; quantity?: number | null; filled_qty?: number | null; price?: number | null; average_price?: number | null;
  legs?: Record<string, { status: string; price?: number | null }>;
}
export interface SellProbeRowDoc {
  kind: "PROBE"; symbol?: string | null; at?: string | null; order_id?: string | null; verdict?: string | null; reason?: string | null;
  entry_order_type?: string | null; entry_price_sent?: number | null; ref_price?: number | null; entry_fill?: number | null; cover_fill?: number | null; qty?: number | null;
  broker?: BrokerSellRow | null;
}
export interface SellShortRowDoc {
  kind: "SHORT"; id?: number | null; symbol?: string | null; status?: string | null; quantity?: number | null; entry_price?: number | null; stop_price?: number | null;
  target_price?: number | null; exit_price?: number | null; order_id?: string | null; plain?: boolean; opened_at?: string | null; broker?: BrokerSellRow | null;
}
export interface DhanShortDoc { security_id?: string | null; symbol?: string | null; net_qty: number; sell_avg?: number | null; ltp?: number | null; product?: string | null; unrealized?: number | null; tracked: boolean }
export interface SellOrdersDoc {
  probes: SellProbeRowDoc[]; shorts: SellShortRowDoc[]; broker_rows: (BrokerSellRow & { tracked?: boolean })[]; dhan_shorts?: DhanShortDoc[]; errors: string[]; short_mode?: string;
  entry_style?: { marketable_limit: boolean; cushion_pct: number };
  summary: { probe_runs: number; shorts_open: number; sell_orders_at_dhan: number; sell_orders_waiting: number; sell_orders_untracked?: number; dhan_shorts_open?: number; dhan_shorts_untracked?: number };
}

export type Tone = "good" | "bad" | "warn" | "mute";
export interface SellOrderLine {
  key: string; tag: "SELL PROBE" | "SHORT" | "AT DHAN"; symbol: string; when: string | null; tone: Tone; state: string; detail: string; orderId: string | null;
}

const num = (v: unknown): number | null => (typeof v === "number" && isFinite(v) ? v : null);
const px = (v: unknown): string => { const n = num(v); return n == null ? "-" : n.toFixed(2); };

/** Plain words + tone for what Dhan says about one SELL order (its own book), so a resting order reads as "waiting", never as a silent nothing. */
export function brokerStateView(b: BrokerSellRow | null | undefined): { tone: Tone; text: string } {
  if (!b) return { tone: "mute", text: "not in Dhan's book today" };
  const type = b.order_type ? `${b.order_type} ` : "";
  const at = num(b.price) != null ? ` @ ${px(b.price)}` : "";
  switch (b.state) {
    case "FILLED": return { tone: "good", text: `Dhan: ${type}SELL filled${num(b.average_price) ? ` at ${px(b.average_price)}` : ""}` };
    case "PARTIAL": return { tone: "warn", text: `Dhan: part filled (${b.filled_qty ?? 0} of ${b.quantity ?? "?"})` };
    case "WAITING": return { tone: "warn", text: `Dhan: ${type}SELL${at} is RESTING, nothing filled yet` };
    case "CANCELLED": return { tone: "mute", text: `Dhan: cancelled, filled ${b.filled_qty ?? 0}` };
    case "REJECTED": return { tone: "bad", text: "Dhan: rejected" };
    default: return { tone: "mute", text: `Dhan: ${b.status || "status unreadable"}` };
  }
}

function probeLine(r: SellProbeRowDoc, i: number): SellOrderLine {
  const v = String(r.verdict || "").toUpperCase();
  const entry = r.entry_order_type ? `${r.entry_order_type}${num(r.entry_price_sent) != null ? ` @ ${px(r.entry_price_sent)}` : ""}` : "entry type not recorded";
  const fill = num(r.entry_fill) != null ? `filled ${px(r.entry_fill)}` : "never filled";
  const cover = num(r.cover_fill) != null ? `, covered ${px(r.cover_fill)}` : "";
  const b = brokerStateView(r.broker);
  return {
    key: `p${i}-${r.order_id || r.at || i}`, tag: "SELL PROBE", symbol: String(r.symbol || "-"), when: r.at ?? null,
    tone: v === "PASS" ? "good" : v === "FAIL" ? "bad" : "warn", state: v || "NOT RUN",
    detail: `${r.qty ?? 1} share · ${entry} · ref ${px(r.ref_price)} · ${fill}${cover}. ${b.text}${v === "FAIL" && r.reason ? `. ${r.reason}` : ""}`,
    orderId: r.order_id ?? null,
  };
}

function shortLine(r: SellShortRowDoc, i: number): SellOrderLine {
  const open = r.status === "OPEN" || r.status === "EXIT_LEGS_REJECTED";
  const bad = r.status === "ERROR" || r.status === "EXIT_LEGS_REJECTED" || r.status === "STOP_HIT" || r.status === "STOP_BREACH_EXIT";
  const b = r.broker ? ` ${brokerStateView(r.broker).text}.` : "";
  return {
    key: `s${r.id ?? i}`, tag: "SHORT", symbol: String(r.symbol || "-"), when: r.opened_at ?? null, tone: open ? "warn" : bad ? "bad" : "good", state: String(r.status || "-"),
    detail: `${r.quantity ?? "-"} @ ${px(r.entry_price)} · stop ${px(r.stop_price)} · target ${px(r.target_price)}${num(r.exit_price) != null ? ` · covered ${px(r.exit_price)}` : ""}${r.plain ? " · plain order + broker stop" : ""}.${b}`,
    orderId: r.order_id ?? null,
  };
}

/** A short Dhan holds right now. When Stockky has no row for it, it says so loudly: nothing here is managing its stop, target or the 14:55 cover. */
function dhanShortLine(p: DhanShortDoc, i: number): SellOrderLine {
  const sym = String(p.symbol || p.security_id || "-");
  const qty = Math.abs(p.net_qty);
  return {
    key: `d${p.security_id ?? i}`, tag: "AT DHAN", symbol: sym, when: null, tone: p.tracked ? "warn" : "bad", state: p.tracked ? "SHORT OPEN" : "NOT IN STOCKKY",
    detail: `${qty} share${qty === 1 ? "" : "s"} short at Dhan${num(p.sell_avg) != null ? ` · sold avg ${px(p.sell_avg)}` : ""}${num(p.ltp) != null ? ` · last ${px(p.ltp)}` : ""}${p.product ? ` · ${p.product}` : ""}.`
      + (p.tracked ? "" : " Stockky has no position row for it, so nothing here manages its stop or the 14:55 cover: close it in Dhan."),
    orderId: null,
  };
}

/** A SELL Super Order in Dhan's book that Stockky has no record of (not a probe run, not a live short row). */
function untrackedOrderLine(b: BrokerSellRow, i: number): SellOrderLine {
  const s = brokerStateView(b);
  return { key: `o${b.order_id || i}`, tag: "AT DHAN", symbol: String(b.symbol || "-"), when: null, tone: s.tone === "good" ? "warn" : s.tone, state: String(b.state),
           detail: `${s.text}. Stockky has no record of this order (placed by an older run).`, orderId: b.order_id || null };
}

export interface SellOrdersView {
  kind: "unavailable" | "empty" | "active";
  headline: string;
  lines: SellOrderLine[];         // newest first (live shorts, then probe runs)
  waiting: number;                // SELL orders resting unfilled at Dhan right now (the card turns amber when > 0)
  errors: string[];
}

export function sellOrdersView(doc: SellOrdersDoc | null | undefined): SellOrdersView {
  if (!doc) return { kind: "unavailable", headline: "SELL order data is not available.", lines: [], waiting: 0, errors: [] };
  const lines = [...(doc.shorts || []).map(shortLine), ...(doc.probes || []).map(probeLine)];
  lines.sort((a, b) => String(b.when || "").localeCompare(String(a.when || "")));
  const untracked = (doc.broker_rows || []).filter((b) => b.tracked === false).map(untrackedOrderLine);
  const heldAtDhan = (doc.dhan_shorts || []).map(dhanShortLine);
  lines.unshift(...heldAtDhan, ...untracked);                       // what Dhan holds or knows that Stockky does not comes first
  const waiting = doc.summary?.sell_orders_waiting ?? 0;
  const open = doc.summary?.shorts_open ?? 0;
  const lost = doc.summary?.dhan_shorts_untracked ?? 0;
  if (!lines.length) return { kind: "empty", headline: "No SELL order has been sent yet (no live short, no SELL probe run).", lines, waiting, errors: doc.errors || [] };
  const parts = [`${open} short${open === 1 ? "" : "s"} open`, `${doc.summary?.probe_runs ?? 0} probe run${(doc.summary?.probe_runs ?? 0) === 1 ? "" : "s"}`];
  if ((doc.summary?.dhan_shorts_open ?? 0) > 0) parts.unshift(`${doc.summary.dhan_shorts_open} short${doc.summary.dhan_shorts_open === 1 ? "" : "s"} held at Dhan${lost > 0 ? ` (${lost} NOT in Stockky)` : ""}`);
  if (waiting > 0) parts.push(`${waiting} SELL order${waiting === 1 ? "" : "s"} RESTING unfilled at Dhan`);
  return { kind: "active", headline: parts.join(" · "), lines, waiting, errors: doc.errors || [] };
}
