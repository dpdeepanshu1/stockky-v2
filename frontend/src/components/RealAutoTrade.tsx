import { useCallback, useEffect, useState } from "react";
import {
  realTradeApi, getRealTradeApiUrl, setRealTradeApiUrl,
  getSessionToken, setSessionToken, type GateStatus, type AuditLogRow,
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

  const [auditRows, setAuditRows] = useState<AuditLogRow[]>([]);

  const loadStatus = useCallback(async (m: Mode) => {
    try {
      const s = await realTradeApi.gateStatus(m);
      setStatus(s);
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
        Phase 1: authentication, gate sequence, risk engine, and Dhan connection only.
        No automated entry/exit logic is wired yet — arming here does not place any order.
      </p>

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

      {!loggedIn ? (
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
          <div className="flex justify-end mb-2">
            <button onClick={() => void doLogout()} className="font-mono text-[11px] text-paper/50 underline">
              Log out
            </button>
          </div>

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

          {/* Dhan connect (REAL only) */}
          {mode === "REAL" && status && !status.dhan_connected && (
            <div className="border border-white/10 rounded-lg p-4 mb-4">
              <p className="font-mono text-xs text-paper/70 mb-3">Connect Dhan Account</p>
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
                  placeholder="Dhan Access Token (regenerate daily from web.dhan.co unless TOTP is enabled)"
                  value={dhanToken}
                  onChange={(e) => setDhanToken(e.target.value)}
                />
                <button
                  onClick={() => void doConnectDhan()}
                  disabled={loading || !dhanClientId || !dhanToken}
                  className="w-full px-4 py-2 rounded bg-sky-600/30 border border-sky-500/60 font-mono text-xs disabled:opacity-50"
                >
                  {loading ? "Connecting…" : "Connect Dhan"}
                </button>
              </div>
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
