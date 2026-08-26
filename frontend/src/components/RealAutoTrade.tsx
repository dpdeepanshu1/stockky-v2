import { useCallback, useEffect, useState } from "react";
import {
  realTradeApi, getRealTradeApiUrl, setRealTradeApiUrl,
  getSessionToken, setSessionToken, type GateStatus, type AuditLogRow,
  type Position, type OrderRow, type CycleResult,
} from "../realTradeApi";

type Mode = "DEMO" | "REAL";

/**
 * Phase 1 UI: everything up through ARM/DISARM and the audit trail. There
 * is deliberately no "place order" control anywhere in this component —
 * entry_engine/exit_engine don't exist yet (Phase 2), so arming today
 * only proves the gate sequence and risk engine work; it does not cause
 * any order to be sent anywhere.
 */
export default function RealAutoTrade() {
  const [mode, setMode] = useState<Mode>("DEMO");
  const [apiUrlInput, setApiUrlInput] = useState(getRealTradeApiUrl());
  const [status, setStatus] = useState<GateStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const [loggedIn, setLoggedIn] = useState(!!getSessionToken());
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");

  const [dhanClientId, setDhanClientId] = useState("");
  const [dhanToken, setDhanToken] = useState("");
  const [dhanDetail, setDhanDetail] = useState<{
    client_id_masked: string | null;
    token_expires_at: string | null;
    days_remaining: number | null;
  } | null>(null);
  const [showDhanForm, setShowDhanForm] = useState(false);

  const [auditRows, setAuditRows] = useState<AuditLogRow[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [orders, setOrders] = useState<OrderRow[]>([]);
  const [cycleBusy, setCycleBusy] = useState(false);
  const [cycleResult, setCycleResult] = useState<CycleResult | null>(null);

  const loadStatus = useCallback(async (m: Mode) => {
    try {
      const s = await realTradeApi.gateStatus(m);
      setStatus(s);
      // Detailed Dhan status (masked client id + expiry countdown) is its
      // own admin-only endpoint — only worth calling in REAL mode, and
      // only once logged in (it 401s otherwise, which loadStatus already
      // handles by dropping the session token).
      if (m === "REAL" && getSessionToken()) {
        try {
          const d = await realTradeApi.dhanStatus();
          setDhanDetail(d.connected ? d : null);
        } catch {
          setDhanDetail(null);
        }
      } else {
        setDhanDetail(null);
      }
    } catch (e: any) {
      setError(e?.message || "Failed to load status");
    }
  }, []);

  useEffect(() => {
    if (getRealTradeApiUrl()) void loadStatus(mode);
  }, [mode, loadStatus]);

  const saveApiUrl = () => {
    setRealTradeApiUrl(apiUrlInput);
    void loadStatus(mode);
  };

  const doLogin = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await realTradeApi.login(username, password);
      setSessionToken(res.token);
      setLoggedIn(true);
      setPassword("");
      await loadStatus(mode);
    } catch (e: any) {
      setError(e?.message || "Login failed");
    } finally {
      setLoading(false);
    }
  };

  const doLogout = async () => {
    try {
      await realTradeApi.logout();
    } catch {
      /* token may already be invalid — clear locally regardless */
    }
    setSessionToken(null);
    setLoggedIn(false);
    void loadStatus(mode);
  };

  const doConnectDhan = async () => {
    setLoading(true);
    setError(null);
    try {
      await realTradeApi.connectDhan(dhanClientId, dhanToken);
      setDhanToken(""); // never keep the pasted token in component state after sending
      setDhanClientId("");
      setShowDhanForm(false);
      await loadStatus(mode);
    } catch (e: any) {
      setError(e?.message || "Failed to connect Dhan");
    } finally {
      setLoading(false);
    }
  };

  const doConfirmRisk = async () => {
    try {
      await realTradeApi.confirmRiskConfig(mode);
      await loadStatus(mode);
    } catch (e: any) {
      setError(e?.message || "Failed to confirm risk config");
    }
  };

  const doArm = async () => {
    setLoading(true);
    setError(null);
    try {
      await realTradeApi.arm(mode);
      await loadStatus(mode);
    } catch (e: any) {
      setError(e?.message || "Failed to arm");
    } finally {
      setLoading(false);
    }
  };

  const doDisarm = async () => {
    try {
      await realTradeApi.disarm(mode);
      await loadStatus(mode);
    } catch (e: any) {
      setError(e?.message || "Failed to disarm");
    }
  };

  const doEmergencyPause = async () => {
    if (!window.confirm("Pause ALL trading (DEMO and REAL)? This disarms both modes immediately.")) return;
    try {
      await realTradeApi.emergencyPause();
      await loadStatus(mode);
    } catch (e: any) {
      setError(e?.message || "Failed to pause");
    }
  };

  const loadAudit = async () => {
    try {
      setAuditRows(await realTradeApi.auditLog(mode, 30));
    } catch (e: any) {
      setError(e?.message || "Failed to load audit log");
    }
  };

  const loadPositionsAndOrders = useCallback(async (m: Mode) => {
    try {
      const [p, o] = await Promise.all([realTradeApi.positions(m), realTradeApi.orders(m, 20)]);
      setPositions(p);
      setOrders(o);
    } catch {
      // Positions/orders panel is best-effort — a stale/unreachable read
      // here shouldn't block the rest of the page (status, arm/disarm)
      // from working.
    }
  }, []);

  const doRunCycle = async () => {
    setCycleBusy(true);
    setError(null);
    try {
      const res = await realTradeApi.runCycle(mode);
      setCycleResult(res);
      await Promise.all([loadStatus(mode), loadPositionsAndOrders(mode)]);
    } catch (e: any) {
      setError(e?.message || "Cycle failed");
    } finally {
      setCycleBusy(false);
    }
  };

  useEffect(() => {
    if (getRealTradeApiUrl() && (mode === "DEMO" || loggedIn)) {
      void loadPositionsAndOrders(mode);
    }
  }, [mode, loggedIn, loadPositionsAndOrders]);

  if (!getRealTradeApiUrl()) {
    return (
      <div className="page-terminal max-w-lg">
        <p className="dash-section-title">Real Automatic Trade — Setup</p>
        <p className="font-mono text-xs text-paper/60 mb-3">
          Enter the real-trade-service URL (its own Render deploy, separate from the main API Gateway).
        </p>
        <div className="flex gap-2">
          <input
            className="flex-1 bg-graphite border border-white/10 rounded px-3 py-2 font-mono text-xs"
            placeholder="https://stockky-real-trade.onrender.com"
            value={apiUrlInput}
            onChange={(e) => setApiUrlInput(e.target.value)}
          />
          <button onClick={saveApiUrl} className="px-4 py-2 rounded bg-emerald-600/30 border border-emerald-500/50 font-mono text-xs">
            Save
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="page-terminal max-w-3xl">
      <p className="dash-section-title">🤖 Real Automatic Trade</p>
      <p className="font-mono text-[11px] text-amber-300/80 mb-4">
        Phase 2: candidates, risk-checked entries, DEMO fills, and exit
        management (stop/target/trailing/time-stop) are live for DEMO mode.
        REAL mode computes and logs identical decisions but does not place
        live orders yet — that's Phase 3.
      </p>
      {mode === "DEMO" && (
        <p className="font-mono text-[11px] text-emerald-300/70 mb-4">
          DEMO mode is open — no login required. Everything here uses paper
          capital only.
        </p>
      )}

      {error && (
        <div className="mb-3 px-3 py-2 rounded bg-rose-950/40 border border-rose-500/40 font-mono text-xs text-rose-200">
          {error}
        </div>
      )}

      {/* Mode toggle */}
      <div className="flex gap-2 mb-4">
        {(["DEMO", "REAL"] as Mode[]).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={`px-4 py-2 rounded-lg font-mono text-xs border ${
              mode === m
                ? m === "DEMO"
                  ? "bg-emerald-600/30 border-emerald-500/60 text-emerald-200"
                  : "bg-rose-600/30 border-rose-500/60 text-rose-200"
                : "bg-graphite border-white/10 text-paper/50"
            }`}
          >
            {m === "DEMO" ? "🟢 DEMO / TRAINING" : "🔴 REAL TRADE"}
          </button>
        ))}
      </div>

      {mode === "REAL" && !loggedIn ? (
        <div className="border border-white/10 rounded-lg p-4 mb-4">
          <p className="font-mono text-xs text-paper/70 mb-3">Admin Authentication</p>
          <div className="space-y-2">
            <input
              className="w-full bg-graphite border border-white/10 rounded px-3 py-2 font-mono text-xs"
              placeholder="Admin ID"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
            <input
              type="password"
              className="w-full bg-graphite border border-white/10 rounded px-3 py-2 font-mono text-xs"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void doLogin()}
            />
            <button
              onClick={() => void doLogin()}
              disabled={loading || !password}
              className="w-full px-4 py-2 rounded bg-sky-600/30 border border-sky-500/60 font-mono text-xs disabled:opacity-50"
            >
              {loading ? "Authenticating…" : "Authenticate"}
            </button>
          </div>
        </div>
      ) : (
        <>
          {mode === "REAL" && loggedIn && (
            <div className="flex justify-end mb-2">
              <button onClick={() => void doLogout()} className="font-mono text-[11px] text-paper/50 underline">
                Log out
              </button>
            </div>
          )}

          {/* Gate status */}
          {status && (
            <div className="border border-white/10 rounded-lg p-4 mb-4 space-y-2 font-mono text-xs">
              <div className="flex justify-between">
                <span>Admin authenticated</span>
                <span>{status.admin_authenticated ? "🟢" : "🔴"}</span>
              </div>
              {mode === "REAL" && (
                <div className="flex justify-between">
                  <span>Dhan connected</span>
                  <span>{status.dhan_connected ? "🟢" : "🔴"}</span>
                </div>
              )}
              {mode === "REAL" && status.dhan_connected && dhanDetail?.days_remaining != null && (
                <div className="flex justify-between">
                  <span>Dhan token expires</span>
                  <span
                    className={
                      dhanDetail.days_remaining <= 3
                        ? "text-red-400"
                        : dhanDetail.days_remaining <= 7
                        ? "text-amber-300"
                        : "text-emerald-300"
                    }
                  >
                    {dhanDetail.days_remaining <= 0
                      ? "expired"
                      : `${Math.floor(dhanDetail.days_remaining)}d left (${dhanDetail.client_id_masked})`}
                  </span>
                </div>
              )}
              <div className="flex justify-between">
                <span>Risk config confirmed</span>
                <span>{status.risk_config_confirmed ? "🟢" : "🔴"}</span>
              </div>
              <div className="flex justify-between font-bold">
                <span>Armed</span>
                <span>{status.armed ? "🟢 ACTIVE" : "🔴 OFF"}</span>
              </div>
              {status.disarmed_reason && (
                <p className="text-amber-300/80 text-[11px]">Last disarm reason: {status.disarmed_reason}</p>
              )}
            </div>
          )}

          {/* Dhan connect / rotate (REAL only) — always reachable, not just
              on first connect, since a 30-day token needs re-pasting well
              before the hard expiry disarms live trading. */}
          {mode === "REAL" && status && (!status.dhan_connected || showDhanForm) && (
            <div className="border border-white/10 rounded-lg p-4 mb-4">
              <p className="font-mono text-xs text-paper/70 mb-3">
                {status.dhan_connected ? "Rotate Dhan Token" : "Connect Dhan Account"}
              </p>
              <div className="space-y-2">
                <input
                  className="w-full bg-graphite border border-white/10 rounded px-3 py-2 font-mono text-xs"
                  placeholder="Dhan Client ID"
                  value={dhanClientId}
                  onChange={(e) => setDhanClientId(e.target.value)}
                />
                <input
                  type="password"
                  className="w-full bg-graphite border border-white/10 rounded px-3 py-2 font-mono text-xs"
                  placeholder="Dhan Access Token (generate from web.dhan.co → DhanHQ Trading APIs; valid up to 30 days unless TOTP is enabled)"
                  value={dhanToken}
                  onChange={(e) => setDhanToken(e.target.value)}
                />
                <button
                  onClick={() => void doConnectDhan()}
                  disabled={loading || !dhanClientId || !dhanToken}
                  className="w-full px-4 py-2 rounded bg-sky-600/30 border border-sky-500/60 font-mono text-xs disabled:opacity-50"
                >
                  {loading ? "Connecting…" : status.dhan_connected ? "Save New Token" : "Connect Dhan"}
                </button>
                {status.dhan_connected && (
                  <button
                    onClick={() => setShowDhanForm(false)}
                    className="w-full px-4 py-2 rounded border border-white/10 font-mono text-xs text-paper/60"
                  >
                    Cancel
                  </button>
                )}
              </div>
            </div>
          )}
          {mode === "REAL" && status?.dhan_connected && !showDhanForm && (
            <div className="flex justify-end mb-4">
              <button
                onClick={() => setShowDhanForm(true)}
                className="font-mono text-[11px] text-paper/50 underline"
              >
                Rotate Dhan token
              </button>
            </div>
          )}

          {/* Risk config summary + confirm */}
          {status?.risk_config && (
            <div className="border border-white/10 rounded-lg p-4 mb-4 font-mono text-xs">
              <p className="text-paper/70 mb-2">Risk Configuration ({mode})</p>
              <div className="grid grid-cols-2 gap-2 text-paper/80">
                <span>Risk per trade</span><span>{status.risk_config.risk_per_trade_pct}%</span>
                <span>Max daily loss</span><span>{status.risk_config.max_daily_loss_pct}%</span>
                <span>Max concurrent positions</span><span>{status.risk_config.max_concurrent_positions}</span>
                <span>Max portfolio risk</span><span>{status.risk_config.max_portfolio_risk_pct}%</span>
              </div>
              {!status.risk_config_confirmed && (
                <button
                  onClick={() => void doConfirmRisk()}
                  className="mt-3 w-full px-4 py-2 rounded bg-amber-600/30 border border-amber-500/60 text-xs"
                >
                  Confirm Risk Configuration
                </button>
              )}
            </div>
          )}

          {/* Arm / Disarm */}
          <div className="flex gap-2 mb-4">
            {status?.armed ? (
              <button onClick={() => void doDisarm()} className="flex-1 px-4 py-3 rounded-lg bg-rose-600/30 border border-rose-500/60 font-mono text-xs">
                🛑 DISARM
              </button>
            ) : (
              <button
                onClick={() => void doArm()}
                disabled={loading}
                className="flex-1 px-4 py-3 rounded-lg bg-emerald-600/30 border border-emerald-500/60 font-mono text-xs disabled:opacity-50"
              >
                {loading ? "Arming…" : "⚡ ARM"}
              </button>
            )}
            <button onClick={() => void doEmergencyPause()} className="px-4 py-3 rounded-lg bg-rose-900/50 border border-rose-500/70 font-mono text-xs">
              🚨 PAUSE ALL
            </button>
          </div>

          {/* Account snapshot */}
          {status?.account && (
            <div className="border border-white/10 rounded-lg p-4 mb-4 font-mono text-xs">
              <p className="text-paper/70 mb-2">Account ({mode})</p>
              <div className="grid grid-cols-2 gap-2 text-paper/80">
                <span>Starting capital</span><span>₹{status.account.starting_capital ?? "—"}</span>
                <span>Current equity</span><span>₹{status.account.current_equity ?? "—"}</span>
                <span>Cash available</span><span>₹{status.account.cash_available ?? "—"}</span>
                <span>Realized P&amp;L today</span><span>₹{status.account.realized_pnl_today ?? "—"}</span>
              </div>
            </div>
          )}

          {/* Run cycle — Phase 2: candidates -> entries -> fills -> exits */}
          {status?.armed && (
            <div className="border border-white/10 rounded-lg p-4 mb-4">
              <div className="flex items-center justify-between mb-2">
                <p className="font-mono text-xs text-paper/70">
                  AI Trade Pipeline — Candidate → Entry → Risk → {mode === "DEMO" ? "Simulated Fill" : "Execution"} → Position → Exit
                </p>
                <button
                  onClick={() => void doRunCycle()}
                  disabled={cycleBusy}
                  className="font-mono text-xs px-3 py-1.5 rounded bg-emerald-600/30 border border-emerald-500/60 disabled:opacity-50"
                >
                  {cycleBusy ? "Running…" : "▶ Run Cycle"}
                </button>
              </div>
              {mode === "REAL" && (
                <p className="font-mono text-[11px] text-amber-300/70 mb-2">
                  REAL mode computes and risk-checks every decision but does not place live orders yet (Phase 3).
                </p>
              )}
              {(cycleBusy || cycleResult) && (
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-6 gap-2 mt-1">
                  {[
                    { label: "Candidates", value: cycleResult?.new_candidates, color: "text-sky-300" },
                    { label: "Entered", value: cycleResult?.entry.entered, color: "text-emerald-300" },
                    { label: "Waited", value: cycleResult?.entry.waited, color: "text-amber-300" },
                    { label: "Rejected", value: cycleResult?.entry.rejected, color: "text-rose-300" },
                    { label: "Fills", value: cycleResult?.fills, color: "text-emerald-300" },
                    { label: "Expired", value: cycleResult?.expired_orders, color: "text-paper/50" },
                    { label: "Held", value: cycleResult?.exit.held, color: "text-paper/70" },
                    { label: "Trailed", value: cycleResult?.exit.trailed, color: "text-sky-300" },
                    { label: "Partial Exit", value: cycleResult?.exit.partial_exits, color: "text-amber-300" },
                    { label: "Closed", value: cycleResult?.exit.full_exits, color: "text-emerald-300" },
                    { label: "Time-stopped", value: cycleResult?.exit.time_stops, color: "text-rose-300" },
                  ].map((stage) => (
                    <div key={stage.label} className="border border-white/10 rounded-lg px-2 py-1.5 bg-graphite/40">
                      <p className="font-mono text-[9px] text-paper/40 uppercase tracking-wide">{stage.label}</p>
                      <p className={`font-mono text-sm font-bold ${cycleBusy && stage.value == null ? "text-paper/20 animate-pulse" : stage.color}`}>
                        {cycleBusy && stage.value == null ? "…" : stage.value ?? 0}
                      </p>
                    </div>
                  ))}
                </div>
              )}
              {cycleBusy && (
                <div className="mt-2 h-1 w-full bg-white/5 rounded-full overflow-hidden">
                  <div className="h-full w-1/3 bg-emerald-500/60 animate-[pulse_1.2s_ease-in-out_infinite] rounded-full" />
                </div>
              )}
            </div>
          )}

          {/* Open positions */}
          <div className="border border-white/10 rounded-lg p-4 mb-4">
            <p className="font-mono text-xs text-paper/70 mb-2">Open Positions ({mode})</p>
            {positions.length === 0 ? (
              <p className="font-mono text-[11px] text-paper/40">No open positions.</p>
            ) : (
              <div className="space-y-1">
                {positions.map((p, i) => (
                  <div key={i} className="font-mono text-[11px] text-paper/70 grid grid-cols-6 gap-1">
                    <span className="font-bold">{p.symbol}</span>
                    <span>qty {p.qty_open}</span>
                    <span>entry ₹{p.avg_entry_price}</span>
                    <span>SL {p.current_stop ?? "—"}</span>
                    <span>TGT {p.current_target ?? "—"}</span>
                    <span className={p.unrealized_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}>
                      {p.unrealized_pnl >= 0 ? "+" : ""}₹{p.unrealized_pnl}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Recent orders */}
          <div className="border border-white/10 rounded-lg p-4 mb-4">
            <p className="font-mono text-xs text-paper/70 mb-2">Recent Orders ({mode})</p>
            {orders.length === 0 ? (
              <p className="font-mono text-[11px] text-paper/40">No orders yet.</p>
            ) : (
              <div className="space-y-1 max-h-48 overflow-y-auto">
                {orders.map((o, i) => (
                  <div key={i} className="font-mono text-[11px] text-paper/60 flex justify-between gap-2">
                    <span>{o.side} {o.symbol} x{o.qty} @ {o.limit_price ?? "mkt"}</span>
                    <span className="text-paper/40">{o.status}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Audit log */}
          <div className="border border-white/10 rounded-lg p-4">
            <div className="flex justify-between items-center mb-2">
              <p className="font-mono text-xs text-paper/70">Recent Activity</p>
              <button onClick={() => void loadAudit()} className="font-mono text-[11px] underline text-paper/50">
                Refresh
              </button>
            </div>
            <div className="space-y-1 max-h-64 overflow-y-auto">
              {auditRows.map((r, i) => (
                <div key={i} className="font-mono text-[11px] text-paper/60 flex justify-between gap-2">
                  <span>{r.action} {r.detail ? `— ${r.detail}` : ""}</span>
                  <span className="text-paper/30 whitespace-nowrap">{new Date(r.occurred_at).toLocaleTimeString()}</span>
                </div>
              ))}
              {auditRows.length === 0 && <p className="font-mono text-[11px] text-paper/40">No activity loaded yet — click Refresh.</p>}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
