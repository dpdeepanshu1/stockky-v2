// E538 (redesign round 5b): the "pipeline" sub-tab of Position Stocks, moved out of PositionStocksTab.tsx (JSX unchanged).
// All state and handlers come from usePositionStocks() through `s`, so the ten sub-tabs still share one set of state.
import { positionStocksApi } from "../../positionStocksApi";
import CloseAllDialog from "../CloseAllDialog";
import ApprovalsPanel from "../ApprovalsPanel";
import { fmtInr, fmtDateTimeIst, PipelineStage, PipelineArrow, RunCycleResultPanel, LivePipelineStatus } from "./parts";
import type { PsState } from "./usePositionStocks";

export function PsPipelineTab({ s }: { s: PsState }) {
  const { status, candidates, ledger, lastCycleResult, pipelineLive, busy, confirmKill, setConfirmKill, showCloseAll, setShowCloseAll, confirmResetDaily, setConfirmResetDaily, candidateLog, loggedIn, loadAll, doAction, openPositions } = s;
  return (
      <>
      {/* ── Status banners ── */}
      {status && !status.service_enabled && (
        <div className="rounded-xl border border-signal-avoid/40 bg-signal-avoid/10 px-3 py-2 font-display tabular-nums text-[11px] text-signal-avoid">
          Module paused — screening and new entries are stopped. Exit reconciliation and 3:00 PM EOD square-off keep running.
        </div>
      )}
      {status && status.service_enabled && !status.auto_pilot_enabled && (
        <div className="rounded-xl border border-signal-hold/40 bg-signal-hold/10 px-3 py-2 font-display tabular-nums text-[11px] text-signal-hold">
          Auto-Pilot off — screener is scanning but won't act automatically. Use "Run Cycle Now" for a manual push.
        </div>
      )}
      {status?.stagnation_exit_enabled && (
        <div className="rounded-xl border border-signal-prepare/40 bg-signal-prepare/10 px-3 py-2 font-display tabular-nums text-[11px] text-signal-prepare">
          Stagnation Exit ON — a position flat within ±{status.pipeline_config?.stagnation_exit_band_pct ?? "?"}% of entry
          for {status.pipeline_config?.stagnation_exit_minutes ?? "?"}m closes early to free capital for a better candidate.
        </div>
      )}
      {status?.breakeven_stop_enabled && (
        <div className="rounded-xl border border-signal-prepare/40 bg-signal-prepare/10 px-3 py-2 font-display tabular-nums text-[11px] text-signal-prepare">
          Breakeven Stop ON — once an open position's unrealized gain reaches {((status.pipeline_config?.breakeven_frac ?? 0.4) * 100).toFixed(0)}%
          of its target, its stop-loss is moved up to entry price to lock in a wash on any reversal.
        </div>
      )}

      <ApprovalsPanel loggedIn={loggedIn} source={{
        status: positionStocksApi.approvalsStatus, approve: positionStocksApi.approveEntry,
        reject: positionStocksApi.rejectEntry, reset: positionStocksApi.resetApprovals }} />

      <LivePipelineStatus live={pipelineLive} />

      {lastCycleResult && <RunCycleResultPanel result={lastCycleResult} />}

      {/* ── Action buttons ── */}
      <div className="flex flex-wrap gap-2">
        <button disabled={!loggedIn || busy !== null || !!status?.service_enabled} onClick={() => doAction("service_enable")}
          className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy disabled:opacity-40">
          {busy === "service_enable" ? "Enabling…" : "Enable Module"}
        </button>
        <button disabled={!loggedIn || busy !== null || !status?.service_enabled} onClick={() => doAction("service_disable")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-paper disabled:opacity-40">
          {busy === "service_disable" ? "Pausing…" : "Pause Module"}
        </button>
        <button disabled={!loggedIn || busy !== null || !!status?.armed} onClick={() => doAction("arm")}
          className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy disabled:opacity-40">
          {busy === "arm" ? "Arming…" : "Arm"}
        </button>
        <button disabled={!loggedIn || busy !== null || !status?.armed} onClick={() => doAction("disarm")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-paper disabled:opacity-40">
          {busy === "disarm" ? "Disarming…" : "Disarm"}
        </button>
        <button disabled={!loggedIn || busy !== null} onClick={() => setShowCloseAll(true)}
          className="px-4 py-2 rounded-xl bg-signal-sell/40 border border-signal-sell/60 font-display tabular-nums text-xs text-paper disabled:opacity-40">
          ⛔ Close all
        </button>
        {showCloseAll && (
          <CloseAllDialog open={showCloseAll}
            title="Close all and disarm — Position Stocks"
            detail="This disarms the module, cancels resting BUY orders and sends a MARKET SELL for every open position. It cannot be undone."
            onCancel={() => { setShowCloseAll(false); void loadAll(); }}
            onConfirm={(phrase) => positionStocksApi.closeAll(phrase)}
          />
        )}
        <button disabled={!loggedIn || busy !== null || !!status?.auto_pilot_enabled} onClick={() => doAction("autopilot_enable")}
          className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy disabled:opacity-40">
          {busy === "autopilot_enable" ? "Enabling…" : "Enable Auto-Pilot"}
        </button>
        <button disabled={!loggedIn || busy !== null || !status?.auto_pilot_enabled} onClick={() => doAction("autopilot_disable")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-paper disabled:opacity-40">
          {busy === "autopilot_disable" ? "Disabling…" : "Disable Auto-Pilot"}
        </button>
        {/* session69: DB-backed toggle for the stagnation early-exit — see
            orders/eod_squareoff.py::run_stagnation_exit's docstring. Off by
            default; closes a flat position early (config.STAGNATION_EXIT_
            MINUTES / _BAND_PCT) instead of waiting for the 15:00 EOD sweep. */}
        <button disabled={!loggedIn || busy !== null || !!status?.stagnation_exit_enabled} onClick={() => doAction("stagnation_exit_enable")}
          className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy disabled:opacity-40">
          {busy === "stagnation_exit_enable" ? "Enabling…" : "Enable Stagnation Exit"}
        </button>
        <button disabled={!loggedIn || busy !== null || !status?.stagnation_exit_enabled} onClick={() => doAction("stagnation_exit_disable")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-paper disabled:opacity-40">
          {busy === "stagnation_exit_disable" ? "Disabling…" : "Disable Stagnation Exit"}
        </button>
        {/* this session ("breakeven stop is dead code — fix it"): DB-backed
            toggle for the breakeven-stop management — see
            orders/breakeven.py::run_breakeven_stop's docstring. Off by
            default; once an OPEN position's unrealized gain crosses its
            recorded trigger % (orders/adaptive.py's BREAKEVEN_FRAC, 40% of
            target by default) its stop-loss leg is moved up to entry price. */}
        <button disabled={!loggedIn || busy !== null || !!status?.breakeven_stop_enabled} onClick={() => doAction("breakeven_stop_enable")}
          className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy disabled:opacity-40">
          {busy === "breakeven_stop_enable" ? "Enabling…" : "Enable Breakeven Stop"}
        </button>
        <button disabled={!loggedIn || busy !== null || !status?.breakeven_stop_enabled} onClick={() => doAction("breakeven_stop_disable")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-paper disabled:opacity-40">
          {busy === "breakeven_stop_disable" ? "Disabling…" : "Disable Breakeven Stop"}
        </button>
        <button disabled={!loggedIn || busy !== null || !status?.armed || !status?.service_enabled} onClick={() => doAction("run_cycle")}
          className="px-4 py-2 rounded-xl bg-signal-prepare/20 border border-signal-prepare/40 font-display tabular-nums text-xs text-signal-prepare disabled:opacity-40">
          {busy === "run_cycle" ? "Running…" : "Run Cycle Now"}
        </button>
        <button disabled={!loggedIn || busy !== null} onClick={() => doAction("sync")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist disabled:opacity-40">
          {busy === "sync" ? "Syncing…" : "Sync Capital from Dhan"}
        </button>
        <button disabled={!loggedIn || busy !== null} onClick={() => doAction("reconcile")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist disabled:opacity-40">
          {busy === "reconcile" ? "Checking…" : "Check Exits Now"}
        </button>
        {!confirmResetDaily ? (
          <button disabled={!loggedIn || busy !== null} onClick={() => setConfirmResetDaily(true)}
            className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist disabled:opacity-40">
            Reset Daily Ledger
          </button>
        ) : (
          <div className="flex items-center gap-2">
            <span className="font-display tabular-nums text-[11px] text-signal-sell">Clear today's P&amp;L + kill switch?</span>
            <button onClick={() => doAction("reset_daily")} className="px-3 py-2 rounded-xl bg-signal-sell/30 border border-signal-sell font-display tabular-nums text-xs text-signal-sell">Confirm</button>
            <button onClick={() => setConfirmResetDaily(false)} className="px-3 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist">Cancel</button>
          </div>
        )}
        {!confirmKill ? (
          <button disabled={!loggedIn || busy !== null} onClick={() => setConfirmKill(true)}
            className="px-4 py-2 rounded-xl bg-signal-sell/20 border border-signal-sell/40 font-display tabular-nums text-xs text-signal-sell disabled:opacity-40 ml-auto">
            Kill Switch
          </button>
        ) : (
          <div className="flex items-center gap-2 ml-auto">
            <span className="font-display tabular-nums text-[11px] text-signal-sell">Disarm + trip daily kill switch?</span>
            <button onClick={() => doAction("kill")} className="px-3 py-2 rounded-xl bg-signal-sell/30 border border-signal-sell font-display tabular-nums text-xs text-signal-sell">Confirm</button>
            <button onClick={() => setConfirmKill(false)} className="px-3 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist">Cancel</button>
          </div>
        )}
      </div>
      {!loggedIn && (
        <p className="font-display tabular-nums text-[10px] text-mist -mt-2">
          Action buttons stay off until this page has an admin session. Use "Log in again" at the top of the page.
        </p>
      )}

      {/* ── Run Cycle / Auto-Pilot pipeline dashboard ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          <p className="dash-section-title">Run Cycle / Auto-Pilot — how a trade actually gets picked</p>
          <div className="flex items-center gap-3 font-display tabular-nums text-[10px]">
            <span className={status?.auto_pilot_enabled ? "text-signal-buy" : "text-mist"}>
              ● Auto-Pilot {status?.auto_pilot_enabled ? "ON" : "OFF"}
            </span>
            <span className={status?.market_open ? "text-signal-buy" : "text-signal-hold"}>
              ● Market {status?.market_open ? "Open" : "Closed"}
            </span>
          </div>
        </div>
        <p className="font-display tabular-nums text-[11px] text-mist mb-3">
          When Auto-Pilot is ON and the market is open, this whole pipeline runs automatically every{" "}
          <span className="text-paper font-bold">{status?.pipeline_config?.scan_interval_s ?? 10}s</span> — that's
          the "Run Cycle" a manual click on the Overview tab also triggers on demand, once, right away.
          Last automatic/manual run: <span className="text-paper">{status?.last_cycle_run_at ? fmtDateTimeIst(status.last_cycle_run_at) : "never yet"}</span>
          {status?.last_cycle_run_trigger && <span className="text-mist"> ({status.last_cycle_run_trigger})</span>}.
        </p>

        <div className="flex flex-col sm:flex-row gap-1.5 items-stretch">
          <PipelineStage
            n={1} title="WS Feed" active={!!status?.ws?.connected}
            metric={status?.ws?.subscribed_symbols ?? "—"} metricLabel="symbols live"
            detail={[
              "Every NSE-EQ stock, one shared WebSocket (LTP mode).",
              status?.ws?.last_tick_at ? `Last tick: ${fmtDateTimeIst(status.ws.last_tick_at)}` : "No tick yet.",
            ]}
          />
          <PipelineArrow />
          <PipelineStage
            n={2} title="Scan (all 4 windows)" active={(candidates?.length ?? 0) > 0}
            metric={candidates?.length ?? 0} metricLabel="passed"
            detail={[
              `1m ≥ ${status?.pipeline_config?.windows["1m"] ?? "0.5"}%  ·  5m ≥ ${status?.pipeline_config?.windows["5m"] ?? "1.0"}%`,
              `15m ≥ ${status?.pipeline_config?.windows["15m"] ?? "1.5"}%  ·  60m ≥ ${status?.pipeline_config?.windows["60m"] ?? "2.5"}%`,
              `+ min activity floor (≈${status?.pipeline_config?.min_avg_volume ?? 50000} avg vol proxy)`,
            ]}
          />
          <PipelineArrow />
          <PipelineStage
            n={3} title="Composite Rank" active={(candidates?.length ?? 0) > 0}
            metric={candidates?.length ? `Top ${status?.pipeline_config?.quality_gate_top_n ?? 3}` : "—"}
            detail={[
              "score = %change × min(activity / floor, 3×)",
              "Every passing symbol across all windows, ranked into one list — best score wins regardless of which window found it.",
            ]}
          />
          <PipelineArrow />
          <PipelineStage
            n={4} title="Quality Gate" active={status?.pipeline_config?.quality_gate_enabled ?? true}
            metric={candidateLog.length ? candidateLog.filter(c => c.decision === "ENTERED").length : "—"}
            metricLabel={`entered / ${candidateLog.length || 0} logged`}
            detail={[
              `Fundamental ≥ ${status?.pipeline_config?.min_fundamental_score ?? 40}, Technical ≥ ${status?.pipeline_config?.min_technical_score ?? 40}`,
              `Market cap ≥ ₹${status?.pipeline_config?.min_market_cap_cr ?? 500}cr (excludes micro-caps)`,
              "Checked against analysis-intelligence-service, best-effort with timeout.",
            ]}
          />
          <PipelineArrow />
          <PipelineStage
            n={5} title="Entry Decision" active={openPositions.length > 0}
            metric={`${openPositions.length}/${status?.max_concurrent_scalp_positions ?? 5}`} metricLabel="open slots used"
            detail={[
              `Risk ${status?.risk_per_trade_pct ?? "—"}% of pool per trade`,
              ledger ? `Pool available: ${fmtInr(ledger.available_capital)}` : "Pool capital not loaded.",
              status?.daily_loss_kill_switch ? "⚠ Daily loss kill-switch TRIPPED — no new entries." : "Kill-switch clear.",
            ]}
          />
        </div>
      </div>

      {/* ── Why the shortlist stays narrow ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-2">Why the list stays short (narrowed down, not the whole market)</p>
        <p className="font-display tabular-nums text-[11px] text-mist leading-relaxed">
          Every NSE-EQ symbol is scanned every cycle (Stage 1+2 above) — nothing is skipped — but only symbols
          clearing <span className="text-paper">all</span> of: the window's %-change floor, the activity/liquidity
          floor, and not already an open position, ever become a "candidate". Of those, only the top{" "}
          <span className="text-paper">{status?.pipeline_config?.quality_gate_top_n ?? 3}</span> by composite score
          go to the Quality Gate (Stage 4) — which then narrows further on fundamentals/technicals/market-cap/catalyst.
          What survives all of that is what actually gets sized and sent to Dhan. See the Candidate Log below the
          Screener tab for the full "entered vs skipped, and why" trail — it's the audit record of every one of those decisions.
        </p>
      </div>
      </>
      );
}
