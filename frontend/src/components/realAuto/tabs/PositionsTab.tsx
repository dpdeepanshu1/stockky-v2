import type { PipelineStatus, Position } from "../../../realTradeApi";
import { realTradeApi } from "../../../realTradeApi";
import ApprovalsPanel from "../../ApprovalsPanel";
import { PipelineLiveStatus } from "../panels";
import { PositionCard, SectionHdr } from "../parts";
import type { Mode } from "../types";

// E59 - E31 step 3: the "positions" tab, moved out of RealAutoTrade.tsx (JSX unchanged).
export interface PositionsTabProps {
  mode: Mode;
  loggedIn: boolean;
  pipeline: PipelineStatus | null;
  positions: Position[];
  autoPilotPositions: Position[];
  dematPositions: Position[];
  armed: boolean;
  actionBusy: string | null;
  actionMsg: { ok: boolean; text: string } | null;
  setActionMsg: (m: { ok: boolean; text: string } | null) => void;
  loadPositionsAndOrders: (m: Mode) => Promise<void>;
  doReconcile: () => Promise<void>;
  doClosePosition: (p: Position) => Promise<void>;
}

export function PositionsTab({ mode, loggedIn, pipeline, positions, autoPilotPositions, dematPositions, armed, actionBusy, actionMsg, setActionMsg, loadPositionsAndOrders, doReconcile, doClosePosition }: PositionsTabProps) {
  return (
            <div className="space-y-3">
              {/* ADDED (this session — item 1 from the follow-up list):
                  "Positions tab shows no live run-cycle progress" was
                  flagged as a genuine open item, not a bug in the Pipeline
                  tab itself (that one already works). Reusing the same
                  PipelineLiveStatus component the Pipeline tab renders,
                  fed by the same poll (now also active while this tab is
                  open — see the effect above), so you don't have to
                  switch tabs to see what a running cycle is doing. */}
              {mode === "REAL" && <ApprovalsPanel loggedIn={loggedIn} source={{
                status: realTradeApi.approvalsStatus, approve: realTradeApi.approveEntry,
                reject: realTradeApi.rejectEntry, reset: realTradeApi.resetApprovals }} />}

              <PipelineLiveStatus pipeline={pipeline} mode={mode} />
              <div className="flex items-center justify-between">
                <SectionHdr>Open positions — {mode} ({positions.length})</SectionHdr>
                <div className="flex gap-2">
                  {armed && (
                    <button onClick={() => void doReconcile()} disabled={actionBusy === "reconcile"}
                      className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-signal-prepare/10 border border-signal-prepare/30 text-signal-prepare disabled:opacity-40">
                      {actionBusy === "reconcile" ? "Checking…" : "🔄 Reconcile"}
                    </button>
                  )}
                  {/* BUG FIX (this session — "banner permanently stuck,
                      refresh button feels broken"): this ↻ button always
                      fetched fresh positions/orders correctly, but never
                      touched `actionMsg` — the 409 "exit-side operation
                      already in progress" text set by a single earlier
                      Close/Cancel/Reconcile click had no dismiss button and
                      was only ever cleared at the START of the next such
                      action (see doClosePosition/doCancelOrder/doReconcile
                      above). So one transient 409 left the red banner
                      showing forever, and every click of ↻ silently updated
                      the list underneath it while the frozen banner made it
                      look like nothing had happened. Clearing it here makes
                      refresh visibly do something again. */}
                  <button onClick={() => { setActionMsg(null); void loadPositionsAndOrders(mode); }}
                    className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist">
                    ↻
                  </button>
                </div>
              </div>

              {actionMsg && (
                <div className={`px-3 py-2 rounded-xl border font-display tabular-nums text-xs flex items-center justify-between gap-3 ${actionMsg.ok ? "bg-signal-buy/5 border-signal-buy/20 text-signal-buy" : "bg-signal-sell/5 border-signal-sell/20 text-signal-sell"}`}>
                  <span>{actionMsg.text}</span>
                  {/* BUG FIX (this session): no dismiss control existed on
                      this banner at all — unlike the top-level `error`
                      banner (which has always had a ✕), a failed
                      Close/Cancel/Reconcile left this stuck on screen with
                      no way to clear it short of triggering another one of
                      those three actions. */}
                  <button onClick={() => setActionMsg(null)} className="flex-shrink-0 opacity-70 hover:opacity-100">✕</button>
                </div>
              )}

              {positions.length === 0 ? (
                <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                  <p className="font-display tabular-nums text-sm text-mist">No open positions.</p>
                </div>
              ) : (
                <>
                  {/* AUDIT FIX (session64, issue #8 follow-up): split into
                      "Auto-Pilot Positions" (this service's own BUYs) and
                      "Demat Positions" (pre-existing Dhan holdings imported
                      via portfolio.import_broker_holdings — see that
                      module's docstring) instead of one unlabeled mixed
                      list. Both groups are evaluated by exit_engine every
                      cycle exactly the same way (stop/target, partial exit,
                      trailing) — this is purely a display grouping so it's
                      clear at a glance which positions came from which
                      source; broker_imported ones always sell CNC (see
                      TradePosition.broker_imported) since they were never
                      bought as an intraday MIS position by this app. */}
                  {autoPilotPositions.length > 0 && (
                    <div className="space-y-2">
                      {dematPositions.length > 0 ? (
                        <SectionHdr>Auto-Pilot Positions ({autoPilotPositions.length})</SectionHdr>
                      ) : null}
                      {autoPilotPositions.map(p => (
                        <PositionCard key={p.id} p={p} closing={actionBusy === `close:${p.id}`}
                          onClose={() => void doClosePosition(p)} />
                      ))}
                    </div>
                  )}
                  {dematPositions.length > 0 && (
                    <div className="space-y-2">
                      <SectionHdr>Demat Positions ({dematPositions.length})</SectionHdr>
                      <p className="font-display tabular-nums text-[10px] text-mist -mt-1">
                        Pre-existing Dhan holdings, not bought via auto-pilot — now evaluated for
                        stop/target/partial-exit every cycle just like the positions above.
                      </p>
                      {dematPositions.map(p => (
                        <PositionCard key={p.id} p={p} closing={actionBusy === `close:${p.id}`}
                          onClose={() => void doClosePosition(p)} />
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
  );
}
