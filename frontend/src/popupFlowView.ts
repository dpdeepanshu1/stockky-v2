// round 555 (E580, Tredye plan P6): the order-flow section of the stock popup. DISPLAY ONLY: it reads the tape the entry gate already uses (market-data GET /flow/{symbol},
// through the admin route GET /flow/symbol/{symbol}) and decides nothing. Pure words, no React. Rules:
//  - it is an ESTIMATE from quote snapshots, not a trade feed, and says so on the section;
//  - an unavailable or UNRELIABLE tape shows NOTHING (no numbers at all), because an unreliable tape is not evidence;
//  - every number comes with the time it was read (IST); a missing number is a dash, never 0;
//  - no buy / sell verdict and no six-check judgement here (those belong to the gate and the calibration card).
import type { SymbolTapeDoc } from "./tapeSymbolView.ts";

export const DASH = "-";
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const px = (v: unknown): string => (isNum(v) && v > 0 ? v.toFixed(2) : DASH);
const pct = (v: unknown, nd = 0): string => (isNum(v) ? `${v.toFixed(nd)}%` : DASH);

export interface FlowLine { key: string; label: string; value: string }
export interface PopupFlowView {
  kind: "hidden" | "ok";
  lines: FlowLine[];
  title: string;
  note: string;
  asOf: string;           // "14:32" IST, "" when unknown
}

/** "14:32" in IST for an epoch-ms time, "" when it is not a real time. */
export function istClock(ms: unknown): string {
  if (!isNum(ms) || ms <= 0) return "";
  const t = new Date(ms);
  if (!Number.isFinite(t.getTime())) return "";
  return new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false }).format(t);
}

const hidden = (): PopupFlowView => ({ kind: "hidden", lines: [], title: "", note: "", asOf: "" });

/** Words for the popup section. `loadedAtMs` = when the browser read it (the tape has no clock of its own in the reply). Hidden unless the tape is available AND reliable. */
export function popupFlowView(doc: SymbolTapeDoc | null | undefined, loadedAtMs?: number): PopupFlowView {
  if (!doc || typeof doc !== "object" || doc.available !== true || doc.reliable !== true) return hidden();
  const p = doc.profile ?? {}, s = doc.signals ?? {};
  const asOf = istClock(loadedAtMs);
  if (!asOf) return hidden();                       // no number without a time
  const loc = p.location ? String(p.location).replace(/_/g, " ") : DASH;
  const delta = s.delta_agrees === true ? "agrees with the 20 min move" : s.delta_agrees === false ? "disagrees with the 20 min move" : "no read";
  const lines: FlowLine[] = [
    { key: "value", label: "Value area", value: isNum(p.val) && isNum(p.vah) ? `${px(p.val)} to ${px(p.vah)}` : DASH },
    { key: "poc", label: "Most-traded price", value: px(p.poc) },
    { key: "loc", label: "Price is", value: loc },
    { key: "hvn_up", label: "Heavy volume above", value: px(p.hvn_above) },
    { key: "hvn_dn", label: "Heavy volume below", value: px(p.hvn_below) },
    { key: "buy", label: "Buy share, 10 min", value: pct(s.buy_pct_10m) },
    { key: "delta", label: "Delta", value: delta },
    { key: "cov", label: "Tape coverage", value: pct(doc.coverage_pct, 0) },
  ];
  return {
    kind: "ok", lines, title: "Order flow (estimate)", asOf,
    note: `ESTIMATE from quote snapshots, not a trade feed. Read ${asOf} IST${isNum(doc.tape_minutes) ? `, ${doc.tape_minutes} min of tape` : ""}. For reading only; it does not change any trade.`,
  };
}

// ── round 559 (P8): the liquidity badge. Two recorded facts with the time each was read; a missing one is a dash, never 0; no verdict ("liquid" / "illiquid") is given. ──
export interface LiquidityView { kind: "hidden" | "ok"; turnover: string; turnoverSub: string; spread: string; spreadSub: string; note: string }

/** rupees in the Indian way: 12.4 Cr, 8.6 L, or the plain number below a lakh; a dash when it is not a positive number */
export function rupeesShort(v: unknown): string {
  if (!isNum(v) || v <= 0) return DASH;
  if (v >= 1e7) return `\u20B9${(v / 1e7).toFixed(v >= 1e9 ? 0 : 1)} Cr`;
  if (v >= 1e5) return `\u20B9${(v / 1e5).toFixed(1)} L`;
  return `\u20B9${Math.round(v).toLocaleString("en-IN")}`;
}

/** "14:32 IST", or "13 Nov 15:29 IST" when the read is from another IST day than `nowMs` (a closed market shows the last session, and says so). "" when it is not a time. */
export function istStamp(sec: unknown, nowMs: number): string {
  if (!isNum(sec) || sec <= 0) return "";
  const clock = istClock(sec * 1000);
  if (!clock) return "";
  const day = (ms: number) => new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(ms));
  if (!isNum(nowMs) || nowMs <= 0 || day(sec * 1000) === day(nowMs)) return `${clock} IST`;
  const d = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", day: "numeric", month: "short" }).format(new Date(sec * 1000));
  return `${d} ${clock} IST`;
}

/** Words for the popup's liquidity line. Hidden when the service sent nothing or neither fact has a time (no number without a time). `nowMs` = the browser's clock. */
export function liquidityView(doc: unknown, nowMs: number): LiquidityView {
  const none: LiquidityView = { kind: "hidden", turnover: DASH, turnoverSub: "", spread: DASH, spreadSub: "", note: "" };
  if (!doc || typeof doc !== "object") return none;
  const d = doc as { turnover?: any; spread?: any };
  const t = d.turnover && typeof d.turnover === "object" ? d.turnover : null;
  const sp = d.spread && typeof d.spread === "object" ? d.spread : null;
  const tAt = t ? istStamp(t.as_of, nowMs) : "";
  const sAt = sp ? istStamp(sp.last_at, nowMs) : "";
  const tOk = !!t && !!tAt && isNum(t.value) && t.value > 0;
  const sOk = !!sp && !!sAt && isNum(sp.last_pct) && sp.last_pct >= 0;
  if (!tOk && !sOk) return none;
  const rank = tOk && isNum(t.pct_rank) ? `higher than ${t.pct_rank.toFixed(0)}% of ${isNum(t.of) ? t.of : "the"} tracked stocks` : "rank not available";
  const spreadSub = sOk
    ? `${sAt}${isNum(sp.median_pct) ? `, session median ${sp.median_pct.toFixed(2)}%` : ""}${isNum(sp.max_pct) ? `, widest minute ${sp.max_pct.toFixed(2)}%` : ""}`
    : "no recorded reading this session";
  return {
    kind: "ok",
    turnover: tOk ? rupeesShort(t.value) : DASH, turnoverSub: tOk ? `${rank}, as of ${tAt}` : "",
    spread: sOk ? `${sp.last_pct.toFixed(2)}%` : DASH, spreadSub,
    note: "Traded value so far this session, ranked among the board's tracked stocks; spread = the recorded bid-ask spread (last minute). Facts for reading only; they do not change any trade.",
  };
}
