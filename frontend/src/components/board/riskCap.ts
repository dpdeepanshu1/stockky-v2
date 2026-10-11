// group311: the "over risk cap" check behind the hatched tiles on the Real Trade board. It mirrors the two sizing steps of the Real
// Trade risk engine (risk_engine/engine.py checks 5 and 5b): the largest quantity whose stop-loss risk fits equity x risk-per-trade %,
// then what cash allows. Hatched = not even ONE share can be sized. DISPLAY ONLY - the server-side risk engine stays the authority.
//
// Stop used: this service's own proposed stop when the symbol is a candidate with a decision (exact), otherwise the board zone's far
// edge (an approximation - the real stop can be wider or tighter). `source` says which, and the UI says so.
export interface RiskCapAccount { equity: number | null; cashAvailable: number | null; riskPerTradePct: number | null }
export interface RiskCapInput {
  ltp: number;
  zoneRiskPerShare?: number | null;                  // board zone: |ltp - far edge|
  ownEntry?: number | null; ownStop?: number | null; // candidate decision: proposed price / stop
  engineVerdict?: string | null;                     // candidate decision: risk_verdict_reason, when the engine already said no
}
export type RiskCapStatus = "ok" | "over" | "unknown";
export interface RiskCapResult {
  status: RiskCapStatus; maxQty: number | null; riskPerShare: number | null; maxTradeRisk: number | null;
  source: "own-stop" | "zone-stop" | "engine" | "none"; reason: string;
}
const fin = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export function riskCap(i: RiskCapInput, a: RiskCapAccount): RiskCapResult {
  if (i.engineVerdict && /per[_ -]trade[_ ]?(risk[_ ])?cap|even 1 share/i.test(i.engineVerdict)) {
    return { status: "over", maxQty: 0, riskPerShare: null, maxTradeRisk: null, source: "engine", reason: "The risk engine already rejected this candidate on the per-trade risk cap." };
  }
  let rps: number | null = null;
  let source: RiskCapResult["source"] = "none";
  if (fin(i.ownEntry) && fin(i.ownStop) && i.ownEntry > i.ownStop && i.ownStop > 0) { rps = i.ownEntry - i.ownStop; source = "own-stop"; }
  else if (fin(i.zoneRiskPerShare) && i.zoneRiskPerShare > 0) { rps = i.zoneRiskPerShare; source = "zone-stop"; }
  if (rps == null || !fin(a.equity) || a.equity <= 0 || !fin(a.riskPerTradePct) || a.riskPerTradePct <= 0) {
    return { status: "unknown", maxQty: null, riskPerShare: rps, maxTradeRisk: null, source, reason: "Not enough information (stop, equity or risk % missing)." };
  }
  const maxTradeRisk = a.equity * (a.riskPerTradePct / 100);
  const byRisk = Math.floor(maxTradeRisk / rps);
  const byCash = fin(a.cashAvailable) && fin(i.ltp) && i.ltp > 0 ? Math.floor(Math.max(0, a.cashAvailable) / i.ltp) : null;
  const maxQty = byCash == null ? byRisk : Math.min(byRisk, byCash);
  if (maxQty < 1) {
    const why = byRisk < 1
      ? `Even 1 share risks ₹${rps.toFixed(2)}, above the ₹${maxTradeRisk.toFixed(0)} per-trade cap (${a.riskPerTradePct}% of equity).`
      : `Cash ₹${Math.max(0, a.cashAvailable ?? 0).toFixed(0)} does not cover one share at ₹${i.ltp.toFixed(2)}.`;
    return { status: "over", maxQty: 0, riskPerShare: rps, maxTradeRisk, source, reason: why };
  }
  return { status: "ok", maxQty, riskPerShare: rps, maxTradeRisk, source,
           reason: `Up to ${maxQty} share${maxQty === 1 ? "" : "s"} fit the ₹${maxTradeRisk.toFixed(0)} per-trade cap${byCash != null && byCash < byRisk ? " (cash-limited)" : ""}.` };
}

export function zoneScoreLabel(score: number | null | undefined): { text: string; tone: "good" | "mid" | "weak" | "none" } {
  if (score == null || !Number.isFinite(score)) return { text: "-", tone: "none" };
  return { text: String(Math.round(score)), tone: score >= 65 ? "good" : score >= 40 ? "mid" : "weak" };
}
