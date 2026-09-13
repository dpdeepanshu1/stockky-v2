// frontend/src/components/PositionStocksTab.tsx
//
// Session 9 — full dashboard rebuild: capital utilisation bar, circuit breaker
// status, WS health details, kill-switch badge, orders-placed counter, P&L
// history chart labels. All data comes from position-stocks-service only.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  positionStocksApi, getPositionStocksApiUrl, setPositionStocksApiUrl,
  getSessionToken, setSessionToken, setSessionExpiredHandler,
  type ScalpStatus, type ScalpPositionRow, type ScalpCandidateRow, type ScalpLedgerState,
  type ScalpTradeHistory, type DhanLiveOrders, type ScalpCycleResult,
} from "../positionStocksApi";

type Window = "1m" | "5m" | "15m" | "60m";

function fmtInr(n: number | null | undefined, decimals = 0): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1_00_00_000) return `${sign}₹${(abs / 1_00_00_000).toFixed(1)}Cr`;
  if (abs >= 1_00_000) return `${sign}₹${(abs / 1_00_000).toFixed(1)}L`;
  return `${sign}₹${abs.toLocaleString("en-IN", { maximumFractionDigits: decimals })}`;
}

function fmtDateTimeIst(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const date = d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: "Asia/Kolkata" });
    const time = d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" });
    return `${date}, ${time} IST`;
  } catch { return iso; }
}

function statusColor(status: string): string {
  if (status === "OPEN") return "text-signal-prepare";
  if (status === "TARGET_HIT") return "text-signal-buy";
  if (status === "STOP_HIT") return "text-signal-sell";
  if (status === "EOD_SQUAREOFF" || status === "MANUAL_EXIT") return "text-signal-hold";
  return "text-signal-avoid";
}

// Capital utilisation bar
function CapitalBar({ allocated, available }: { allocated: number; available: number }) {
  if (!allocated || allocated <= 0) return null;
  const used = allocated - available;
  const usedPct = Math.min(100, Math.max(0, (used / allocated) * 100));
  const color = usedPct > 80 ? "bg-signal-sell" : usedPct > 50 ? "bg-signal-hold" : "bg-signal-buy";
  return (
    <div className="mt-3">
      <div className="flex justify-between font-display tabular-nums text-[9px] text-mist mb-1">
        <span>Capital utilised: {usedPct.toFixed(1)}%</span>
        <span>{fmtInr(used)} / {fmtInr(allocated)}</span>
      </div>
      <div className="h-1.5 rounded-full bg-slate overflow-hidden">
        <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${usedPct}%` }} />
      </div>
    </div>
  );
}

// Circuit breaker badge
function CBadge({ cb }: { cb: ScalpStatus["circuit_breaker"] | undefined }) {
  if (!cb) return <span className="text-mist">—</span>;
  const stateColor = cb.state === "closed" ? "text-signal-buy" : cb.state === "open" ? "text-signal-sell" : "text-signal-hold";
  return (
    <span className={`font-display tabular-nums text-xs font-bold uppercase ${stateColor}`}>
      {cb.state}
      {cb.state === "open" && cb.seconds_until_retry != null
        ? ` (retry in ${cb.seconds_until_retry}s)`
        : cb.state !== "closed"
          ? ` (${cb.consecutive_failures}/${cb.failure_threshold} fails)`
          : ""}
    </span>
  );
}

export default function PositionStocksTab() {
  const [apiUrlInput, setApiUrlInput] = useState(getPositionStocksApiUrl());
  const [status, setStatus] = useState<ScalpStatus | null>(null);
  const [positions, setPositions] = useState<ScalpPositionRow[]>([]);
  const [candidates, setCandidates] = useState<ScalpCandidateRow[]>([]);
  const [ledger, setLedger] = useState<ScalpLedgerState | null>(null);
  const [tradeHistory, setTradeHistory] = useState<ScalpTradeHistory | null>(null);
  const [dhanLive, setDhanLive] = useState<DhanLiveOrders | null>(null);
  const [dhanLiveError, setDhanLiveError] = useState<string | null>(null);
  const [lastCycleResult, setLastCycleResult] = useState<ScalpCycleResult | null>(null);
  const [windowFilter, setWindowFilter] = useState<Window | "all">("all");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmKill, setConfirmKill] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  const [loggedIn, setLoggedIn] = useState(!!getSessionToken());
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [loginError, setLoginError] = useState<string | null>(null);
  const [loginLoading, setLoginLoading] = useState(false);

  useEffect(() => {
    setSessionExpiredHandler(() => setLoggedIn(false));
    return () => setSessionExpiredHandler(null);
  }, []);

  const doLogin = async () => {
    setLoginLoading(true); setLoginError(null);
    try {
      const res = await positionStocksApi.login(username, password);
      setSessionToken(res.token); setLoggedIn(true); setPassword("");
    } catch (e: any) {
      setLoginError(e?.message || "Login failed");
    } finally { setLoginLoading(false); }
  };

  const doLogout = async () => {
    try { await positionStocksApi.logout(); } catch { /* fine */ }
    setSessionToken(null); setLoggedIn(false);
  };

  const loadAll = useCallback(async () => {
    if (!getPositionStocksApiUrl()) return;
    try {
      const [s, p, c, l, h] = await Promise.all([
        positionStocksApi.status(),
        positionStocksApi.positions(),
        positionStocksApi.candidates(),
        positionStocksApi.ledger().catch(() => null),   // don't let /ledger 500 kill the whole poll
        positionStocksApi.tradeHistory(200),
      ]);
      setStatus(s); setPositions(p); setCandidates(c);
      if (l) setLedger(l);
      setTradeHistory(h);
      setError(null);
      setLastRefreshed(new Date());
    } catch (e: any) {
      setError(e?.message || "Failed to reach position-stocks-service");
    }
  }, []);

  const loadDhanLive = useCallback(async () => {
    try {
      const d = await positionStocksApi.dhanLiveOrders();
      setDhanLive(d); setDhanLiveError(null);
    } catch (e: any) {
      setDhanLiveError(e?.message || "Failed to fetch live Dhan orders");
    }
  }, []);

  useEffect(() => {
    void loadAll();
    void loadDhanLive();
    const t = setInterval(() => void loadAll(), 15_000);
    const td = setInterval(() => void loadDhanLive(), 30_000);
    return () => { clearInterval(t); clearInterval(td); };
  }, [loadAll, loadDhanLive]);

  const saveApiUrl = () => { setPositionStocksApiUrl(apiUrlInput); void loadAll(); };

  const doAction = async (action: string) => {
    setBusy(action); setError(null);
    try {
      if (action === "arm") await positionStocksApi.arm();
      else if (action === "disarm") await positionStocksApi.disarm();
      else if (action === "kill") { await positionStocksApi.kill(); setConfirmKill(false); }
      else if (action === "sync") await positionStocksApi.syncLedger();
      else if (action === "reconcile") await positionStocksApi.reconcile();
      else if (action === "service_enable") await positionStocksApi.serviceEnable();
      else if (action === "service_disable") await positionStocksApi.serviceDisable();
      else if (action === "autopilot_enable") await positionStocksApi.autopilotEnable();
      else if (action === "autopilot_disable") await positionStocksApi.autopilotDisable();
      else if (action === "run_cycle") { const r = await positionStocksApi.runCycle(); setLastCycleResult(r); }
      await loadAll();
    } catch (e: any) {
      setError(e?.message || `${action} failed`);
    } finally { setBusy(null); }
  };

  const openPositions = useMemo(() => positions.filter(p => p.status === "OPEN"), [positions]);
  const closedToday = useMemo(() => positions.filter(p => p.status !== "OPEN"), [positions]);
  const filteredCandidates = useMemo(
    () => windowFilter === "all" ? candidates : candidates.filter(c => c.window === windowFilter),
    [candidates, windowFilter]
  );
  const grouped = useMemo(() => {
    const g: Record<Window, ScalpCandidateRow[]> = { "1m": [], "5m": [], "15m": [], "60m": [] };
    for (const c of filteredCandidates) g[c.window]?.push(c);
    return g;
  }, [filteredCandidates]);

  if (!getPositionStocksApiUrl()) {
    return (
      <div className="p-4 max-w-lg mx-auto">
        <p className="font-display tabular-nums text-xs text-mist mb-1 uppercase tracking-widest">Position Stocks Service URL</p>
        <p className="font-display tabular-nums text-[11px] text-mist mb-3">Paste your position-stocks-service URL (port 8006).</p>
        <div className="flex gap-2">
          <input
            className="flex-1 bg-graphite border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none focus:border-slate"
            placeholder="https://stockky-position-stocks.onrender.com"
            value={apiUrlInput}
            onChange={e => setApiUrlInput(e.target.value)}
          />
          <button onClick={saveApiUrl} className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy">Save</button>
        </div>
      </div>
    );
  }

  const killSwitchTripped = status?.daily_loss_kill_switch || ledger?.daily_loss_kill_switch_tripped;

  return (
    <div className="page-terminal space-y-4">
      <div className="flex items-center justify-between">
        <p className="dash-section-title">Position Stocks — 5m / 15m / 60m Scalp Pool</p>
        <span className="font-display tabular-nums text-[9px] text-mist">
          {lastRefreshed ? `Updated ${lastRefreshed.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}` : "Refreshing…"}
        </span>
      </div>

      {error && (
        <div className="rounded-xl border border-signal-sell/40 bg-signal-sell/10 px-3 py-2 font-display tabular-nums text-xs text-signal-sell">
          {error}
        </div>
      )}

      {!status?.risk_confirmed && status != null && (
        <div className="rounded-xl border border-signal-hold/40 bg-signal-hold/10 px-3 py-2 font-display tabular-nums text-[11px] text-signal-hold">
          ⚠ RISK_PER_TRADE_PCT not yet confirmed — running on a placeholder value ({status?.risk_per_trade_pct ?? "—"}%). Set RISK_PER_TRADE_PCT_CONFIRMED=true in the service env.
        </div>
      )}

      {killSwitchTripped && (
        <div className="rounded-xl border border-signal-sell/60 bg-signal-sell/15 px-3 py-2 font-display tabular-nums text-xs text-signal-sell font-bold">
          🔴 DAILY LOSS KILL SWITCH TRIPPED — No new entries for the rest of the day.
        </div>
      )}

      {/* ── Admin auth ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        {!loggedIn ? (
          <>
            <p className="dash-section-title mb-2">Admin login required</p>
            <p className="font-display tabular-nums text-[10px] text-mist mb-2">Same admin credentials as Real Automatic Trade.</p>
            {loginError && <p className="font-display tabular-nums text-[11px] text-signal-sell mb-2">{loginError}</p>}
            <div className="flex flex-col sm:flex-row gap-2">
              <input className="flex-1 bg-ink border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none"
                placeholder="Admin username" value={username} onChange={e => setUsername(e.target.value)} />
              <input type="password" className="flex-1 bg-ink border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none"
                placeholder="Password" value={password} onChange={e => setPassword(e.target.value)}
                onKeyDown={e => e.key === "Enter" && void doLogin()} />
              <button onClick={() => void doLogin()} disabled={loginLoading || !password}
                className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy disabled:opacity-40">
                {loginLoading ? "Logging in…" : "Log In"}
              </button>
            </div>
          </>
        ) : (
          <div className="flex items-center justify-between">
            <p className="font-display tabular-nums text-xs text-signal-buy">● Admin session active ({username})</p>
            <button onClick={() => void doLogout()} className="font-display tabular-nums text-[11px] text-mist hover:text-paper">Log out</button>
          </div>
        )}
      </div>

      {/* ── 6-cell status grid ── */}
      <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
        {[
          { label: "Armed", value: status?.armed ? "ARMED" : "DISARMED", color: status?.armed ? "text-signal-buy" : "text-signal-avoid" },
          { label: "Market", value: status?.market_open ? "OPEN" : "CLOSED", color: status?.market_open ? "text-signal-buy" : "text-mist" },
          { label: "WS Feed", value: status?.ws?.connected ? "LIVE" : "DOWN", color: status?.ws?.connected ? "text-signal-buy" : "text-signal-sell" },
          { label: "Kill Switch", value: killSwitchTripped ? "TRIPPED" : "clear", color: killSwitchTripped ? "text-signal-sell" : "text-signal-buy" },
          { label: "Module", value: status?.service_enabled ? "ENABLED" : "PAUSED", color: status?.service_enabled ? "text-signal-buy" : "text-signal-avoid" },
          { label: "Auto-Pilot", value: status?.auto_pilot_enabled ? "ON" : "OFF", color: status?.auto_pilot_enabled ? "text-signal-buy" : "text-signal-hold" },
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
            <p className="text-[9px] text-mist uppercase tracking-widest">Orders Today</p>
            <p className="font-display tabular-nums text-xs text-paper">{status?.orders_placed_today ?? "—"}</p>
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

      {lastCycleResult && (
        <div className="rounded-xl border border-slate bg-graphite px-3 py-2 font-display tabular-nums text-[11px] text-paper">
          Last manual cycle: {lastCycleResult.candidates_seen} candidate(s) seen
          {lastCycleResult.entered_symbol ? ` · entered ${lastCycleResult.entered_symbol}` : ""}
          {lastCycleResult.eod_fired ? " · EOD squareoff fired" : ""}
          {lastCycleResult.skipped_reason ? ` · skipped: ${lastCycleResult.skipped_reason}` : ""}
        </div>
      )}

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
        <button disabled={!loggedIn || busy !== null || !!status?.auto_pilot_enabled} onClick={() => doAction("autopilot_enable")}
          className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy disabled:opacity-40">
          {busy === "autopilot_enable" ? "Enabling…" : "Enable Auto-Pilot"}
        </button>
        <button disabled={!loggedIn || busy !== null || !status?.auto_pilot_enabled} onClick={() => doAction("autopilot_disable")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-paper disabled:opacity-40">
          {busy === "autopilot_disable" ? "Disabling…" : "Disable Auto-Pilot"}
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
          Log in above to enable action buttons. All controls require the same admin password as Real Automatic Trade.
        </p>
      )}

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
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest">P&L Total</p>
            <p className={`font-display tabular-nums font-bold text-sm ${(ledger?.realized_pnl_total ?? 0) >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
              {fmtInr(ledger?.realized_pnl_total)}
            </p>
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

      {/* ── Live screener ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="dash-section-title">Live Screener</p>
          <div className="flex gap-1">
            {(["all", "1m", "5m", "15m", "60m"] as const).map(w => (
              <button key={w} onClick={() => setWindowFilter(w)}
                className={`px-2 py-1 rounded-lg font-display tabular-nums text-[10px] uppercase border ${
                  windowFilter === w ? "bg-signal-prepare/20 border-signal-prepare/40 text-signal-prepare" : "border-slate text-mist"
                }`}>{w}</button>
            ))}
          </div>
        </div>
        {windowFilter === "all" ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            {(["1m", "5m", "15m", "60m"] as Window[]).map(w => (
              <div key={w}>
                <p className="text-[9px] text-mist uppercase tracking-widest mb-1">{w} window</p>
                <CandidateList rows={grouped[w]} />
              </div>
            ))}
          </div>
        ) : (
          <CandidateList rows={filteredCandidates} />
        )}
      </div>

      {/* ── Open positions ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">Open Positions ({openPositions.length}/5)</p>
        {openPositions.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">No open scalp positions.</p>
        ) : (
          <div className="space-y-2">{openPositions.map(p => <PositionRow key={p.id} p={p} />)}</div>
        )}
      </div>

      {/* ── Closed today ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">Closed Today ({closedToday.length})</p>
        {closedToday.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">No positions closed yet today.</p>
        ) : (
          <div className="space-y-2">{closedToday.map(p => <PositionRow key={p.id} p={p} />)}</div>
        )}
      </div>

      {/* ── Trade history ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">Trade History — Buy vs Sell, Win Rate</p>
        {tradeHistory ? (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
              <div>
                <p className="text-[9px] text-mist uppercase tracking-widest">Total Trades</p>
                <p className="font-display tabular-nums font-bold text-sm text-paper">{tradeHistory.summary.total_trades}</p>
              </div>
              <div>
                <p className="text-[9px] text-mist uppercase tracking-widest">Win Rate</p>
                <p className="font-display tabular-nums font-bold text-sm text-paper">
                  {tradeHistory.summary.win_rate_pct != null ? `${tradeHistory.summary.win_rate_pct}%` : "—"}
                  <span className="text-[10px] text-mist ml-1">({tradeHistory.summary.wins}W / {tradeHistory.summary.losses}L)</span>
                </p>
              </div>
              <div>
                <p className="text-[9px] text-mist uppercase tracking-widest">Total P&L</p>
                <p className={`font-display tabular-nums font-bold text-sm ${tradeHistory.summary.total_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                  {fmtInr(tradeHistory.summary.total_pnl)}
                </p>
              </div>
              <div>
                <p className="text-[9px] text-mist uppercase tracking-widest">Best / Worst</p>
                <p className="font-display tabular-nums text-[11px]">
                  {tradeHistory.summary.best_trade
                    ? <span className="text-signal-buy">{tradeHistory.summary.best_trade.symbol} {fmtInr(tradeHistory.summary.best_trade.pnl)}</span>
                    : <span className="text-mist">—</span>}
                  <span className="text-mist"> / </span>
                  {tradeHistory.summary.worst_trade
                    ? <span className="text-signal-sell">{tradeHistory.summary.worst_trade.symbol} {fmtInr(tradeHistory.summary.worst_trade.pnl)}</span>
                    : <span className="text-mist">—</span>}
                </p>
              </div>
            </div>

            {/* Win/loss breakdown bar */}
            {tradeHistory.summary.total_trades > 0 && (
              <div className="mb-3">
                <div className="h-1.5 rounded-full overflow-hidden flex">
                  <div className="bg-signal-buy h-full transition-all"
                    style={{ width: `${((tradeHistory.summary.wins / tradeHistory.summary.total_trades) * 100).toFixed(1)}%` }} />
                  <div className="bg-signal-sell h-full flex-1" />
                </div>
                <div className="flex justify-between font-display tabular-nums text-[9px] text-mist mt-1">
                  <span>Wins: {tradeHistory.summary.wins}</span>
                  <span>Losses: {tradeHistory.summary.losses}</span>
                </div>
              </div>
            )}

            {tradeHistory.trades.length === 0 ? (
              <p className="font-display tabular-nums text-xs text-mist">No trades recorded yet.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-[11px] font-display tabular-nums">
                  <thead>
                    <tr className="text-mist text-left border-b border-slate">
                      <th className="py-1 pr-3">Symbol</th>
                      <th className="py-1 pr-3">Window</th>
                      <th className="py-1 pr-3">Buy ₹</th>
                      <th className="py-1 pr-3">Sell ₹</th>
                      <th className="py-1 pr-3">Qty</th>
                      <th className="py-1 pr-3">P&L</th>
                      <th className="py-1 pr-3">Status</th>
                      <th className="py-1">Closed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tradeHistory.trades.slice(0, 50).map(t => (
                      <tr key={t.id} className="border-b border-slate/50">
                        <td className="py-1 pr-3 text-paper font-bold">{t.symbol}</td>
                        <td className="py-1 pr-3 text-mist">{t.window_source}</td>
                        <td className="py-1 pr-3 text-paper">₹{t.entry_price.toFixed(2)}</td>
                        <td className="py-1 pr-3 text-paper">{t.exit_price != null ? `₹${t.exit_price.toFixed(2)}` : "—"}</td>
                        <td className="py-1 pr-3 text-mist">{t.quantity}</td>
                        <td className={`py-1 pr-3 ${t.realized_pnl != null && t.realized_pnl >= 0 ? "text-signal-buy" : t.realized_pnl != null ? "text-signal-sell" : "text-mist"}`}>
                          {t.realized_pnl != null ? `${fmtInr(t.realized_pnl)} (${(t.realized_pnl_pct ?? 0).toFixed(2)}%)` : "—"}
                        </td>
                        <td className={`py-1 pr-3 ${statusColor(t.status)}`}>{t.status}</td>
                        <td className="py-1 text-mist">{fmtDateTimeIst(t.closed_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        ) : (
          <p className="font-display tabular-nums text-xs text-mist">Loading trade history…</p>
        )}
      </div>

      {/* ── Live Dhan Orders ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="dash-section-title">Live Dhan Order Activity</p>
          <button onClick={() => void loadDhanLive()}
            className="px-3 py-1 rounded-lg bg-graphite border border-slate font-display tabular-nums text-[10px] text-mist">
            Refresh
          </button>
        </div>
        {dhanLiveError ? (
          <p className="font-display tabular-nums text-[11px] text-signal-sell">{dhanLiveError}</p>
        ) : !dhanLive ? (
          <p className="font-display tabular-nums text-xs text-mist">Loading…</p>
        ) : dhanLive.orders.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">No live scalp orders on Dhan right now.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-display tabular-nums">
              <thead>
                <tr className="text-mist text-left border-b border-slate">
                  <th className="py-1 pr-3">Symbol</th>
                  <th className="py-1 pr-3">Leg</th>
                  <th className="py-1 pr-3">Side</th>
                  <th className="py-1 pr-3">Qty</th>
                  <th className="py-1 pr-3">Price</th>
                  <th className="py-1 pr-3">Trigger</th>
                  <th className="py-1">Status</th>
                </tr>
              </thead>
              <tbody>
                {dhanLive.orders.map((o, i) => (
                  <tr key={`${o.orderId ?? i}-${o.legName ?? ""}`} className="border-b border-slate/50">
                    <td className="py-1 pr-3 text-paper font-bold">{String(o.tradingSymbol ?? "—")}</td>
                    <td className="py-1 pr-3 text-mist">{String(o.legName ?? "—")}</td>
                    <td className={`py-1 pr-3 ${o.transactionType === "BUY" ? "text-signal-buy" : o.transactionType === "SELL" ? "text-signal-sell" : "text-mist"}`}>
                      {String(o.transactionType ?? "—")}
                    </td>
                    <td className="py-1 pr-3 text-mist">{String(o.quantity ?? "—")}</td>
                    <td className="py-1 pr-3 text-paper">{o.price != null ? `₹${o.price}` : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{o.triggerPrice != null ? `₹${o.triggerPrice}` : "—"}</td>
                    <td className="py-1 text-paper">{String(o.orderStatus ?? "—")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="font-display tabular-nums text-[9px] text-mist mt-2">
          Pulled directly from Dhan's super-order book — broker-side truth, can lag the positions list above by up to one reconciliation cycle.
        </p>
      </div>

      {/* ── Settings ── */}
      <details className="bg-graphite border border-slate rounded-2xl p-4">
        <summary className="font-display tabular-nums text-xs text-mist cursor-pointer">▶ Settings</summary>
        <p className="font-display tabular-nums text-[10px] text-mist mt-2 mb-1">Position Stocks service URL</p>
        <div className="flex gap-2">
          <input
            className="flex-1 bg-ink border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none"
            value={apiUrlInput}
            onChange={e => setApiUrlInput(e.target.value)}
          />
          <button onClick={saveApiUrl} className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy">Save</button>
        </div>
      </details>
    </div>
  );
}

function CandidateList({ rows }: { rows: ScalpCandidateRow[] }) {
  if (rows.length === 0) return <p className="font-display tabular-nums text-[11px] text-mist">No candidates.</p>;
  return (
    <div className="space-y-1">
      {rows.slice(0, 10).map((c, i) => (
        <div key={`${c.symbol}-${c.window}-${i}`} className="flex items-center justify-between border-t border-slate pt-1 first:border-t-0 first:pt-0">
          <span className="font-display tabular-nums text-xs text-paper">{c.symbol}</span>
          <span className={`font-display tabular-nums text-xs ${c.pct_change >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
            {c.pct_change >= 0 ? "+" : ""}{c.pct_change.toFixed(2)}%
          </span>
          <span className="font-display tabular-nums text-[10px] text-mist">score {c.composite_score.toFixed(1)}</span>
        </div>
      ))}
    </div>
  );
}

function PositionRow({ p }: { p: ScalpPositionRow }) {
  const pnlPct = p.realized_pnl_pct ?? (p.exit_price && p.entry_price
    ? ((p.exit_price - p.entry_price) / p.entry_price) * 100
    : null);
  return (
    <div className="border border-slate rounded-xl p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="font-display tabular-nums font-bold text-sm text-paper">{p.symbol}</span>
        <div className="flex items-center gap-2">
          <span className="font-display tabular-nums text-[10px] text-mist">{p.window_source}</span>
          <span className={`font-display tabular-nums text-[10px] uppercase font-bold ${statusColor(p.status)}`}>{p.status}</span>
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2 text-[11px]">
        <div><span className="text-mist">Buy </span><span className="tabular-nums text-paper">₹{p.entry_price.toFixed(2)}</span></div>
        <div><span className="text-mist">Sell </span><span className="tabular-nums text-paper">{p.exit_price != null ? `₹${p.exit_price.toFixed(2)}` : "—"}</span></div>
        <div><span className="text-mist">Qty </span><span className="tabular-nums text-paper">{p.quantity}</span></div>
        <div><span className="text-mist">Target </span><span className="tabular-nums text-signal-buy">₹{p.target_price.toFixed(2)} <span className="text-[9px]">({p.adaptive_target_pct.toFixed(1)}%)</span></span></div>
        <div><span className="text-mist">Stop </span><span className="tabular-nums text-signal-sell">₹{p.stop_price.toFixed(2)} <span className="text-[9px]">({p.adaptive_stop_pct.toFixed(1)}%)</span></span></div>
      </div>
      {p.realized_pnl != null && (
        <p className={`font-display tabular-nums text-xs mt-2 font-bold ${p.realized_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
          P&L {fmtInr(p.realized_pnl)} {pnlPct != null ? `(${pnlPct.toFixed(2)}%)` : ""}
        </p>
      )}
      <p className="font-display tabular-nums text-[9px] text-mist mt-1">
        Opened {fmtDateTimeIst(p.opened_at)}
        {p.closed_at ? ` · Closed ${fmtDateTimeIst(p.closed_at)}` : ""}
        {p.is_first_live_order ? " · 🟡 FIRST LIVE ORDER" : ""}
        {p.dhan_super_order_id ? ` · Order ${p.dhan_super_order_id}` : ""}
      </p>
    </div>
  );
}
