import type { Dispatch, SetStateAction } from "react";
import { realTradeApi, type DhanEdisSummary, type DhanStatus, type GateStatus, type Position, type CumulativeBrokerage } from "../../../realTradeApi";
import ManualTradeTicket from "../../trading/ManualTradeTicket";
import CloseAllDialog from "../../CloseAllDialog";
import { fmtDate, fmtHms, fmtInr, fmtTime, pickNum, pnlColor } from "../format";
import { OrderFlowDiagram } from "../panels";
import { Dot, SectionHdr, StatCard } from "../parts";
import type { Mode } from "../types";

// E66 - E31 step 10: the "overview" tab, moved out of RealAutoTrade.tsx (JSX unchanged). PortfolioSummary and GateStep moved with it
// (only this tab used them). State and handlers stay in RealAutoTrade and arrive as props.

// ── Portfolio summary: invested / current stock value / P&L ─────────────────
// Deliberately built from `positions` (this service's own OPEN/
// PARTIALLY_CLOSED/PENDING_EXIT tracked positions, each carrying a known
// avg_entry_price and live current_price from /positions) rather than
// `livePositions`/`dhanAccount.funds` (Dhan's raw broker figures, already
// shown separately in BalanceAllocation above).
//
// P&L calculation:
//   unrealized = currentValue - invested         (open positions only)
//   realized   = all closed trades this engine has tracked (realized_pnl_total)
//   totalPnl   = realized + unrealized
//   unrealizedPct = unrealized / invested * 100  (only open-position capital)
//
// NOTE: realized_pnl_total tracks only trades placed THROUGH this engine.
// Money deposited from Dhan (outside this app) is NOT reflected as "profit"
// — it only increases your fund balance and current_equity, not realized P&L.
// So if equity > starting_capital, that gap is deposits, not gains.
function PortfolioSummary({
  positions,
  realizedPnlTotal,
  startingCapital,
  currentEquity,
  chargesTotal,
}: {
  positions: Position[];
  realizedPnlTotal: number | null;
  startingCapital: number | null;
  currentEquity: number | null;
  /** group 262: all Dhan charges since the first filled order (backend ledger); null until loaded / in DEMO. */
  chargesTotal?: number | null;
}) {
  if (positions.length === 0 && realizedPnlTotal == null) return null;

  const invested = positions.reduce((sum, p) => sum + p.avg_entry_price * p.qty_open, 0);
  // Falls back to avg_entry_price (i.e. 0 unrealized) for a position with no
  // live tick yet this session — matches the same fallback /positions itself
  // already uses for live_unrealized_pnl.
  const currentValue = positions.reduce(
    (sum, p) => sum + (p.current_price ?? p.avg_entry_price) * p.qty_open, 0
  );
  const unrealized = currentValue - invested;
  const realized = realizedPnlTotal ?? 0;
  const totalPnl = realized + unrealized;
  // P&L % is based on invested capital (open positions only), not equity
  const unrealizedPct = invested > 0 ? (unrealized / invested) * 100 : 0;
  // AUDIT FIX (session67 — live case: showed -25.5% off a -₹1,429 all-time
  // realized loss): this used to divide totalPnl (realized-all-time +
  // unrealized-today) by `invested`, but `invested` is only the capital
  // sitting in TODAY's open positions — realized P&L came from trades whose
  // capital was already closed out and returned to cash long ago, so it has
  // no relationship to today's invested amount. Concretely: -₹1,429 / ₹5,613
  // (today's 3 open positions) read as a -25.5% loss, when the actual
  // all-time return on the capital this engine has ever traded with is
  // -₹1,429 / ₹12,305 (startingCapital) ≈ -11.6%. unrealizedPct above is
  // correctly left on `invested` — unrealized P&L genuinely only concerns
  // currently-open capital — only totalPnlPct (which mixes in all-time
  // realized P&L) needed to move to startingCapital as its denominator.
  const totalPnlPct = startingCapital && startingCapital > 0 ? (totalPnl / startingCapital) * 100 : 0;
  // group 262: the same total after every brokerage / STT / GST / DP charge actually paid since the start.
  const netOfCharges = chargesTotal != null ? totalPnl - chargesTotal : null;
  const netOfChargesPct = netOfCharges != null && startingCapital && startingCapital > 0 ? (netOfCharges / startingCapital) * 100 : 0;

  // Deposits made outside this engine = equity growth not from trading
  const depositsOutside =
    startingCapital != null && currentEquity != null
      ? Math.max(0, currentEquity - startingCapital - realized - unrealized)
      : null;

  return (
    <div className="bg-graphite border border-slate rounded-2xl p-4 mb-4">
      <SectionHdr>Portfolio summary (stocks only — excludes fund balance)</SectionHdr>
      <div className="grid grid-cols-2 gap-2">
        <StatCard label="Invested (open positions)" value={fmtInr(invested, 0)} />
        <StatCard label="Current stock value" value={fmtInr(currentValue, 0)} />
        <StatCard
          label="Unrealized P&L (open positions)"
          value={`${unrealized >= 0 ? "+" : ""}${fmtInr(unrealized, 0)} (${unrealizedPct >= 0 ? "+" : ""}${unrealizedPct.toFixed(1)}%)`}
          color={pnlColor(unrealized)}
          sub={`${positions.length} open position${positions.length === 1 ? "" : "s"}`}
        />
        <StatCard
          label="Total P&L (realized + unrealized)"
          value={`${totalPnl >= 0 ? "+" : ""}${fmtInr(totalPnl, 0)} (${totalPnlPct >= 0 ? "+" : ""}${totalPnlPct.toFixed(1)}%)`}
          color={pnlColor(totalPnl)}
          sub={`Realized all-time (this engine): ${realized >= 0 ? "+" : ""}${fmtInr(realized, 0)}`}
        />
        {netOfCharges != null && (
          <StatCard
            label="Total P&L after charges"
            value={`${netOfCharges >= 0 ? "+" : ""}${fmtInr(netOfCharges, 0)} (${netOfChargesPct >= 0 ? "+" : ""}${netOfChargesPct.toFixed(1)}%)`}
            color={pnlColor(netOfCharges)}
            sub={`− charges paid since start: ${fmtInr(chargesTotal ?? 0, 0)}`}
          />
        )}
      </div>
      {/* NEW (2026-09-07): this P&L is pure (exit_price - entry_price) * qty — it does
          NOT subtract brokerage/STT/GST/DP. Without this line the number above reads
          as the full picture when it isn't; the Charges tab has the real after-costs
          number for today. */}
      <p className="font-display tabular-nums text-[10px] text-mist mt-2 pt-2 border-t border-slate">
        ℹ️ {chargesTotal != null
          ? "Total P&L above is price movement only; “after charges” subtracts every brokerage, STT, GST and DP charge paid since the start (estimated from Dhan's rate card)."
          : "P&L above is price movement only — brokerage, STT, GST and other charges are not subtracted. See the Charges tab for the net after charges."}
      </p>
      {depositsOutside != null && depositsOutside > 500 && (
        <p className="font-display tabular-nums text-[10px] text-mist mt-2 pt-2 border-t border-slate">
          ℹ️ Equity is ₹{(depositsOutside / 1000).toFixed(1)}k higher than starting capital + P&L — this reflects funds you deposited into Dhan directly, not trading gains.
        </p>
      )}
    </div>
  );
}

// ── Gate sequence steps ──────────────────────────────────────────────────────
function GateStep({ n, label, done, active }: { n: number; label: string; done: boolean; active: boolean }) {
  return (
    <div className={`rt-gate transition-colors ${active && !done ? "animate-pulse" : ""}`}
      data-state={done ? "done" : active ? "active" : "idle"}>
      <div className="rt-gate-n">{done ? "✓" : n}</div>
      <span className="font-display tabular-nums">{label}</span>
    </div>
  );
}

export type RiskForm = {
  risk_per_trade_pct: string; max_daily_loss_pct: string; max_concurrent_positions: string;
  max_portfolio_risk_pct: string; stale_data_seconds: string; max_tick_volatility_mult: string;
  allow_pyramiding: boolean;
  // 2026-09-18 fix (follow-on item #5): cost-gate knobs, now editable
  // here alongside every other risk knob. Empty string = "use default".
  min_trade_value: string; min_edge_to_cost_ratio: string;
};
type Setter<T> = Dispatch<SetStateAction<T>>;
type Act = () => Promise<void>;
export type DhanAccountState = (DhanStatus & { funds: any; funds_error: string | null }) | null;

export interface OverviewTabProps {
  mode: Mode;
  status: GateStatus | null;
  loggedIn: boolean;
  loading: boolean;
  gate1: boolean; gate2: boolean; gate3: boolean; armed: boolean;
  nextGateNeeded: number | null;
  positions: Position[];
  // login
  username: string; setUsername: Setter<string>; password: string; setPassword: Setter<string>; doLogin: Act;
  // arming
  doConfirmRisk: Act; doArm: Act; doDisarm: Act; doEmergencyPause: Act;
  showCloseAll: boolean; setShowCloseAll: Setter<boolean>; loadStatus: (m: Mode) => Promise<void>;
  // Dhan
  dhanAccount: DhanAccountState; secondsRemaining: number | null;
  showDhanForm: boolean; setShowDhanForm: Setter<boolean>;
  dhanClientId: string; setDhanClientId: Setter<string>; dhanToken: string; setDhanToken: Setter<string>;
  doConnectDhan: Act; doRegenerateToken: Act; regenLoading: boolean;
  edisSummary: DhanEdisSummary | null; edisBusy: boolean; doEdisRequestTpin: Act; doEdisAuthorize: Act;
  networkCheck: { outbound_ip: string | null; note: string } | null; networkCheckBusy: boolean; doNetworkCheck: Act;
  // risk config
  riskForm: RiskForm | null; setRiskForm: Setter<RiskForm | null>;
  riskDefaults: { min_trade_value: number; min_edge_to_cost_ratio: number };
  riskSaving: boolean; riskMsg: { ok: boolean; text: string } | null; doSaveRiskConfig: Act;
  // account snapshot
  cumBrokerage: CumulativeBrokerage | null;
  loadPositionsAndOrders: (m: Mode) => Promise<void>;
}

export function OverviewTab({
  mode, status, loggedIn, loading, gate1, gate2, gate3, armed, nextGateNeeded, positions,
  username, setUsername, password, setPassword, doLogin,
  doConfirmRisk, doArm, doDisarm, doEmergencyPause, showCloseAll, setShowCloseAll, loadStatus,
  dhanAccount, secondsRemaining, showDhanForm, setShowDhanForm, dhanClientId, setDhanClientId, dhanToken, setDhanToken,
  doConnectDhan, doRegenerateToken, regenLoading, edisSummary, edisBusy, doEdisRequestTpin, doEdisAuthorize,
  networkCheck, networkCheckBusy, doNetworkCheck,
  riskForm, setRiskForm, riskDefaults, riskSaving, riskMsg, doSaveRiskConfig,
  cumBrokerage, loadPositionsAndOrders,
}: OverviewTabProps) {
  return (
            <div className="space-y-4">
              {/* Gate sequence */}
              <div className="bg-graphite border border-slate rounded-2xl p-4">
                <SectionHdr>Arming sequence — {mode} mode</SectionHdr>
                <div className="grid grid-cols-2 gap-2 mb-4">
                  <GateStep n={1} label="Admin authenticated" done={gate1} active={nextGateNeeded === 1} />
                  <GateStep n={2} label={mode === "DEMO" ? "Dhan (not required)" : "Dhan connected"} done={gate2} active={nextGateNeeded === 2} />
                  <GateStep n={3} label="Risk config confirmed" done={gate3} active={nextGateNeeded === 3} />
                  <GateStep n={4} label="Armed" done={armed} active={nextGateNeeded === 4} />
                </div>

                {/* Inline login form when armed but session expired —
                    don't block the full page, just gate the mutating actions */}
                {mode === "REAL" && !loggedIn && armed && (
                  <div className="rt-login-inline">
                    <p className="font-display tabular-nums">Session expired — log in to disarm or change config. Auto-pilot keeps running.</p>
                    <div className="rt-inline-row">
                      <input type="text" autoComplete="username" autoCapitalize="none" autoCorrect="off" spellCheck={false}
                        placeholder="Username" value={username} onChange={e => setUsername(e.target.value)} />
                      <input type="password" autoComplete="current-password"
                        placeholder="Password" value={password} onChange={e => setPassword(e.target.value)}
                        onKeyDown={e => e.key === "Enter" && void doLogin()} />
                      <button onClick={() => void doLogin()} disabled={loading || !password} className="rt-btn rt-btn-primary" style={{ width: "auto" }}>
                        {loading ? "…" : "Login"}
                      </button>
                    </div>
                  </div>
                )}

                {/* Login form already shown above when not logged in; here only if
                    logged in but gate not met */}
                {status && !gate3 && mode === "REAL" && gate1 && gate2 && (
                  <button onClick={() => void doConfirmRisk()}
                    className="w-full py-2 rounded-xl bg-signal-hold/10 border border-signal-hold/30 font-display tabular-nums text-xs text-signal-hold">
                    Confirm risk configuration
                  </button>
                )}
                {status && !gate3 && mode === "DEMO" && (
                  <button onClick={() => void doConfirmRisk()}
                    className="w-full py-2 rounded-xl bg-signal-hold/10 border border-signal-hold/30 font-display tabular-nums text-xs text-signal-hold">
                    Confirm risk configuration
                  </button>
                )}

                <div className="flex gap-2 mt-2">
                  {armed ? (
                    <button onClick={() => void doDisarm()} className="flex-1 py-2.5 rounded-xl bg-signal-sell/10 border border-signal-sell/30 font-display tabular-nums text-xs text-signal-sell">
                      🛑 DISARM
                    </button>
                  ) : (
                    <button onClick={() => void doArm()} disabled={loading || !gate1 || !gate2 || !gate3}
                      className="flex-1 py-2.5 rounded-xl bg-signal-buy/10 border border-signal-buy/30 font-display tabular-nums text-xs text-signal-buy disabled:opacity-30">
                      {loading ? "Arming…" : "⚡ ARM"}
                    </button>
                  )}
                  <button onClick={() => void doEmergencyPause()} className="px-4 py-2.5 rounded-xl bg-signal-sell/30 border border-signal-sell/40 font-display tabular-nums text-xs text-signal-sell">
                    🚨 PAUSE ALL
                  </button>
                  <button onClick={() => setShowCloseAll(true)} className="px-4 py-2.5 rounded-xl bg-signal-sell/40 border border-signal-sell font-display tabular-nums text-xs text-signal-sell">
                    🚨 CLOSE ALL
                  </button>
                  {showCloseAll && (
                    <CloseAllDialog open={showCloseAll}
                      title={`Close all and disarm — ${mode}`}
                      detail={`This disarms ${mode}, cancels resting BUY orders and sends a MARKET SELL for every open position. It cannot be undone.`}
                      onCancel={() => { setShowCloseAll(false); void loadStatus(mode); }}
                      onConfirm={(phrase) => realTradeApi.closeAll(mode, phrase)}
                    />
                  )}
                </div>
                {status?.disarmed_reason && (
                  /outbound IP|invalid ip|not whitelisted/i.test(status.disarmed_reason) ? (
                    <div className="mt-2 rounded-xl border border-signal-hold/30 bg-signal-hold/5 px-3 py-2">
                      <p className="font-display tabular-nums text-[10px] text-signal-hold">
                        🚨 Auto-paused: Dhan rejected an order because this service's outbound IP isn't whitelisted.
                      </p>
                      <p className="font-display tabular-nums text-[9px] text-mist mt-1">
                        Reads (funds/positions) still work — only order placement is IP-gated by Dhan, which is
                        why the account shows connected while this happens.
                      </p>
                      <div className="flex items-center gap-2 mt-2">
                        <button onClick={() => void doNetworkCheck()} disabled={networkCheckBusy}
                          className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-signal-hold/10 border border-signal-hold/30 text-signal-hold disabled:opacity-40">
                          {networkCheckBusy ? "Checking…" : "Check outbound IP"}
                        </button>
                        {networkCheck?.outbound_ip && (
                          <code className="font-display tabular-nums text-[10px] text-paper bg-ink px-2 py-1 rounded">{networkCheck.outbound_ip}</code>
                        )}
                      </div>
                      {networkCheck && (
                        <p className="font-display tabular-nums text-[9px] text-mist mt-1">{networkCheck.note}</p>
                      )}
                      <p className="font-display tabular-nums text-[9px] text-mist mt-1">
                        Add that IP under Dhan Web → My Profile → API Access → IP Whitelisting, then re-arm.
                        A non-static host IP can change on redeploy — re-check if this recurs.
                      </p>
                    </div>
                  ) : (
                    <p className="font-display tabular-nums text-[10px] text-signal-hold/60 mt-2">Last disarm: {status.disarmed_reason}</p>
                  )
                )}
              </div>

              {/* REAL: Dhan account card */}
              {mode === "REAL" && (
                <div className="bg-graphite border border-slate rounded-2xl p-4">
                  <div className="flex items-center justify-between mb-3">
                    <SectionHdr>Dhan account</SectionHdr>
                    <span className={`font-display tabular-nums text-[10px] px-2 py-0.5 rounded-full border ${
                      dhanAccount?.connected ? "bg-signal-buy/10 border-signal-buy/30 text-signal-buy" : "bg-signal-sell/10 border-signal-sell/30 text-signal-sell"
                    }`}>
                      {dhanAccount?.connected ? "🟢 Connected" : "🔴 Disconnected"}
                    </span>
                  </div>

                  {dhanAccount?.connected ? (
                    <div className="space-y-3">
                      <div className="grid grid-cols-2 gap-2 text-[11px] font-display tabular-nums text-mist">
                        <div>Client ID <span className="text-paper ml-1">{dhanAccount.client_id_masked}</span></div>
                        <div>Token <span className={`ml-1 ${(secondsRemaining ?? 0) < 7200 ? "text-signal-sell" : "text-signal-buy"}`}>
                          {secondsRemaining != null ? (secondsRemaining <= 0 ? "expired" : fmtHms(secondsRemaining) + " left") : "—"}
                        </span></div>
                      </div>
                      <p className="font-display tabular-nums text-[9px] text-mist -mt-2">
                        {dhanAccount.token_issued_at && `Issued ${fmtDate(dhanAccount.token_issued_at)} ${fmtTime(dhanAccount.token_issued_at)} · `}
                        Countdown reflects Dhan's own expiry when a regenerated token reports one, otherwise assumes {dhanAccount.token_hard_cap_hours ?? 24}h as a safety ceiling.
                      </p>

                      {/* Daily CDSL eDIS / T-PIN check — a valid Dhan token alone doesn't
                          mean sells will go through; CDSL separately requires this once-a-day
                          authorization per holding. Distinct from the token badge above on
                          purpose (green Dhan connection + red eDIS is a real, common state). */}
                      <div className={`rounded-xl px-3 py-2 border ${
                        edisSummary?.no_holdings ? "bg-ink border-slate"
                        : edisSummary?.verified_today === true ? "bg-signal-buy/5 border-signal-buy/20"
                        : edisSummary?.verified_today === false ? "bg-signal-sell/5 border-signal-sell/20"
                        : "bg-ink border-slate"
                      }`}>
                        <div className="flex items-center justify-between">
                          <span className="font-display tabular-nums text-[10px] uppercase tracking-widest text-mist">Daily eDIS (CDSL)</span>
                          <span className={`font-display tabular-nums text-[10px] px-2 py-0.5 rounded-full border ${
                            edisSummary?.no_holdings ? "bg-slate/20 border-slate text-mist"
                            : edisSummary?.verified_today === true ? "bg-signal-buy/10 border-signal-buy/30 text-signal-buy"
                            : edisSummary?.verified_today === false ? "bg-signal-sell/10 border-signal-sell/30 text-signal-sell"
                            : "bg-slate/20 border-slate text-mist"
                          }`}>
                            {/* session73 fix: "no holdings, nothing to authorize" is a
                                distinct, neutral state — it must not read as "✅ Verified
                                today", which implies you actually completed CDSL's T-PIN/
                                OTP flow. Check no_holdings before verified_today. */}
                            {edisSummary?.no_holdings ? "— Nothing to authorize"
                              : edisSummary?.verified_today === true ? "🟢 Verified today"
                              : edisSummary?.verified_today === false ? "🔴 Not verified"
                              : edisSummary ? "⚠ Unknown" : "— Checking…"}
                          </span>
                        </div>
                        <p className="font-display tabular-nums text-[9px] text-mist mt-1">
                          {edisSummary?.detail ?? "Loading eDIS status…"}
                        </p>
                        {edisSummary?.verified_today !== true && (
                          <div className="flex gap-2 mt-2">
                            <button onClick={() => void doEdisRequestTpin()} disabled={edisBusy}
                              className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-signal-hold/10 border border-signal-hold/30 text-signal-hold disabled:opacity-40">
                              {edisBusy ? "Working…" : "1. Request T-PIN"}
                            </button>
                            <button onClick={() => void doEdisAuthorize()} disabled={edisBusy}
                              className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-signal-prepare/10 border border-signal-prepare/30 text-signal-prepare disabled:opacity-40">
                              {edisBusy ? "Working…" : "2. Authorize on CDSL"}
                            </button>
                            <button onClick={() => void loadStatus(mode)} disabled={edisBusy}
                              className="font-display tabular-nums text-[10px] text-mist hover:text-paper disabled:opacity-40">
                              ↻ Re-check
                            </button>
                          </div>
                        )}
                      </div>

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

                      <div className="flex gap-2">
                        <button onClick={() => void loadStatus(mode)} className="font-display tabular-nums text-[10px] text-signal-prepare hover:text-signal-prepare">
                          ↻ Refresh
                        </button>
                        <button onClick={() => setShowDhanForm(!showDhanForm)} className="font-display tabular-nums text-[10px] text-mist hover:text-paper">
                          Rotate token
                        </button>
                        <button onClick={() => void doRegenerateToken()} disabled={regenLoading} className="font-display tabular-nums text-[10px] text-signal-prepare hover:text-signal-prepare disabled:opacity-40">
                          {regenLoading ? "Regenerating…" : "⟳ Regenerate token"}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <p className="font-display tabular-nums text-[11px] text-mist">Connect Dhan below to enable REAL trading and see live balance.</p>
                  )}

                  {/* Dhan connect form */}
                  {(!dhanAccount?.connected || showDhanForm) && (
                    <div className="mt-3 space-y-2 border-t border-slate pt-3">
                      <p className="font-display tabular-nums text-[10px] text-mist">{dhanAccount?.connected ? "Paste a fresh access token to rotate" : "Connect your Dhan account"}</p>
                      <div className="rt-field">
                        <label className="rt-label" htmlFor="rt-dhan-client">Dhan Client ID</label>
                        <input id="rt-dhan-client" type="text" autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false}
                          placeholder="Dhan Client ID" value={dhanClientId} onChange={e => setDhanClientId(e.target.value)} />
                      </div>
                      <div className="rt-field">
                        <label className="rt-label" htmlFor="rt-dhan-token">Access token</label>
                        <input id="rt-dhan-token" type="password" autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false}
                          placeholder="Access Token (generate at web.dhan.co → DhanHQ APIs)" value={dhanToken} onChange={e => setDhanToken(e.target.value)} />
                      </div>
                      <div className="flex flex-col sm:flex-row gap-2">
                        <button onClick={() => void doConnectDhan()} disabled={loading || !dhanClientId || !dhanToken}
                          className="rt-btn rt-btn-primary flex-1">
                          {loading ? "Connecting…" : dhanAccount?.connected ? "Save new token" : "Connect Dhan"}
                        </button>
                        {dhanAccount?.connected && (
                          <button onClick={() => setShowDhanForm(false)} className="rt-btn rt-btn-ghost">
                            Cancel
                          </button>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Risk config — editable while disarmed, read-only snapshot while armed
                  (backend enforces the same rule and 409s otherwise, so the UI just
                  mirrors it instead of letting someone submit a doomed request). */}
              {status?.risk_config && (
                <div className="bg-graphite border border-slate rounded-2xl p-4">
                  <div className="flex items-center justify-between mb-1">
                    <SectionHdr>Risk configuration — {mode}</SectionHdr>
                    {armed && <span className="font-display tabular-nums text-[9px] text-signal-hold uppercase">locked while armed</span>}
                  </div>
                  {riskForm && (
                    <div className="grid grid-cols-2 gap-3">
                      {([
                        ["risk_per_trade_pct", "Risk / trade (%)"],
                        ["max_daily_loss_pct", "Max daily loss (%)"],
                        ["max_concurrent_positions", "Max positions"],
                        ["max_portfolio_risk_pct", "Portfolio risk cap (%)"],
                        ["stale_data_seconds", "Stale data cutoff (s)"],
                        ["max_tick_volatility_mult", "Max tick volatility ×"],
                        // 2026-09-18 fix (follow-on item #5): cost-gate
                        // knobs (Gate 5.6), now editable here — empty shows
                        // the effective config.py default as a placeholder.
                        ["min_trade_value", `Min trade value ₹ (default ${riskDefaults.min_trade_value})`],
                        ["min_edge_to_cost_ratio", `Min edge/cost ratio × (default ${riskDefaults.min_edge_to_cost_ratio})`],
                      ] as const).map(([key, label]) => (
                        <div key={key} className="bg-ink border border-slate rounded-xl p-2">
                          <p className="font-display tabular-nums text-[9px] uppercase tracking-widest text-mist">{label}</p>
                          <input
                            type="number" inputMode="decimal" disabled={armed}
                            value={riskForm[key]}
                            placeholder={
                              key === "min_trade_value" ? String(riskDefaults.min_trade_value)
                              : key === "min_edge_to_cost_ratio" ? String(riskDefaults.min_edge_to_cost_ratio)
                              : undefined
                            }
                            onChange={e => setRiskForm(f => f && { ...f, [key]: e.target.value })}
                            className="w-full bg-transparent font-display tabular-nums text-sm font-bold text-paper mt-0.5 focus:outline-none disabled:opacity-60 placeholder:text-mist/30"
                          />
                        </div>
                      ))}
                      <label className="col-span-2 flex items-center gap-2 bg-ink border border-slate rounded-xl p-2 cursor-pointer">
                        <input type="checkbox" disabled={armed} checked={riskForm.allow_pyramiding}
                          onChange={e => setRiskForm(f => f && { ...f, allow_pyramiding: e.target.checked })}
                          className="accent-signal-prepare" />
                        <span className="font-display tabular-nums text-[10px] text-mist">Allow pyramiding (add to an existing open position)</span>
                      </label>
                    </div>
                  )}
                  {riskMsg && (
                    <p className={`font-display tabular-nums text-[10px] mt-2 ${riskMsg.ok ? "text-signal-buy" : "text-signal-sell"}`}>{riskMsg.text}</p>
                  )}
                  {status.risk_config.updated_at && (
                    <p className="font-display tabular-nums text-[9px] text-mist mt-2">
                      Last saved {fmtDate(status.risk_config.updated_at)} {fmtTime(status.risk_config.updated_at)}
                      {status.risk_config.updated_by ? ` by ${status.risk_config.updated_by}` : ""}
                    </p>
                  )}
                  {!armed && (
                    <button onClick={() => void doSaveRiskConfig()} disabled={riskSaving}
                      className="mt-3 w-full py-2 rounded-xl bg-signal-prepare/10 border border-signal-prepare/30 font-display tabular-nums text-xs text-signal-prepare disabled:opacity-40">
                      {riskSaving ? "Saving…" : "Save changes"}
                    </button>
                  )}
                  {!gate3 && (
                    <button onClick={() => void doConfirmRisk()} className="mt-2 w-full py-2 rounded-xl bg-signal-hold/10 border border-signal-hold/30 font-display tabular-nums text-xs text-signal-hold">
                      Confirm this risk configuration
                    </button>
                  )}
                </div>
              )}

              {/* Portfolio summary — invested / current value / P&L, stocks only */}
              <PortfolioSummary
                positions={positions}
                realizedPnlTotal={status?.account?.realized_pnl_total ?? null}
                startingCapital={status?.account?.starting_capital ?? null}
                currentEquity={status?.account?.current_equity ?? null}
                chargesTotal={mode === "REAL" && cumBrokerage ? cumBrokerage.all_charges_total : null}
              />

              {/* Stockky account snapshot */}
              {status?.account && (() => {
                const realized = status.account.realized_pnl_total ?? 0;
                const unrealizedNow = positions.reduce(
                  (sum, p) => sum + ((p.current_price ?? p.avg_entry_price) - p.avg_entry_price) * p.qty_open, 0
                );
                const netPnl = realized + unrealizedNow;
                return (
                  <div className="space-y-2">
                    <div className="grid grid-cols-2 gap-2">
                      <StatCard label="Starting capital" value={fmtInr(status.account.starting_capital)} />
                      <StatCard label="Current equity" value={fmtInr(status.account.current_equity)} />
                      <StatCard label="Cash available" value={fmtInr(status.account.cash_available)} />
                      <StatCard
                        label="P&L today"
                        value={fmtInr(status.account.realized_pnl_today, 0)}
                        color={pnlColor(status.account.realized_pnl_today)}
                        sub={mode === "REAL" && cumBrokerage?.pnl
                          ? `after charges ${cumBrokerage.pnl.net_realized_today >= 0 ? "+" : ""}${fmtInr(cumBrokerage.pnl.net_realized_today, 0)}`
                          : undefined}
                      />
                    </div>
                    {/* Net P&L summary — big clear card */}
                    <div className={`rounded-2xl border p-4 flex items-center justify-between ${
                      netPnl >= 0
                        ? "bg-signal-buy/5 border-signal-buy/30"
                        : "bg-signal-sell/5 border-signal-sell/30"
                    }`}>
                      <div>
                        <p className="font-display tabular-nums text-[11px] uppercase tracking-widest text-mist mb-0.5">
                          Total P&L — realized + unrealized
                        </p>
                        <p className="font-display tabular-nums text-[10px] text-mist">
                          Realized: {fmtInr(realized, 0)} · Unrealized: {unrealizedNow >= 0 ? "+" : ""}{fmtInr(unrealizedNow, 0)}
                          {mode === "REAL" && cumBrokerage && (
                            <> · After charges: {netPnl - cumBrokerage.all_charges_total >= 0 ? "+" : ""}{fmtInr(netPnl - cumBrokerage.all_charges_total, 0)}</>
                          )}
                        </p>
                      </div>
                      <p className={`font-display tabular-nums text-2xl font-bold ${
                        netPnl >= 0 ? "text-signal-buy" : "text-signal-sell"
                      }`}>
                        {netPnl >= 0 ? "+" : ""}{fmtInr(netPnl, 0)}
                      </p>
                    </div>
                  </div>
                );
              })()}

              {/* Order flow diagram */}
              <OrderFlowDiagram mode={mode} />

              {/* Manual trade ticket */}
              {(mode === "DEMO" || gate1) && (
                <ManualTradeTicket
                  mode={mode}
                  armed={armed}
                  onOrderComplete={() => { void loadPositionsAndOrders(mode); void loadStatus(mode); }}
                />
              )}
            </div>
  );
}
