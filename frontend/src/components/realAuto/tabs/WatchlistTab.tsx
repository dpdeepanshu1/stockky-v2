import { realTradeApi, type CandidateRow } from "../../../realTradeApi";
import { coveragePct, gateStatus, tally as structureTally } from "../../../structureView";
import StructureGateCard from "../../StructureGateCard";
import StructurePanel from "../../StructurePanel";
import { SOURCE_LABELS } from "../panels";
import { fmtDate, fmtTime } from "../format";
import { SectionHdr } from "../parts";
import type { Mode } from "../types";

// E60 - E31 step 4: the "watchlist" tab, moved out of RealAutoTrade.tsx (JSX unchanged).
export interface WatchlistTabProps {
  mode: Mode;
  loggedIn: boolean;
  candidates: CandidateRow[];
  candidatesLoading: boolean;
  expandedCandidateId: number | null;
  setExpandedCandidateId: (id: number | null) => void;
  loadCandidates: (m: Mode) => Promise<void>;
}

export function WatchlistTab({ mode, loggedIn, candidates, candidatesLoading, expandedCandidateId, setExpandedCandidateId, loadCandidates }: WatchlistTabProps) {
  return (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <SectionHdr>Candidate watchlist — {mode}</SectionHdr>
                <button onClick={() => void loadCandidates(mode)} disabled={candidatesLoading}
                  className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist disabled:opacity-40">
                  {candidatesLoading ? "…" : "↻"}
                </button>
              </div>
              <p className="font-display tabular-nums text-[10px] text-mist -mt-2">
                Stocks pulled from Hot Picks / IPO / market scan, evaluated by the risk engine each cycle.
              </p>

              {/* group355: how the daily-structure gate is behaving for this mode (read-only; REAL needs the admin session) */}
              <StructureGateCard key={`sg-${mode}-${loggedIn ? "in" : "out"}`} kind="real" title={`Daily structure gate — ${mode}`}
                load={(h) => (mode === "REAL" && !loggedIn ? Promise.reject(new Error("Log in as admin to see the REAL structure gate status.")) : realTradeApi.structureStatus(mode, h))} />

              {candidates.length === 0 ? (
                <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                  <p className="font-display tabular-nums text-sm text-mist">
                    {candidatesLoading ? "Loading…" : "No candidates fetched yet — run a cycle or wait for Auto-Pilot."}
                  </p>
                </div>
              ) : (
                <div className="space-y-2">
                  {(() => {
                    // group351: how the daily-structure gate treated the evaluated candidates (display only).
                    const t = structureTally(candidates);
                    if (t.checked === 0) return null;
                    const cov = coveragePct(t);
                    return (
                      <p className="font-display tabular-nums text-[10px] text-mist" data-testid="structure-tally">
                        Daily structure: {t.blocked} blocked · {t.shadow} would reject (shadow) · {t.noOpinion} no data
                        {cov != null ? ` · coverage ${cov}%` : ""}
                      </p>
                    );
                  })()}
                  {candidates.map(c => {
                    const d = c.latest_decision;
                    const actionColor = d?.action === "ENTER" ? "text-signal-buy"
                      : d?.action === "WAIT" ? "text-signal-hold"
                      : d?.risk_verdict === "REJECTED" || d?.risk_verdict === "BLOCKED_GLOBAL" ? "text-signal-sell"
                      : "text-mist";
                    const expanded = expandedCandidateId === c.id;
                    return (
                      <div key={c.id} className="bg-graphite border border-slate rounded-2xl p-4">
                        <button
                          onClick={() => setExpandedCandidateId(expanded ? null : c.id)}
                          className="flex items-start justify-between mb-2 w-full text-left"
                        >
                          <div>
                            <span className="font-display tabular-nums text-base font-bold text-paper">{c.symbol}</span>
                            {c.fetch_count > 1 && (
                              <span className="font-display tabular-nums text-[9px] text-mist ml-2 px-1.5 py-0.5 rounded bg-ink border border-slate" title={`Seen ${c.fetch_count} times in this window`}>
                                ×{c.fetch_count}
                              </span>
                            )}
                            {c.source_tab && (
                              <span className="font-display tabular-nums text-[9px] text-mist ml-2 uppercase">{SOURCE_LABELS[c.source_tab] || c.source_tab}</span>
                            )}
                            {c.decision_label && (
                              <span className="font-display tabular-nums text-[9px] text-signal-prepare ml-2">{c.decision_label}</span>
                            )}
                          </div>
                          <span className="flex items-center gap-2">
                            <span className={`font-display tabular-nums text-[11px] font-bold ${actionColor}`}>
                              {d ? d.action : "not yet evaluated"}
                            </span>
                            <span className="font-display tabular-nums text-[9px] text-mist">{expanded ? "▲" : "▼"}</span>
                          </span>
                        </button>

                        <div className="grid grid-cols-4 gap-2 font-display tabular-nums text-[10px] text-mist mb-2">
                          <div>Signal<br/><span className="text-paper font-bold">{c.signal_price ? `₹${c.signal_price}` : "—"}</span></div>
                          <div>
                            {d?.action === "WAIT" ? "Waiting at" : "Entry limit"}
                            <br/>
                            <span className="text-signal-hold font-bold">{d?.proposed_price ? `₹${d.proposed_price}` : "—"}</span>
                          </div>
                          <div>
                            Current
                            <br/>
                            <span className="text-paper font-bold">
                              {c.current_price != null ? `₹${c.current_price.toFixed(2)}` : "—"}
                              {d?.limit_distance_pct != null && (
                                <span className={`ml-1 text-[9px] ${d.limit_distance_pct <= 0 ? "text-signal-buy" : "text-mist"}`}>
                                  ({d.limit_distance_pct > 0 ? "+" : ""}{d.limit_distance_pct}%)
                                </span>
                              )}
                            </span>
                          </div>
                          <div>Stop loss<br/><span className="text-signal-sell font-bold">{d?.proposed_stop ? `₹${d.proposed_stop}` : "—"}</span></div>
                        </div>

                        {d?.reasoning && (
                          <p className="font-display tabular-nums text-[10px] text-mist border-t border-slate pt-2 mt-1">
                            {d.risk_verdict && <span className={`font-bold mr-1 ${d.risk_verdict === "APPROVED" ? "text-signal-buy" : "text-signal-sell"}`}>[{d.risk_verdict}]</span>}
                            {/* 2026-09-18 fix (follow-on item #6): badges a
                                Gate 5.6 (cost-model) WAIT distinctly from an
                                ordinary risk-engine WAIT, instead of only
                                being distinguishable by reading the prose. */}
                            {d.gate_tag === "cost_model" && (
                              <span className="font-bold mr-1 text-signal-hold">[COST GATE]</span>
                            )}
                            {(d.gate_tag === "structure" || d.gate_tag === "structure_nodata") && (
                              <span className="font-bold mr-1 text-signal-hold">[STRUCTURE GATE]</span>
                            )}
                            {d.reasoning}
                          </p>
                        )}

                        {d?.structure && !expanded && d.structure.would_reject && !d.structure.enforced && (
                          <p className="font-display tabular-nums text-[10px] text-signal-hold mt-1">
                            Structure: {gateStatus(d.structure).label}
                          </p>
                        )}

                        {expanded && (
                          <div className="border-t border-slate pt-2 mt-2 space-y-2">
                            <div className="grid grid-cols-3 gap-2 font-display tabular-nums text-[10px] text-mist">
                              <div>Conviction<br/><span className="text-paper font-bold">{c.conviction_score != null ? c.conviction_score.toFixed(1) : "—"}</span></div>
                              <div>Proposed qty<br/><span className="text-paper font-bold">{d?.proposed_qty ?? "—"}</span></div>
                              <div>Target<br/><span className="text-signal-buy font-bold">{d?.proposed_target ? `₹${d.proposed_target}` : "—"}</span></div>
                            </div>
                            {d?.risk_verdict_reason && d.risk_verdict_reason !== d.reasoning && (
                              <p className="font-display tabular-nums text-[10px] text-mist">
                                <span className="text-mist">Risk engine verdict — </span>{d.risk_verdict_reason}
                              </p>
                            )}
                            {d?.structure && <StructurePanel snapshot={d.structure} atrStop={d.proposed_stop} />}
                            <p className="font-display tabular-nums text-[9px] text-mist">Candidate #{c.id}{d ? " · has been evaluated" : " · awaiting first evaluation"}</p>
                          </div>
                        )}

                        <div className="flex items-center justify-between mt-2">
                          <span className="font-display tabular-nums text-[9px] text-mist">
                            Fetched {fmtDate(c.received_at)} {fmtTime(c.received_at)}
                            {c.consumed ? "" : " · not yet evaluated"}
                          </span>
                          {d && <span className="font-display tabular-nums text-[9px] text-mist">Evaluated {fmtTime(d.evaluated_at)}</span>}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
  );
}
