// frontend/src/tapeSymbolView.ts  (E271)
// Pure helpers for the per-symbol part of the "Order-flow tape" card: the six checks of the order-flow checklist for one symbol and side, with the levels
// behind them (VAH / VAL / POC, nearest HVN above and below). Input is market-data's GET /flow/{symbol} (through GET /flow/symbol/{symbol} on the trading
// services). No runtime imports, never throws. Covered by tapeSymbol.test.mjs.

export type CheckState = "for" | "against" | "neutral";
export type Side = "LONG" | "SHORT";
export interface SymbolTapeDoc {
  symbol?: string; available?: boolean; reliable?: boolean; reason?: string | null; coverage_pct?: number | null; intervals?: number; tape_minutes?: number; price?: number | null;
  imbalance_5?: number | null;
  profile?: { poc?: number; vah?: number; val?: number; hvn?: number[]; lvn?: number[]; location?: string | null; node?: string | null;
    hvn_above?: number | null; hvn_above_pct?: number | null; hvn_below?: number | null; hvn_below_pct?: number | null } | null;
  signals?: { buy_pct_10m?: number | null; delta_agrees?: boolean | null; price_move_20m_pct?: number | null; absorption?: string | null; exhaustion?: string | null;
    aggressor?: string; big_prints?: { buy?: number; sell?: number }; stacked?: { buy?: number; sell?: number } } | null;
  checklist?: { long?: SideList; short?: SideList } | null;
}
interface SideList { checks?: Record<string, CheckState>; against?: number; for_count?: number; against_names?: string[] }

/** The six checks in the order of the order-flow checklist. */
export const CHECK_ORDER: { key: string; label: string; question: string }[] = [
  { key: "profile", label: "Profile", question: "VAH, VAL, POC: where is price against value?" },
  { key: "nodes", label: "HVN / LVN", question: "Price at a high-volume or low-volume node?" },
  { key: "footprint", label: "Footprint", question: "Who is aggressive?" },
  { key: "imbalance", label: "Imbalance", question: "Imbalance, absorption or exhaustion?" },
  { key: "delta", label: "Delta", question: "Does delta agree with the move?" },
  { key: "big_prints", label: "Big prints", question: "Big prints on the tape?" },
];

const isNum = (v: unknown): v is number => typeof v === "number" && isFinite(v);
const px = (v: unknown): string => (isNum(v) ? v.toFixed(2) : "-");
const stateOf = (v: unknown): CheckState => (v === "for" || v === "against" ? v : "neutral");

export interface SymbolTapeView {
  kind: "none" | "unreliable" | "ok";
  tone: "good" | "warn" | "bad" | undefined;
  headline: string;
  rows: { key: string; label: string; question: string; state: CheckState; detail: string }[];
  against: number; forCount: number;
  levels: { price: string; vah: string; poc: string; val: string; hvnAbove: string; hvnBelow: string; location: string };
  facts: string[];
}

/** Which side an open position is on (Real Trade positions are always long). */
export function sideOf(v: unknown): Side { return String(v ?? "").toUpperCase() === "SHORT" ? "SHORT" : "LONG"; }

export function symbolTapeView(doc: SymbolTapeDoc | null | undefined, side: Side): SymbolTapeView {
  const blank: SymbolTapeView = { kind: "none", tone: "warn", headline: "No tape for this symbol yet.", rows: [], against: 0, forCount: 0,
    levels: { price: "-", vah: "-", poc: "-", val: "-", hvnAbove: "-", hvnBelow: "-", location: "-" }, facts: [] };
  if (!doc || typeof doc !== "object" || doc.available !== true) {
    const why = doc && typeof doc === "object" && doc.reason ? String(doc.reason) : "";
    return { ...blank, headline: why === "no_tape" || !why ? blank.headline : `No tape for this symbol (${why}).` };
  }
  const cl = (side === "SHORT" ? doc.checklist?.short : doc.checklist?.long) ?? {};
  const checks = cl.checks ?? {};
  const p = doc.profile ?? {}, s = doc.signals ?? {};
  const bp = s.big_prints ?? {};
  const detailOf: Record<string, string> = {
    profile: `price ${px(doc.price)} is ${p.location ? String(p.location).replace(/_/g, " ") : "unknown against value"} (VAL ${px(p.val)} to VAH ${px(p.vah)}, POC ${px(p.poc)})`,
    nodes: p.node ? `price sits on a ${String(p.node).toUpperCase()}` : `nearest HVN above ${px(p.hvn_above)}${isNum(p.hvn_above_pct) ? ` (${p.hvn_above_pct}%)` : ""}, below ${px(p.hvn_below)}${isNum(p.hvn_below_pct) ? ` (${p.hvn_below_pct}%)` : ""}`,
    footprint: `aggressor: ${s.aggressor ?? "-"}; stacked imbalances buy ${s.stacked?.buy ?? 0} / sell ${s.stacked?.sell ?? 0}`,
    imbalance: `book ${isNum(doc.imbalance_5) ? doc.imbalance_5 : "-"}; absorption ${s.absorption ?? "none"}; exhaustion ${s.exhaustion ?? "none"}`,
    delta: `buy share (10 min) ${isNum(s.buy_pct_10m) ? s.buy_pct_10m + "%" : "-"}; delta ${s.delta_agrees === true ? "agrees with" : s.delta_agrees === false ? "disagrees with" : "no read on"} the 20 min move${isNum(s.price_move_20m_pct) ? ` (${s.price_move_20m_pct}%)` : ""}`,
    big_prints: `large blocks: buy ${bp.buy ?? 0} / sell ${bp.sell ?? 0}`,
  };
  const rows = CHECK_ORDER.map((c) => ({ key: c.key, label: c.label, question: c.question, state: stateOf(checks[c.key]), detail: detailOf[c.key] ?? "" }));
  const against = isNum(cl.against) ? cl.against : rows.filter((r) => r.state === "against").length;
  const forCount = isNum(cl.for_count) ? cl.for_count : rows.filter((r) => r.state === "for").length;
  const reliable = doc.reliable === true;
  const facts: string[] = [];
  if (isNum(doc.coverage_pct)) facts.push(`coverage ${doc.coverage_pct}%`);
  if (isNum(doc.tape_minutes)) facts.push(`${doc.tape_minutes} min of tape`);
  if (isNum(doc.intervals)) facts.push(`${doc.intervals} intervals`);
  const kind: SymbolTapeView["kind"] = reliable ? "ok" : "unreliable";
  const side_ = side === "SHORT" ? "short" : "long";
  const headline = reliable
    ? `${against} of 6 checks against a ${side_}, ${forCount} for.`
    : `Tape not reliable yet (${doc.reason ?? "unknown"}); the gate gives no opinion. Checks shown for information: ${against} against a ${side_}.`;
  const tone: SymbolTapeView["tone"] = !reliable ? "warn" : against >= 2 ? "bad" : against === 1 ? "warn" : "good";
  return { kind, tone, headline, rows, against, forCount, facts,
    levels: { price: px(doc.price), vah: px(p.vah), poc: px(p.poc), val: px(p.val), hvnAbove: px(p.hvn_above), hvnBelow: px(p.hvn_below),
      location: p.location ? String(p.location).replace(/_/g, " ") : "-" } };
}
