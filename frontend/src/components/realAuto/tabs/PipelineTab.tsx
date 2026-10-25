import type { CycleResult, GateStatus, Position, OrderRow } from "../../../realTradeApi";
import ManualTradeTicket from "../../trading/ManualTradeTicket";
import { CatalystWatchlistPanel, OrderFlowDiagram, PipelineLiveStatus } from "../panels";
import { SectionHdr, StatCard } from "../parts";
import type { Mode } from "../types";
import type { PipelineStatus } from "../../../realTradeApi";

// E62 - E31 step 6: the "pipeline" (Run Cycle) tab, moved out of RealAutoTrade.tsx (JSX unchanged).
export type PipelineFeature = "prepick" | "enter_at_open" | "eod_squareoff" | "eod_signal_scan" | "afterhours_news_scan" | "overnight_hold";

export interface PipelineTabProps {
  mode: Mode;
  loggedIn: boolean;
  status: GateStatus | null;
  armed: boolean;
  gate1: boolean;
  pipeline: PipelineStatus | null;
  positions: Position[];
  orders: OrderRow[];
  cycleBusy: boolean;
  cycleResult: CycleResult | null;
  afterhoursRunBusy: boolean;
  autoPilotBusy: boolean;
  featureBusy: string | null;
  doRunCycle: () => Promise<void>;
  doRunAfterhoursScan: () => Promise<void>;
  doToggleAutoPilot: () => Promise<void>;
  doToggleFeature: (feature: PipelineFeature, currentlyEnabled: boolean) => Promise<void>;
  loadStatus: (m: Mode) => Promise<void>;
  loadPositionsAndOrders: (m: Mode) => Promise<void>;
}

export function PipelineTab({ mode, loggedIn, status, armed, gate1, pipeline, positions, orders, cycleBusy, cycleResult, afterhoursRunBusy, autoPilotBusy, featureBusy, doRunCycle, doRunAfterhoursScan, doToggleAutoPilot, doToggleFeature, loadStatus, loadPositionsAndOrders }: PipelineTabProps) {
  return (
            <div className="space-y-4">
              <OrderFlowDiagram mode={mode} />

              <PipelineLiveStatus pipeline={pipeline} mode={mode} />

              <CatalystWatchlistPanel mode={mode} loggedIn={loggedIn} />

              <div className="bg-graphite border border-slate rounded-2xl p-4">
                <div className="flex items-center justify-between mb-3">
                  <div>
                    <SectionHdr>AI Trade pipeline — {mode}</SectionHdr>
                    <p className="font-display tabular-nums text-[10px] text-mist">Candidate → Risk → {mode === "REAL" ? "Dhan Order" : "Simulated Fill"} → Position → Exit</p>
                  </div>
                  {armed ? (
                    <button onClick={() => void doRunCycle()} disabled={cycleBusy}
                      className="px-4 py-2 rounded-xl bg-signal-buy/10 border border-signal-buy/30 font-display tabular-nums text-xs text-signal-buy disabled:opacity-40">
                      {cycleBusy ? "Running…" : "▶ Run Cycle"}
                    </button>
                  ) : (
                    <span className="font-display tabular-nums text-[10px] text-mist">Arm first to run cycles</span>
                  )}
                </div>

                {mode === "REAL" && (
                  <p className="font-display tabular-nums text-[10px] text-signal-hold/70 mb-3 bg-signal-hold/5 rounded-xl px-3 py-2 border border-signal-hold/20">
                    REAL mode: orders placed live at Dhan. Reconcile runs automatically at end of each cycle.
                  </p>
                )}

                {/* Auto-Pilot — runs this same cycle on a server-side timer
                    (market hours only) so it keeps working with this page
                    closed. Telegram must be configured on the backend
                    (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) to get notified. */}
                <div className={`flex items-center justify-between mb-3 px-3 py-2 rounded-xl border ${
                  status?.auto_pilot_enabled ? "bg-signal-buy/5 border-signal-buy/20" : "bg-ink border-slate"
                }`}>
                  <div>
                    <p className="font-display tabular-nums text-[11px] text-paper">
                      🤖 Auto-Pilot {status?.auto_pilot_enabled ? <span className="text-signal-buy">ON</span> : <span className="text-mist">OFF</span>}
                    </p>
                    <p className="font-display tabular-nums text-[9px] text-mist">
                      {status?.auto_pilot_enabled
                        ? "Running this cycle automatically during market hours — Telegram notifies you of every action."
                        : "Off — Run Cycle only fires when you click it here."}
                    </p>
                  </div>
                  <button
                    onClick={() => void doToggleAutoPilot()}
                    disabled={!armed || autoPilotBusy}
                    className={`px-4 py-2 rounded-xl font-display tabular-nums text-xs disabled:opacity-40 ${
                      status?.auto_pilot_enabled
                        ? "bg-signal-sell/10 border border-signal-sell/30 text-signal-sell"
                        : "bg-signal-buy/10 border border-signal-buy/30 text-signal-buy"
                    }`}
                  >
                    {autoPilotBusy ? "…" : status?.auto_pilot_enabled ? "Turn Off" : "Turn On"}
                  </button>
                </div>
                {!armed && (
                  <p className="font-display tabular-nums text-[10px] text-mist mb-3">Arm {mode} first to enable Auto-Pilot.</p>
                )}

                {/* Scheduled Automation (2026-08-31) — three optional time-of-day
                    features layered on top of Auto-Pilot. This per-mode switch is
                    the SOLE on/off authority (the server-side env kill-switch was
                    removed 2026-09-01), plus the mode being armed, and fires at
                    most once per trading day. All default OFF — turn on in DEMO
                    first to prove the flow before enabling for REAL money. */}
                {status?.scheduled_automation && (
                  <div className="mb-3 rounded-xl border border-slate bg-ink/60 overflow-hidden">
                    <div className="px-3 py-2 border-b border-slate bg-graphite/40">
                      <p className="font-display tabular-nums text-[11px] text-paper">⏰ Scheduled Automation</p>
                      <p className="font-display tabular-nums text-[9px] text-mist mt-0.5">
                        Pre-pick the best stocks before the open, auto-enter at market open, square off before close,
                        {" "}and scan for tomorrow's overnight-priority picks right after that.
                        {" "}Fires once per trading day. Test in DEMO before enabling for REAL.
                      </p>
                    </div>
                    <div className="divide-y divide-slate">
                      {([
                        { key: "prepick" as const, icon: "🌅", title: "Pre-pick (pre-open)",
                          desc: "Warm the candidate queue so the strongest names are ready the moment the market opens. No order is placed." },
                        { key: "enter_at_open" as const, icon: "🚀", title: "Enter at open",
                          desc: "Run one full entry cycle just after the open so pre-picked names get entered at the early price." },
                        { key: "eod_squareoff" as const, icon: "🌆", title: "EOD square-off",
                          desc: "Close open positions before the close. When Selective overnight hold (below) is ON, a narrow, capped subset with a real backtested Day+1 edge is kept open instead of flattened. The swing lane also needs this ON: it runs the 15:00 hold-or-sell decision for swing positions, and live swing entries are refused while it is OFF (Calibration → Swing lane shows this under Wiring)." },
                        { key: "eod_signal_scan" as const, icon: "🌙", title: "EOD signal scan",
                          desc: "Right after square-off, re-scan for strong positive signals (news/results/events/momentum) and queue the best few as an overnight-priority pick — bought first thing at tomorrow's open, not today." },
                      ]).map(f => {
                        const st = status.scheduled_automation![f.key];
                        const on = st.enabled;
                        const busy = featureBusy === f.key;
                        return (
                          <div key={f.key} className={`flex items-start justify-between gap-3 px-3 py-2.5 ${on ? "bg-signal-buy/[0.04]" : ""}`}>
                            <div className="min-w-0">
                              <p className="font-display tabular-nums text-[11px] text-paper">
                                {f.icon} {f.title}{" "}
                                <span className="text-mist">· {st.time_ist} IST</span>{" "}
                                {on
                                  ? <span className="text-signal-buy">ON</span>
                                  : <span className="text-mist">OFF</span>}
                              </p>
                              <p className="font-display tabular-nums text-[9px] text-mist mt-0.5">{f.desc}</p>
                              {st.last_run && (
                                <p className="font-display tabular-nums text-[9px] text-mist mt-0.5">Last ran: {st.last_run}</p>
                              )}
                            </div>
                            <button
                              onClick={() => void doToggleFeature(f.key, on)}
                              disabled={!armed || busy}
                              className={`shrink-0 px-3 py-1.5 rounded-xl font-display tabular-nums text-[11px] disabled:opacity-40 ${
                                on
                                  ? "bg-signal-sell/10 border border-signal-sell/30 text-signal-sell"
                                  : "bg-signal-buy/10 border border-signal-buy/30 text-signal-buy"
                              }`}
                            >
                              {busy ? "…" : on ? "Turn Off" : "Turn On"}
                            </button>
                          </div>
                        );
                      })}
                      {/* 2026-09-18 fix (user report: "no new toggle shows"). Rendered
                          separately from the .map() above (like afterhours_news_scan
                          below) because it has no time_ist/last_run of its own — it's
                          a modifier on what eod_squareoff does at ITS scheduled time,
                          not a separately-scheduled feature. Defaults true — see
                          TradeGateState.overnight_hold_enabled's docstring. */}
                      {status.scheduled_automation!.overnight_hold && (() => {
                        const oh = status.scheduled_automation!.overnight_hold!;
                        const on = oh.enabled;
                        const busy = featureBusy === "overnight_hold";
                        return (
                          <div className={`flex items-start justify-between gap-3 px-3 py-2.5 ${on ? "bg-signal-buy/[0.04]" : ""}`}>
                            <div className="min-w-0">
                              <p className="font-display tabular-nums text-[11px] text-paper">
                                🌙 Selective overnight hold{" "}
                                {on
                                  ? <span className="text-signal-buy">ON</span>
                                  : <span className="text-mist">OFF</span>}
                              </p>
                              <p className="font-display tabular-nums text-[9px] text-mist mt-0.5">
                                Applies at EOD square-off time. When ON, a narrow, capped subset of open
                                positions with a real backtested Day+1 edge (HIGH_CONVICTION / UPPER_CIRCUIT,
                                at/above breakeven, not extended near today's high) skips square-off and is
                                held overnight instead of flattened. When OFF, EOD square-off closes every
                                open position exactly as it always did before this feature existed.
                              </p>
                            </div>
                            <button
                              onClick={() => void doToggleFeature("overnight_hold", on)}
                              disabled={!armed || busy}
                              className={`shrink-0 px-3 py-1.5 rounded-xl font-display tabular-nums text-[11px] disabled:opacity-40 ${
                                on
                                  ? "bg-signal-sell/10 border border-signal-sell/30 text-signal-sell"
                                  : "bg-signal-buy/10 border border-signal-buy/30 text-signal-buy"
                              }`}
                            >
                              {busy ? "…" : on ? "Turn Off" : "Turn On"}
                            </button>
                          </div>
                        );
                      })()}
                      {/* 2026-09-17 (session56): after-hours RSS news scan. Rendered
                          separately from the .map() above because its status shape
                          (window_ist/interval_seconds/finalize_time_ist) differs from
                          the once-a-day features' (time_ist/last_run) — it runs on a
                          recurring interval across an overnight window instead. */}
                      {status.scheduled_automation!.afterhours_news_scan && (() => {
                        const ah = status.scheduled_automation!.afterhours_news_scan!;
                        const on = ah.enabled;
                        const busy = featureBusy === "afterhours_news_scan";
                        // 2026-09-17 (session58): verification symbol — green
                        // dot + timestamp if the last run (scheduled OR
                        // manual, whichever was most recent) completed ok,
                        // red dot + timestamp if it errored, grey "Never run"
                        // if last_run_at is still null (fresh deploy / never
                        // fired yet).
                        const lastRunAt = ah.last_run_at ? new Date(ah.last_run_at) : null;
                        const lastRunOk = ah.last_run_ok;
                        return (
                          <div className={`flex items-start justify-between gap-3 px-3 py-2.5 ${on ? "bg-signal-buy/[0.04]" : ""}`}>
                            <div className="min-w-0">
                              <p className="font-display tabular-nums text-[11px] text-paper">
                                📰 After-hours news scan{" "}
                                <span className="text-mist">· {ah.window_ist} IST</span>{" "}
                                {on
                                  ? <span className="text-signal-buy">ON</span>
                                  : <span className="text-mist">OFF</span>}
                              </p>
                              <p className="font-display tabular-nums text-[9px] text-mist mt-0.5">
                                Scans Moneycontrol/LiveMint/ET every {Math.round(ah.interval_seconds / 60)} min for
                                {" "}catalyst news, ranks by symbol, and finalizes the top {ah.max_candidates} at{" "}
                                {ah.finalize_time_ist} IST for tomorrow's pre-pick. No order is placed here.
                              </p>
                              <div className="flex items-center gap-1.5 mt-1.5">
                                <span
                                  className={`inline-block w-1.5 h-1.5 rounded-full ${
                                    lastRunAt == null ? "bg-mist" : lastRunOk ? "bg-signal-buy" : "bg-signal-sell"
                                  }`}
                                />
                                <p className="font-display tabular-nums text-[9px] text-mist">
                                  {lastRunAt == null
                                    ? "Never run yet"
                                    : `${lastRunOk ? "Last run OK" : "Last run FAILED"} · ${lastRunAt.toLocaleString()}`}
                                </p>
                              </div>
                            </div>
                            <div className="shrink-0 flex flex-col items-end gap-1.5">
                              <button
                                onClick={() => void doToggleFeature("afterhours_news_scan", on)}
                                disabled={!armed || busy}
                                className={`px-3 py-1.5 rounded-xl font-display tabular-nums text-[11px] disabled:opacity-40 ${
                                  on
                                    ? "bg-signal-sell/10 border border-signal-sell/30 text-signal-sell"
                                    : "bg-signal-buy/10 border border-signal-buy/30 text-signal-buy"
                                }`}
                              >
                                {busy ? "…" : on ? "Turn Off" : "Turn On"}
                              </button>
                              <button
                                onClick={() => void doRunAfterhoursScan()}
                                disabled={afterhoursRunBusy}
                                title="Run the after-hours scan right now, regardless of the toggle above or the 15:45–08:45 window."
                                className="px-3 py-1.5 rounded-xl font-display tabular-nums text-[11px] disabled:opacity-40 bg-signal-prepare/10 border border-signal-prepare/30 text-signal-prepare"
                              >
                                {afterhoursRunBusy ? "Running…" : "▶ Run Now"}
                              </button>
                            </div>
                          </div>
                        );
                      })()}
                    </div>
                    {!armed && (
                      <p className="font-display tabular-nums text-[9px] text-mist px-3 py-2 border-t border-slate">
                        Arm {mode} first to enable scheduled automation.
                      </p>
                    )}
                  </div>
                )}

                {cycleBusy && (
                  <div className="h-1 w-full bg-ink rounded-full overflow-hidden mb-3">
                    <div className="h-full w-1/3 bg-signal-buy/60 animate-pulse rounded-full" />
                  </div>
                )}

                {(cycleResult || cycleBusy) && (
                  <div className="grid grid-cols-3 gap-2">
                    {[
                      { label: "Candidates", value: cycleResult?.new_candidates, color: "text-signal-prepare" },
                      { label: "Entered", value: cycleResult?.entry.entered, color: "text-signal-buy" },
                      { label: "Waited", value: cycleResult?.entry.waited, color: "text-signal-hold" },
                      { label: "Rejected", value: cycleResult?.entry.rejected, color: "text-signal-sell" },
                      { label: "Fills", value: cycleResult?.fills, color: "text-signal-buy" },
                      { label: "Expired", value: cycleResult?.expired_orders, color: "text-mist" },
                      { label: "Held", value: cycleResult?.exit.held, color: "text-mist" },
                      { label: "Trailed", value: cycleResult?.exit.trailed, color: "text-signal-prepare" },
                      { label: "Part. exit", value: cycleResult?.exit.partial_exits, color: "text-signal-hold" },
                      { label: "Closed", value: cycleResult?.exit.full_exits, color: "text-signal-buy" },
                      { label: "Time-stop", value: cycleResult?.exit.time_stops, color: "text-signal-sell" },
                    ].map(s => (
                      <StatCard key={s.label} label={s.label}
                        value={cycleBusy && s.value == null ? "…" : (s.value ?? 0)}
                        color={cycleBusy && s.value == null ? "text-mist animate-pulse" : s.color} />
                    ))}
                  </div>
                )}
              </div>

              {/* Manual trade ticket */}
              {(mode === "DEMO" || gate1) && (
                <ManualTradeTicket mode={mode} armed={armed}
                  onOrderComplete={() => { void loadPositionsAndOrders(mode); void loadStatus(mode); }} />
              )}
            </div>
  );
}
