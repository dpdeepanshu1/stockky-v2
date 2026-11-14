// E538 (redesign round 5b): the "overview" sub-tab of Position Stocks, moved out of PositionStocksTab.tsx (JSX unchanged).
// All state and handlers come from usePositionStocks() through `s`, so the ten sub-tabs still share one set of state.
import { positionStocksApi, type ScalpStatus } from "../../positionStocksApi";
import { ArmStepper } from "../trading/ArmStepper";
import DhanMoneyCard from "../DhanMoneyCard";
import { afterCharges } from "../../moneyView";
import { fmtChargeDay, fmtInr, fmtHms, pickNum, fmtDateTimeIst, CapitalBar, CBadge, psOpenCharges } from "./parts";
import type { PsState } from "./usePositionStocks";

export function PsOverviewTab({ s }: { s: PsState }) {
  const { status, positions, ledger, cumCharges, dhanAccount, subTab, loggedIn, loadDhanAccount, dhanTokenSecondsRemaining, openPositions, killSwitchTripped } = s;
  return (
      <>
      {/* AUDIT ADD (this session): highest-priority real-money banner on the
          whole tab — deliberately placed first, above the arming sequence,
          since this service has no Telegram/webhook notification channel
          (unlike real-trade-service's notify_async EOD alert) to surface
          this any other way. See GET /status's eod_squareoff_stragglers. */}
      {!!status?.eod_squareoff_stragglers && (
        <div className="rounded-xl border border-signal-sell/50 bg-signal-sell/15 px-3 py-2 font-display tabular-nums text-[11px] text-signal-sell">
          ⚠ {status.eod_squareoff_stragglers} position(s) still OPEN after today's 3:00 PM EOD square-off —
          the forced flatten failed for at least one. Check the Positions tab and close manually if needed.
        </div>
      )}
      {/* ── Arming sequence ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">Arming Sequence</p>
        <div className="grid grid-cols-2 gap-2">
          {/* E537: the four gates as a 3-step stepper */}
          <ArmStepper gate1={loggedIn} gate2={!!dhanAccount?.connected} gate3={!!status?.risk_confirmed} armed={!!status?.armed} />
        </div>
        {/* AUDIT FIX (this session): backend GET /status has always returned
            armed_at (gate.armed_at, iso_utc'd) and it's typed on ScalpStatus,
            but no component ever rendered it — same "built on the backend,
            never wired to the tab" pattern this service's audits keep
            finding (candidate log, shared order budget, reset-daily button,
            order ids). Surfacing it here since it's exactly the kind of
            fact this card already exists to answer ("is it armed, and
            since when"). */}
        {status?.armed && status?.armed_at && (
          <p className="font-display tabular-nums text-[9px] text-mist mt-2">
            Armed since {fmtDateTimeIst(status.armed_at)}
          </p>
        )}
      </div>

      {/* ── Dhan account ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="dash-section-title">Dhan Account</p>
          <span className={`font-display tabular-nums text-[10px] px-2 py-0.5 rounded-full border ${
            dhanAccount?.connected ? "bg-signal-buy/10 border-signal-buy/30 text-signal-buy" : "bg-signal-sell/10 border-signal-sell/30 text-signal-sell"
          }`}>
            {dhanAccount?.connected ? "🟢 Connected" : loggedIn ? "🔴 Disconnected" : "— Log in to check"}
          </span>
        </div>
        {dhanAccount?.connected ? (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2 text-[11px] font-display tabular-nums text-mist">
              <div>Client ID <span className="text-paper ml-1">{dhanAccount.client_id_masked}</span></div>
              <div>Token <span className={`ml-1 ${(dhanTokenSecondsRemaining ?? 0) < 7200 ? "text-signal-sell" : "text-signal-buy"}`}>
                {dhanTokenSecondsRemaining != null
                  ? (dhanTokenSecondsRemaining <= 0 ? "expired" : `${fmtHms(dhanTokenSecondsRemaining)} left`)
                  : "—"}
              </span></div>
            </div>
            <p className="font-display tabular-nums text-[9px] text-mist -mt-2">
              This is the SAME Dhan account/token Real Automatic Trade uses — position-stocks-service only
              ever reads it, it never saves or refreshes a token itself. Manage the connection from the Real
              Automatic Trade tab.
            </p>

            {dhanAccount.funds_error ? (
              <p className="font-display tabular-nums text-[11px] text-signal-sell bg-signal-sell/5 rounded-xl px-3 py-2 border border-signal-sell/20">
                ⚠ Live funds check failed: {dhanAccount.funds_error}
              </p>
            ) : dhanAccount.funds ? (
              <div className="grid grid-cols-3 gap-2">
                {[
                  { label: "Available", keys: ["availabelBalance", "availableBalance", "availableCash"] },
                  { label: "Utilized", keys: ["utilizedAmount", "utilisedAmount"] },
                  { label: "Withdrawable", keys: ["withdrawableBalance"] },
                  { label: "SOD Limit", keys: ["sodLimit"] },
                  { label: "Collateral", keys: ["collateralAmount"] },
                  { label: "Blocked", keys: ["blockedPayoutAmount"] },
                ].map(f => {
                  const v = pickNum(dhanAccount.funds, ...f.keys);
                  return (
                    <div key={f.label} className="bg-ink border border-slate rounded-xl p-2">
                      <p className="font-display tabular-nums text-[9px] uppercase tracking-widest text-mist">{f.label}</p>
                      <p className="font-display tabular-nums text-sm font-bold text-paper mt-0.5">{fmtInr(v, 0)}</p>
                    </div>
                  );
                })}
              </div>
            ) : null}
            <button onClick={() => void loadDhanAccount()} className="font-display tabular-nums text-[10px] text-signal-prepare hover:text-signal-prepare">
              ↻ Refresh
            </button>
          </div>
        ) : (
          <p className="font-display tabular-nums text-[11px] text-mist">
            {loggedIn
              ? "No Dhan account connected yet — connect it from the Real Automatic Trade tab (both services share the same account)."
              : "Log in above to check the Dhan connection."}
          </p>
        )}
      </div>

      {/* Round 270: Dhan money - the three-way board, free to trade, charges, open P&L before / after charges (reads /capital/board itself) */}
      {(() => {
        const unreal = openPositions.reduce((a, p) => a + (p.unrealized_pnl ?? 0), 0);
        const est = openPositions.reduce((a, p) => a + (psOpenCharges(p) ?? 0), 0);
        const net = afterCharges(unreal, est);
        const has = openPositions.length > 0;
        return (
          <DhanMoneyCard fetcher={positionStocksApi.capitalBoard} thisService="position_stocks" enabled={loggedIn}
            dhanAvailable={pickNum(dhanAccount?.funds, "availabelBalance", "availableBalance", "availableCash")}
            tiles={[
              { label: "Charges paid today", value: cumCharges ? `-${fmtInr(cumCharges.today.all_charges, 2)}` : "-", sub: cumCharges ? `${cumCharges.today.trades} trades` : "loading", tone: "bad" },
              { label: "Charges since start", value: cumCharges ? `-${fmtInr(cumCharges.all_charges_total, 0)}` : "-", tone: "bad" },
              { label: "Open P&L before charges", value: has ? `${unreal >= 0 ? "+" : ""}${fmtInr(unreal, 2)}` : "-", tone: unreal >= 0 ? "good" : "bad" },
              { label: "Open P&L after charges", value: has && net != null ? `${net >= 0 ? "+" : ""}${fmtInr(net, 2)}` : "-", sub: has ? `if all closed now (est. charges ${fmtInr(est, 0)})` : "no open positions", tone: net != null && net < 0 ? "bad" : "good" },
            ]} />
        );
      })()}

      {/* ── 6-cell status grid ── */}
      <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
        {[
          { label: "Armed", value: status?.armed ? "ARMED" : "DISARMED", color: status?.armed ? "text-signal-buy" : "text-signal-avoid" },
          { label: "Market", value: status?.market_open ? "OPEN" : "CLOSED", color: status?.market_open ? "text-signal-buy" : "text-mist" },
          { label: "WS Feed", value: status?.ws?.connected ? "LIVE" : "DOWN", color: status?.ws?.connected ? "text-signal-buy" : "text-signal-sell" },
          { label: "Kill Switch", value: killSwitchTripped ? "TRIPPED" : "clear", color: killSwitchTripped ? "text-signal-sell" : "text-signal-buy" },
          { label: "Module", value: status?.service_enabled ? "ENABLED" : "PAUSED", color: status?.service_enabled ? "text-signal-buy" : "text-signal-avoid" },
          { label: "Auto-Pilot", value: status?.auto_pilot_enabled ? "ON" : "OFF", color: status?.auto_pilot_enabled ? "text-signal-buy" : "text-signal-hold" },
          { label: "Stagnation Exit", value: status?.stagnation_exit_enabled ? "ON" : "OFF", color: status?.stagnation_exit_enabled ? "text-signal-buy" : "text-signal-hold" },
          { label: "Breakeven Stop", value: status?.breakeven_stop_enabled ? "ON" : "OFF", color: status?.breakeven_stop_enabled ? "text-signal-buy" : "text-signal-hold" },
        ].map(({ label, value, color }) => (
          <div key={label} className="bg-graphite border border-slate rounded-2xl p-3">
            <p className="text-[9px] text-mist uppercase tracking-widest mb-1">{label}</p>
            <p className={`font-display tabular-nums font-bold text-sm ${color}`}>{value}</p>
          </div>
        ))}
      </div>

      {/* ── Extended health details ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">System Health</p>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-x-6 gap-y-3">
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">Circuit Breaker</p>
            <CBadge cb={status?.circuit_breaker} />
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">WS Symbols</p>
            <p className="font-display tabular-nums text-xs text-paper">{status?.ws?.subscribed_symbols ?? "—"}</p>
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">Last WS Tick</p>
            <p className="font-display tabular-nums text-xs text-paper">{fmtDateTimeIst(status?.ws?.last_tick_at)}</p>
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">WS Reconnects</p>
            <p className={`font-display tabular-nums text-xs ${(status?.ws?.reconnect_attempts ?? 0) > 0 ? "text-signal-hold" : "text-paper"}`}>
              {status?.ws?.reconnect_attempts ?? 0}
            </p>
          </div>
          <div>
            {/* AUDIT FIX (this session): this used to show a bare count
                with no denominator — orders/entry.py has always enforced
                this service's own daily order cap (config.DAILY_ORDER_
                BUDGET, separate from the shared Dhan-account-wide budget
                tile below), but the dashboard gave no visibility into how
                close it was, so hitting it just looked like entries
                silently stopping. Now shown the same used/budget way as
                Shared Dhan Order Budget, with the same near-exhausted
                color cue. */}
            <p className="text-[9px] text-mist uppercase tracking-widest">Orders Today</p>
            <p className={`font-display tabular-nums text-xs ${
              status?.orders_placed_today_budget && status.orders_placed_today !== undefined &&
              (status.orders_placed_today_budget - status.orders_placed_today) < status.orders_placed_today_budget * 0.1
                ? "text-signal-hold" : "text-paper"
            }`}>
              {status?.orders_placed_today !== undefined && status?.orders_placed_today_budget
                ? `${status.orders_placed_today}/${status.orders_placed_today_budget}`
                : (status?.orders_placed_today ?? "—")}
            </p>
          </div>
          <div>
            {/* AUDIT ADD (this session): backend has returned this since the
                shared_order_budget feature was built (capital/shared_order_budget.py)
                but no frontend field or display ever consumed it — the Dhan
                account-wide order cap this service shares with Real Automatic
                Trade was invisible on this dashboard. */}
            <p className="text-[9px] text-mist uppercase tracking-widest">Shared Dhan Order Budget</p>
            <p className={`font-display tabular-nums text-xs ${
              status?.shared_order_budget && status.shared_order_budget.remaining < status.shared_order_budget.budget * 0.1
                ? "text-signal-hold" : "text-paper"
            }`}>
              {status?.shared_order_budget ? `${status.shared_order_budget.used_today}/${status.shared_order_budget.budget}` : "—"}
            </p>
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">First Live Order</p>
            <p className={`font-display tabular-nums text-xs ${status?.first_live_order_done ? "text-signal-buy" : "text-mist"}`}>
              {status?.first_live_order_done ? "Done ✓" : "Not yet"}
            </p>
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">Risk / Trade</p>
            <p className="font-display tabular-nums text-xs text-paper">
              {status?.risk_per_trade_pct ?? "—"}%
              {status?.risk_confirmed ? <span className="text-signal-buy ml-1">✓</span> : <span className="text-signal-hold ml-1">⚠ placeholder</span>}
            </p>
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">Last Cycle</p>
            <p className="font-display tabular-nums text-xs text-paper">
              {fmtDateTimeIst(status?.last_cycle_run_at)}
              {status?.last_cycle_run_trigger ? <span className="text-mist ml-1">({status.last_cycle_run_trigger})</span> : ""}
            </p>
          </div>
        </div>
      </div>

      {/* ── Risk configuration (read-only — env/config-driven for this
          service, unlike Real Automatic Trade's editable DB row; see
          config.py) ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">Risk Configuration</p>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="bg-ink border border-slate rounded-xl p-2">
            <p className="font-display tabular-nums text-[9px] uppercase tracking-widest text-mist">Risk / Trade (%)</p>
            <p className="font-display tabular-nums text-sm font-bold text-paper mt-0.5">{status?.risk_per_trade_pct ?? "—"}</p>
          </div>
          <div className="bg-ink border border-slate rounded-xl p-2">
            <p className="font-display tabular-nums text-[9px] uppercase tracking-widest text-mist">Max Daily Loss (%)</p>
            <p className="font-display tabular-nums text-sm font-bold text-paper mt-0.5">{status?.max_daily_loss_pct_of_pool ?? "—"}</p>
          </div>
          <div className="bg-ink border border-slate rounded-xl p-2">
            <p className="font-display tabular-nums text-[9px] uppercase tracking-widest text-mist">Max Positions</p>
            <p className="font-display tabular-nums text-sm font-bold text-paper mt-0.5">{status?.max_concurrent_scalp_positions ?? "—"}</p>
          </div>
          <div className="bg-ink border border-slate rounded-xl p-2">
            <p className="font-display tabular-nums text-[9px] uppercase tracking-widest text-mist">Pool Split of Dhan Balance (%)</p>
            <p className="font-display tabular-nums text-sm font-bold text-paper mt-0.5">{status?.scalp_pool_capital_share_pct ?? "—"}</p>
          </div>
        </div>
        <p className="font-display tabular-nums text-[9px] text-mist mt-2">
          Set via env on this service, not editable from this dashboard — change RISK_PER_TRADE_PCT /
          MAX_DAILY_LOSS_PCT_OF_POOL / MAX_CONCURRENT_SCALP_POSITIONS / SCALP_POOL_CAPITAL_SHARE_PCT and redeploy.
        </p>
      </div>

      {/* AUDIT MOVE (this session): Status banners, "Last manual cycle",
          and all Action buttons (Enable/Pause Module, Arm/Disarm,
          Auto-Pilot toggle, Run Cycle Now, Sync Capital, Check Exits,
          Reset Daily Ledger, Kill Switch) moved to the Pipeline subtab —
          they control the exact pipeline that tab now visualizes, so
          they belong right next to it rather than on Overview. See
          subTab === "pipeline" below. */}

      {/* ── Capital ledger ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">Scalp Capital Pool (50% split, software-enforced)</p>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">Allocated</p>
            <p className="font-display tabular-nums font-bold text-sm text-paper">{fmtInr(ledger?.total_allocated_capital)}</p>
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">Available</p>
            <p className="font-display tabular-nums font-bold text-sm text-paper">{fmtInr(ledger?.available_capital)}</p>
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">P&L Today</p>
            <p className={`font-display tabular-nums font-bold text-sm ${(ledger?.realized_pnl_today ?? 0) >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
              {fmtInr(ledger?.realized_pnl_today)}
            </p>
            {cumCharges?.pnl && ledger && (() => {
              const netToday = ledger.realized_pnl_today - cumCharges.pnl.charges_today;
              return (
                <p className={`font-display tabular-nums text-[9px] ${netToday >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                  after charges {netToday >= 0 ? "+" : ""}{fmtInr(netToday, 0)}
                </p>
              );
            })()}
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">P&L Total</p>
            <p className={`font-display tabular-nums font-bold text-sm ${(ledger?.realized_pnl_total ?? 0) >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
              {fmtInr(ledger?.realized_pnl_total)}
            </p>
            {cumCharges?.pnl && (
              <p className={`font-display tabular-nums text-[9px] ${cumCharges.pnl.net_realized_total >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                after charges {cumCharges.pnl.net_realized_total >= 0 ? "+" : ""}{fmtInr(cumCharges.pnl.net_realized_total, 0)}
                {cumCharges.since ? ` (since ${fmtChargeDay(cumCharges.since)})` : ""}
              </p>
            )}
          </div>
        </div>
        <CapitalBar allocated={ledger?.total_allocated_capital ?? 0} available={ledger?.available_capital ?? 0} />
        <div className="flex items-center justify-between mt-3">
          <p className="font-display tabular-nums text-[9px] text-mist">Last synced from Dhan: {fmtDateTimeIst(ledger?.last_synced_from_broker_at)}</p>
          {ledger?.daily_loss_kill_switch_tripped && (
            <span className="font-display tabular-nums text-[9px] text-signal-sell font-bold uppercase">Kill switch active</span>
          )}
        </div>
      </div>
      </>
      );
}
