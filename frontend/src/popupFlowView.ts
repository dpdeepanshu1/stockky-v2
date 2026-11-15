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
