// frontend/src/components/PositionStocksTab.tsx
//
// Step 8 (tracking doc §5 / STATUS.md) — dashboard for position-stocks-
// service: the separate 5m/15m/60m momentum scalping pipeline, its own
// container, own capital pool, own kill switch. Talks ONLY to
// positionStocksApi.ts (its own service URL/client) — never to api.ts or
// realTradeApi.ts, matching this service's whole isolation design.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  positionStocksApi, getPositionStocksApiUrl, setPositionStocksApiUrl,
  type ScalpStatus, type ScalpPositionRow, type ScalpCandidateRow, type ScalpLedgerState,
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
  return "text-signal-avoid"; // ERROR
}

export default function PositionStocksTab() {
  const [apiUrlInput, setApiUrlInput] = useState(getPositionStocksApiUrl());
  const [status, setStatus] = useState<ScalpStatus | null>(null);
  const [positions, setPositions] = useState<ScalpPositionRow[]>([]);
  const [candidates, setCandidates] = useState<ScalpCandidateRow[]>([]);
  const [ledger, setLedger] = useState<ScalpLedgerState | null>(null);
  const [windowFilter, setWindowFilter] = useState<Window | "all">("all");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmKill, setConfirmKill] = useState(false);

  const loadAll = useCallback(async () => {
    if (!getPositionStocksApiUrl()) return;
    try {
      const [s, p, c, l] = await Promise.all([
        positionStocksApi.status(),
        positionStocksApi.positions(),
        positionStocksApi.candidates(),
        positionStocksApi.ledger(),
      ]);
      setStatus(s); setPositions(p); setCandidates(c); setLedger(l);
      setError(null);
    } catch (e: any) {
      setError(e?.message || "Failed to reach position-stocks-service");
    }
  }, []);

  useEffect(() => {
    void loadAll();
    const t = setInterval(() => void loadAll(), 15_000);
    return () => clearInterval(t);
  }, [loadAll]);

  const saveApiUrl = () => { setPositionStocksApiUrl(apiUrlInput); void loadAll(); };

  const doAction = async (action: "arm" | "disarm" | "kill" | "sync" | "reconcile" | "service_enable" | "service_disable") => {
    setBusy(action); setError(null);
    try {
      if (action === "arm") await positionStocksApi.arm();
      else if (action === "disarm") await positionStocksApi.disarm();
      else if (action === "kill") { await positionStocksApi.kill(); setConfirmKill(false); }
      else if (action === "sync") await positionStocksApi.syncLedger();
      else if (action === "reconcile") await positionStocksApi.reconcile();
      else if (action === "service_enable") await positionStocksApi.serviceEnable();
      else if (action === "service_disable") await positionStocksApi.serviceDisable();
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

  // ── No URL configured ────────────────────────────────────────────────────
  if (!getPositionStocksApiUrl()) {
    return (
      <div className="p-4 max-w-lg mx-auto">
        <p className="font-display tabular-nums text-xs text-mist mb-1 uppercase tracking-widest">Position Stocks Service URL</p>
        <p className="font-display tabular-nums text-[11px] text-mist mb-3">Paste your position-stocks-service URL (separate container from real-trade-service, port 8006).</p>
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

  return (
    <div className="page-terminal space-y-4">
      <p className="dash-section-title">Position Stocks — 5m / 15m / 60m Scalp Pool</p>

      {error && (
        <div className="rounded-xl border border-signal-sell/40 bg-signal-sell/10 px-3 py-2 font-display tabular-nums text-xs text-signal-sell">
          {error}
        </div>
      )}

      {!status?.risk_confirmed && (
        <div className="rounded-xl border border-signal-hold/40 bg-signal-hold/10 px-3 py-2 font-display tabular-nums text-[11px] text-signal-hold">
          ⚠ RISK_PER_TRADE_PCT not yet confirmed — running on a placeholder value ({status?.risk_per_trade_pct ?? "—"}%). Set RISK_PER_TRADE_PCT_CONFIRMED=true in the service env once you've chosen the real number.
        </div>
      )}

      {/* ── Top control strip ── */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        <div className="bg-graphite border border-slate rounded-2xl p-3">
          <p className="text-[9px] text-mist uppercase tracking-widest mb-1">Armed</p>
          <p className={`font-display tabular-nums font-bold text-sm ${status?.armed ? "text-signal-buy" : "text-signal-avoid"}`}>
            {status?.armed ? "ARMED" : "DISARMED"}
          </p>
        </div>
        <div className="bg-graphite border border-slate rounded-2xl p-3">
          <p className="text-[9px] text-mist uppercase tracking-widest mb-1">Market</p>
          <p className={`font-display tabular-nums font-bold text-sm ${status?.market_open ? "text-signal-buy" : "text-mist"}`}>
            {status?.market_open ? "OPEN" : "CLOSED"}
          </p>
        </div>
        <div className="bg-graphite border border-slate rounded-2xl p-3">
          <p className="text-[9px] text-mist uppercase tracking-widest mb-1">WS Feed</p>
          <p className={`font-display tabular-nums font-bold text-sm ${status?.ws?.connected ? "text-signal-buy" : "text-signal-sell"}`}>
            {status?.ws?.connected ? "LIVE" : "DOWN"}
          </p>
        </div>
        <div className="bg-graphite border border-slate rounded-2xl p-3">
          <p className="text-[9px] text-mist uppercase tracking-widest mb-1">Kill Switch</p>
          <p className={`font-display tabular-nums font-bold text-sm ${status?.daily_loss_kill_switch ? "text-signal-sell" : "text-signal-buy"}`}>
            {status?.daily_loss_kill_switch ? "TRIPPED" : "clear"}
          </p>
        </div>
        <div className="bg-graphite border border-slate rounded-2xl p-3">
          <p className="text-[9px] text-mist uppercase tracking-widest mb-1">Module</p>
          <p className={`font-display tabular-nums font-bold text-sm ${status?.service_enabled ? "text-signal-buy" : "text-signal-avoid"}`}>
            {status?.service_enabled ? "ENABLED" : "PAUSED"}
          </p>
        </div>
      </div>

      {status && !status.service_enabled && (
        <div className="rounded-xl border border-signal-avoid/40 bg-signal-avoid/10 px-3 py-2 font-display tabular-nums text-[11px] text-signal-avoid">
          Module paused — screening and new entries are stopped. Exit reconciliation and the 3:00 PM EOD square-off keep running for any open positions.
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        <button
          disabled={busy !== null || status?.service_enabled}
          onClick={() => doAction("service_enable")}
          className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy disabled:opacity-40"
        >{busy === "service_enable" ? "Enabling…" : "Enable Module"}</button>
        <button
          disabled={busy !== null || !status?.service_enabled}
          onClick={() => doAction("service_disable")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-paper disabled:opacity-40"
        >{busy === "service_disable" ? "Pausing…" : "Pause Module"}</button>
        <button
          disabled={busy !== null || status?.armed}
          onClick={() => doAction("arm")}
          className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy disabled:opacity-40"
        >{busy === "arm" ? "Arming…" : "Arm"}</button>
        <button
          disabled={busy !== null || !status?.armed}
          onClick={() => doAction("disarm")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-paper disabled:opacity-40"
        >{busy === "disarm" ? "Disarming…" : "Disarm"}</button>
        <button
          disabled={busy !== null}
          onClick={() => doAction("sync")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist disabled:opacity-40"
        >{busy === "sync" ? "Syncing…" : "Sync Capital from Dhan"}</button>
        <button
          disabled={busy !== null}
          onClick={() => doAction("reconcile")}
          className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist disabled:opacity-40"
        >{busy === "reconcile" ? "Checking…" : "Check Exits Now"}</button>

        {!confirmKill ? (
          <button
            disabled={busy !== null}
            onClick={() => setConfirmKill(true)}
            className="px-4 py-2 rounded-xl bg-signal-sell/20 border border-signal-sell/40 font-display tabular-nums text-xs text-signal-sell disabled:opacity-40 ml-auto"
          >Kill Switch</button>
        ) : (
          <div className="flex items-center gap-2 ml-auto">
            <span className="font-display tabular-nums text-[11px] text-signal-sell">Disarm + trip daily kill switch?</span>
            <button onClick={() => doAction("kill")} className="px-3 py-2 rounded-xl bg-signal-sell/30 border border-signal-sell font-display tabular-nums text-xs text-signal-sell">Confirm</button>
            <button onClick={() => setConfirmKill(false)} className="px-3 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist">Cancel</button>
          </div>
        )}
      </div>

      {/* ── Capital ledger ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-2">Scalp Capital Pool (50% split, software-enforced)</p>
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
        <p className="font-display tabular-nums text-[9px] text-mist mt-2">Last synced from Dhan: {fmtDateTimeIst(ledger?.last_synced_from_broker_at)}</p>
      </div>

      {/* ── Live screener ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-2">
          <p className="dash-section-title">Live Screener</p>
          <div className="flex gap-1">
            {(["all", "1m", "5m", "15m", "60m"] as const).map(w => (
              <button
                key={w}
                onClick={() => setWindowFilter(w)}
                className={`px-2 py-1 rounded-lg font-display tabular-nums text-[10px] uppercase border ${
                  windowFilter === w ? "bg-signal-prepare/20 border-signal-prepare/40 text-signal-prepare" : "border-slate text-mist"
                }`}
              >{w}</button>
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
        <p className="dash-section-title mb-2">Open Positions ({openPositions.length}/5)</p>
        {openPositions.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">No open scalp positions.</p>
        ) : (
          <div className="space-y-2">
            {openPositions.map(p => (
              <PositionRow key={p.id} p={p} />
            ))}
          </div>
        )}
      </div>

      {/* ── Closed today ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-2">Closed Today ({closedToday.length})</p>
        {closedToday.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">No positions closed yet today.</p>
        ) : (
          <div className="space-y-2">
            {closedToday.map(p => (
              <PositionRow key={p.id} p={p} />
            ))}
          </div>
        )}
      </div>

      {/* ── Settings (change URL) ── */}
      <details className="bg-graphite border border-slate rounded-2xl p-4">
        <summary className="font-display tabular-nums text-xs text-mist cursor-pointer">Settings</summary>
        <div className="flex gap-2 mt-3">
          <input
            className="flex-1 bg-ink border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none focus:border-slate"
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
  if (rows.length === 0) {
    return <p className="font-display tabular-nums text-[11px] text-mist">No candidates.</p>;
  }
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
  return (
    <div className="border border-slate rounded-xl p-3">
      <div className="flex items-center justify-between mb-1">
        <span className="font-display tabular-nums font-bold text-sm text-paper">{p.symbol}</span>
        <span className={`font-display tabular-nums text-[10px] uppercase ${statusColor(p.status)}`}>{p.status}</span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px]">
        <div><span className="text-mist">Entry </span><span className="tabular-nums text-paper">₹{p.entry_price.toFixed(2)}</span></div>
        <div><span className="text-mist">Qty </span><span className="tabular-nums text-paper">{p.quantity}</span></div>
        <div><span className="text-mist">Target </span><span className="tabular-nums text-signal-buy">₹{p.target_price.toFixed(2)} ({p.adaptive_target_pct.toFixed(1)}%)</span></div>
        <div><span className="text-mist">Stop </span><span className="tabular-nums text-signal-sell">₹{p.stop_price.toFixed(2)} ({p.adaptive_stop_pct.toFixed(1)}%)</span></div>
      </div>
      {p.realized_pnl != null && (
        <p className={`font-display tabular-nums text-xs mt-1 ${p.realized_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
          P&L {fmtInr(p.realized_pnl)} ({(p.realized_pnl_pct ?? 0).toFixed(2)}%)
        </p>
      )}
      <p className="font-display tabular-nums text-[9px] text-mist mt-1">
        {p.window_source} window · opened {fmtDateTimeIst(p.opened_at)}
        {p.closed_at ? ` · closed ${fmtDateTimeIst(p.closed_at)}` : ""}
        {p.is_first_live_order ? " · FIRST LIVE ORDER" : ""}
      </p>
    </div>
  );
}
