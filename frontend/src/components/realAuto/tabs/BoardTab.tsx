import { realTradeApi, type CandidateRow, type GateStatus, type Position } from "../../../realTradeApi";
import MarketBoard from "../../board/MarketBoard";
import ShortlistPanel from "../../board/ShortlistPanel";
import TopPicks from "../../board/TopPicks";
import { riskCap, type RiskCapResult } from "../../board/riskCap";
import type { Mode } from "../types";

// E65 - E31 step 9: the "board" tab, moved out of RealAutoTrade.tsx (it was an inline function in the JSX).
// The only text change: the three panels' `active` prop was the parent's open-tab check and is now plain `active`, because this tab is only mounted while it is the open one.
export interface BoardTabProps {
  mode: Mode;
  status: GateStatus | null;
  positions: Position[];
  candidates: CandidateRow[];
}

export function BoardTab({ mode, status, positions, candidates }: BoardTabProps) {
            const heldSyms = new Set(positions.map(p => p.symbol));
            const candSyms = new Set(candidates.map(c => c.symbol));
            const marks = new Set<string>([...heldSyms, ...candSyms]);
            // group311: "over risk cap" check (display only; mirrors risk_engine checks 5 / 5b). Own proposed stop when the symbol is a
            // candidate with a decision, else the board zone's far edge.
            const acct = { equity: status?.account?.current_equity ?? null, cashAvailable: status?.account?.cash_available ?? null,
                           riskPerTradePct: Number(status?.risk_config?.risk_per_trade_pct) || null };
            const candBy = new Map(candidates.map(c => [c.symbol, c]));
            const capOf = (row: { symbol: string; ltp: number; risk_per_share?: number | null }): RiskCapResult => {
              const d = candBy.get(row.symbol)?.latest_decision;
              return riskCap({ ltp: row.ltp, zoneRiskPerShare: row.risk_per_share, ownEntry: d?.proposed_price, ownStop: d?.proposed_stop,
                               engineVerdict: d?.risk_verdict_reason }, acct);
            };
            return (
              <div className="space-y-3">
                <TopPicks active resetKey={mode} fetcher={() => realTradeApi.topPicks(mode)}
                  title={`Top picks · best 3 to 5 (${mode})`} note="Display only. Entries come from the same ranking; the risk engine and every entry gate still decide. Board-preview cards are not candidates that passed them." />
                <ShortlistPanel active kind="real" resetKey={mode} fetcher={() => realTradeApi.shortlist(mode)}
                  title={`Best picks · next entries (${mode})`} note="Display only. Every entry gate and the risk engine still decide; if a pick's order fails, the next reserve pick takes its slot." />
                <MarketBoard active marks={marks} title={`Real Trade · Market Board (${mode})`}
                  showZoneScore hatchFor={row => capOf(row).status === "over"}
                  legend="Hatched tile = not even one share fits your per-trade risk cap (stop: your candidate's own stop when it has one, else the board zone's far edge). Z = zone score 0-100, a heuristic. Display only: the risk engine decides."
                  popupExtra={row => (
                    <div className="rounded-lg border border-slate bg-ink p-2 text-[11px] text-mist space-y-1">
                      {(() => { const c = capOf(row); return c.status === "unknown" ? null : (
                        <div className={c.status === "over" ? "text-signal-hold" : ""}>{c.status === "over" ? "Over risk cap: " : "Risk cap: "}{c.reason}{" "}
                          <span className="text-[9px]">(stop: {c.source === "own-stop" ? "this service's own" : c.source === "zone-stop" ? "board zone edge, approximate" : "engine verdict"})</span></div>); })()}
                      {heldSyms.has(row.symbol) ? <span className="text-signal-hold">Open {mode} position in this account.</span>
                        : candSyms.has(row.symbol) ? <span className="text-signal-prepare">On the {mode} candidate list.</span>
                        : <span>Not a candidate or position here. Display only: no order can be placed from the board.</span>}
                    </div>
                  )} />
                <div className="ob ob-card" data-testid="real-board-marks">
                  <p className="ob-cap">Your {mode} positions and candidates</p>
                  <div className="ob-chips">
                    {[...heldSyms].map(s => <span key={"p" + s} className="held">{s} · held</span>)}
                    {[...candSyms].filter(s => !heldSyms.has(s)).map(s => <span key={"c" + s}>{s}</span>)}
                    {heldSyms.size + candSyms.size === 0 && <span className="none">None right now.</span>}
                  </div>
                </div>
              </div>
            );
}
