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
  type ScalpTradeHistory, type DhanLiveOrders, type ScalpCycleResult, type DhanAccountStatus,
  type ScalpPipelineStatus,
  type ScalpCandidateLogRow,
  type ScalpIntradayRestrictedRow,
} from "../positionStocksApi";
// Cross-service, read-only: real-trade-service's own account bookkeeping,
// used only to populate the "Real Trade Service" side of CapitalSplitCard
// below (see that component's docstring for why this is a best-effort,
// non-blocking call — this dashboard must keep working even if real-
// trade-service's URL isn't configured in this browser).
import { realTradeApi, getRealTradeApiUrl } from "../realTradeApi";
import CapitalSplitCard from "./CapitalSplitCard";

type Window = "1m" | "5m" | "15m" | "60m";

function fmtInr(n: number | null | undefined, decimals = 0): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1_00_00_000) return `${sign}₹${(abs / 1_00_00_000).toFixed(1)}Cr`;
  if (abs >= 1_00_000) return `${sign}₹${(abs / 1_00_000).toFixed(1)}L`;
  return `${sign}₹${abs.toLocaleString("en-IN", { maximumFractionDigits: decimals })}`;
}

function fmtHms(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// Same safe-extraction helper as RealAutoTrade.tsx — Dhan's fund object has
// inconsistent casing/typos (e.g. "availabelBalance") across SDK versions.
function pickNum(obj: any, ...keys: string[]): number | null {
  for (const k of keys) {
    const v = obj?.[k];
    if (typeof v === "number") return v;
    if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))) return Number(v);
  }
  return null;
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
  // BUG FIX (this session): STAGNATION_EXIT is now a real status (was
  // silently stored as "MANUAL_EXIT" before, see backend) — give it its
  // own color so it's visually distinguishable from a true manual exit,
  // not just a hold-orange lookalike.
  if (status === "STAGNATION_EXIT") return "text-signal-prepare";
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

// Arming sequence step — same visual language as Real Automatic Trade's
// gate checklist, so the two tabs read as one system rather than two
// differently-styled dashboards.
function GateStep({ n, label, done }: { n: number; label: string; done: boolean }) {
  return (
    <div className={`flex items-center gap-2 px-3 py-2 rounded-xl border transition-colors ${
      done ? "bg-signal-buy/5 border-signal-buy/20" : "bg-graphite border-slate"
    }`}>
      <div className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold flex-shrink-0 ${
        done ? "bg-signal-buy/20 text-signal-buy" : "bg-ink text-mist"
      }`}>{done ? "✓" : n}</div>
      <span className={`text-[11px] font-display tabular-nums ${done ? "text-signal-buy" : "text-mist"}`}>{label}</span>
    </div>
  );
}

// AUDIT ADD (this session): one stage of the Run Cycle / Auto-Pilot
// pipeline dashboard — WS Feed → Scan → Liquidity Gate → Rank → Quality
// Gate → Entry. `metric` is the live number that stage is currently
// producing (subscribed symbol count, candidates surfaced, etc.); `detail`
// lines are the static configured rule(s) that stage enforces, pulled from
// status.pipeline_config so they can never drift from what the backend is
// actually doing.
function PipelineStage({
  n, title, metric, metricLabel, detail, active,
}: {
  n: number; title: string; metric?: string | number | null; metricLabel?: string;
  detail: string[]; active: boolean;
}) {
  return (
    <div className={`rounded-xl border p-3 flex-1 min-w-[150px] ${active ? "bg-signal-buy/5 border-signal-buy/20" : "bg-ink border-slate"}`}>
      <div className="flex items-center gap-1.5 mb-1.5">
        <div className={`w-4 h-4 rounded-full flex items-center justify-center text-[9px] font-bold flex-shrink-0 ${
          active ? "bg-signal-buy/20 text-signal-buy" : "bg-graphite text-mist"
        }`}>{n}</div>
        <p className="font-display tabular-nums text-[10px] uppercase tracking-widest text-mist">{title}</p>
      </div>
      {metric != null && (
        <p className={`font-display tabular-nums text-lg font-bold ${active ? "text-signal-buy" : "text-paper"}`}>
          {metric}
          {metricLabel && <span className="text-[9px] text-mist ml-1 font-normal">{metricLabel}</span>}
        </p>
      )}
      <div className="mt-1 space-y-0.5">
        {detail.map((d, i) => (
          <p key={i} className="font-display tabular-nums text-[9px] text-mist leading-tight">{d}</p>
        ))}
      </div>
    </div>
  );
}

function PipelineArrow() {
  return <div className="hidden sm:flex items-center text-mist text-lg px-1 select-none">→</div>;
}

function fmtMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

// AUDIT ADD (this session): renders the full stage-by-stage breakdown a
// Run Cycle (manual OR the last automatic tick) actually did — what each
// stage took, and which stock(s) it looked at / picked. Requested
// specifically so "Run Cycle Now" shows more than a one-line tally.
function RunCycleResultPanel({ result }: { result: ScalpCycleResult }) {
  return (
    <div className="bg-graphite border border-slate rounded-2xl p-4">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <p className="dash-section-title">
          Run Cycle Result — {result.trigger === "MANUAL" ? "manual click" : "automatic tick"}
        </p>
        <div className="flex items-center gap-3 font-display tabular-nums text-[10px]">
          <span className="text-paper">{fmtDateTimeIst(result.started_at)}</span>
          <span className="text-signal-prepare">total {fmtMs(result.total_duration_ms)}</span>
        </div>
      </div>

      {/* ── Headline: what actually happened ── */}
      <div className="flex flex-wrap gap-2 mb-3">
        <span className="px-2 py-1 rounded-lg bg-ink border border-slate font-display tabular-nums text-[10px] text-mist">
          {result.candidates_seen} candidate(s) seen
        </span>
        {result.entered_symbol && (
          <span className="px-2 py-1 rounded-lg bg-signal-buy/15 border border-signal-buy/40 font-display tabular-nums text-[10px] text-signal-buy">
            ENTERED {result.entered_symbol}
          </span>
        )}
        {result.eod_fired && (
          <span className="px-2 py-1 rounded-lg bg-signal-hold/15 border border-signal-hold/40 font-display tabular-nums text-[10px] text-signal-hold">
            EOD squareoff fired
          </span>
        )}
        {result.skipped_reason && (
          <span className="px-2 py-1 rounded-lg bg-signal-avoid/15 border border-signal-avoid/40 font-display tabular-nums text-[10px] text-signal-avoid">
            skipped: {result.skipped_reason}
          </span>
        )}
      </div>

      {/* ── Stage-by-stage timing ── */}
      <div className="space-y-1.5">
        {result.stages.map((stage, i) => (
          <div key={i} className="rounded-xl border border-slate bg-ink p-2.5">
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <div className="flex items-center gap-2">
                <span className="w-4 h-4 rounded-full bg-graphite text-mist flex items-center justify-center text-[9px] font-bold flex-shrink-0">{i + 1}</span>
                <span className="font-display tabular-nums text-[11px] text-paper">{stage.label}</span>
              </div>
              <span className="font-display tabular-nums text-[10px] text-signal-prepare">{fmtMs(stage.duration_ms)}</span>
            </div>
            {stage.detail && (
              <p className="font-display tabular-nums text-[10px] text-mist mt-1 leading-relaxed">{stage.detail}</p>
            )}

            {/* Scan stage: top candidates surfaced, by symbol */}
            {stage.candidates && stage.candidates.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {stage.candidates.map((c, j) => (
                  <span key={j} className="px-1.5 py-0.5 rounded-md bg-graphite border border-slate font-display tabular-nums text-[9px] text-paper">
                    {c.symbol} <span className="text-mist">{c.window}</span> {c.pct_change >= 0 ? "+" : ""}{c.pct_change}%
                  </span>
                ))}
              </div>
            )}

            {/* Quality gate stage: pass/fail per symbol checked */}
            {stage.checked && stage.checked.length > 0 && (
              <div className="mt-2 space-y-1">
                {stage.checked.map((c, j) => (
                  <div key={j} className="flex items-center gap-1.5 flex-wrap">
                    <span className={`px-1.5 py-0.5 rounded-md border font-display tabular-nums text-[9px] ${
                      c.passed ? "bg-signal-buy/15 border-signal-buy/40 text-signal-buy" : "bg-signal-avoid/15 border-signal-avoid/40 text-signal-avoid"
                    }`}>
                      {c.passed ? "✓" : "✗"} {c.symbol}
                    </span>
                    {c.reason && <span className="font-display tabular-nums text-[9px] text-mist">{c.reason}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ADDED (session48 — "no live process shows and no stock name shows"):
// live counterpart to RunCycleResultPanel above. That panel only ever
// rendered a *completed* cycle's result; this one shows what the
// background AUTO loop (or an in-flight manual click) is doing right now,
// fed by the new GET /pipeline/status poll. Most ticks finish in
// single-digit milliseconds with nothing to catch mid-flight — the "Idle"
// state below is the common, correct, non-broken case — but a tick that
// reaches Quality Gate makes real outbound HTTP calls and can run for
// real seconds, which is exactly when this becomes useful. Unlike
// real-trade-service's equivalent, quality-gate checks here run
// concurrently (asyncio.gather, see main.py's _run_cycle) rather than one
// symbol at a time, so there's no single "currently checking symbol X of
// N" to show — only the most recently completed stage and, once scan has
// run, which stock(s) it surfaced.
function LivePipelineStatus({ live }: { live: ScalpPipelineStatus | null }) {
  if (!live) {
    return (
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-2">Live cycle status</p>
        <p className="font-display tabular-nums text-[11px] text-mist">Loading…</p>
      </div>
    );
  }
  return (
    <div className="bg-graphite border border-slate rounded-2xl p-4 space-y-2">
      <div className="flex items-center justify-between">
        <p className="dash-section-title">Live cycle status</p>
        {live.running ? (
          <span className="font-display tabular-nums text-[10px] text-signal-buy flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-signal-buy animate-pulse" />
            {live.trigger === "AUTO" ? "Auto-Pilot cycle running" : "Cycle running"}
          </span>
        ) : (
          <span className="font-display tabular-nums text-[10px] text-mist">Idle — no cycle running</span>
        )}
      </div>
      {live.running && (
        <div className="bg-ink border border-slate rounded-xl p-3 space-y-2">
          <p className="font-display tabular-nums text-xs text-paper">{live.stage_label || live.stage}</p>
          {live.candidates.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {live.candidates.map((c, j) => (
                <span key={j} className="px-1.5 py-0.5 rounded-md bg-graphite border border-slate font-display tabular-nums text-[9px] text-paper">
                  {c.symbol} <span className="text-mist">{c.window}</span> {c.pct_change >= 0 ? "+" : ""}{c.pct_change}%
                </span>
              ))}
            </div>
          )}
        </div>
      )}
      {/* Show last completed cycle summary even when idle */}
      {!live.running && live.last_cycle && (
        <div className="bg-ink border border-slate rounded-xl p-2.5 space-y-1.5">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-display tabular-nums text-[10px] text-mist">
              Last cycle — {live.last_cycle.trigger} at {fmtDateTimeIst(live.last_cycle.started_at)}
            </span>
            <span className="font-display tabular-nums text-[10px] text-signal-prepare">
              {fmtMs(live.last_cycle.total_duration_ms)}
            </span>
            {live.last_cycle.candidates_seen > 0 && (
              <span className="font-display tabular-nums text-[10px] text-paper">
                {live.last_cycle.candidates_seen} candidate(s) seen
              </span>
            )}
            {live.last_cycle.entered_symbol && (
              <span className="px-1.5 py-0.5 rounded-md bg-signal-buy/15 border border-signal-buy/40 font-display tabular-nums text-[9px] text-signal-buy">
                ENTERED {live.last_cycle.entered_symbol}
              </span>
            )}
            {live.last_cycle.skipped_reason && (
              <span className="font-display tabular-nums text-[9px] text-mist">{live.last_cycle.skipped_reason}</span>
            )}
          </div>
          {/* Show the scan stage candidates from last_cycle if any */}
          {(() => {
            const scanStage = live.last_cycle.stages?.find((s: any) => s.name === "scan");
            if (!scanStage?.candidates?.length) return null;
            return (
              <div className="flex flex-wrap gap-1">
                {scanStage.candidates.map((c: any, j: number) => (
                  <span key={j} className="px-1.5 py-0.5 rounded-md bg-graphite border border-slate font-display tabular-nums text-[9px] text-paper">
                    {c.symbol} <span className="text-mist">{c.window}</span> {c.pct_change >= 0 ? "+" : ""}{c.pct_change}%
                  </span>
                ))}
              </div>
            );
          })()}
        </div>
      )}
    </div>
  );
}

export default function PositionStocksTab() {
  const [apiUrlInput, setApiUrlInput] = useState(getPositionStocksApiUrl());
  const [status, setStatus] = useState<ScalpStatus | null>(null);
  const [positions, setPositions] = useState<ScalpPositionRow[]>([]);
  const [candidates, setCandidates] = useState<ScalpCandidateRow[]>([]);
  const [ledger, setLedger] = useState<ScalpLedgerState | null>(null);
  const [tradeHistory, setTradeHistory] = useState<ScalpTradeHistory | null>(null);
  // this session — Trade History Today/Last-3-days subtabs the user asked
  // for. Defaults to "3d" since that's the full depth the backend retains
  // anyway (orders/reconcile.py::run_retention_cleanup(),
  // config.TRADE_HISTORY_RETENTION_DAYS).
  const [tradeHistoryRange, setTradeHistoryRange] = useState<"today" | "3d">("3d");
  const [dhanLive, setDhanLive] = useState<DhanLiveOrders | null>(null);
  const [dhanLiveError, setDhanLiveError] = useState<string | null>(null);
  const [lastCycleResult, setLastCycleResult] = useState<ScalpCycleResult | null>(null);
  // ADDED (session48 — "no live process shows and no stock name shows"):
  // polled snapshot of the current/last cycle from the new GET
  // /pipeline/status route (see positionStocksApi.ts + pipeline_status.py).
  const [pipelineLive, setPipelineLive] = useState<ScalpPipelineStatus | null>(null);
  const [windowFilter, setWindowFilter] = useState<Window | "all">("all");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmKill, setConfirmKill] = useState(false);
  // AUDIT FIX (this session): backend POST /ledger/reset-daily existed with
  // no frontend wiring at all — see positionStocksApi.ts's resetLedgerDaily
  // comment. Same confirm-guard pattern as confirmKill, since this clears
  // today's realized P&L + kill switch on demand (an emergency/manual
  // override on top of the automatic midnight reset).
  const [confirmResetDaily, setConfirmResetDaily] = useState(false);
  // ADDED (this session — manual Buy control on the Positions tab)
  const [manualBuySymbol, setManualBuySymbol] = useState("");
  const [manualBuyQty, setManualBuyQty] = useState("");
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const [dhanAccount, setDhanAccount] = useState<DhanAccountStatus | null>(null);
  const [nowTick, setNowTick] = useState(() => Date.now());
  const [candidateLog, setCandidateLog] = useState<ScalpCandidateLogRow[]>([]);
  const [candidateLogError, setCandidateLogError] = useState<string | null>(null);
  const [restrictedSymbols, setRestrictedSymbols] = useState<ScalpIntradayRestrictedRow[]>([]);
  const [restrictedSymbolsError, setRestrictedSymbolsError] = useState<string | null>(null);
  // Real Trade Service's own account figures, fetched cross-service purely
  // to populate CapitalSplitCard (see that component's docstring) — this
  // dashboard's own data (status/ledger/dhanAccount above) never depends on it.
  const [rtCashAvailable, setRtCashAvailable] = useState<number | null>(null);
  const [rtAccountError, setRtAccountError] = useState<string | null>(null);

  // Sub-tabs — the page used to be one long scroll of every section at
  // once; grouped into tabs so each screen only shows what's relevant to
  // that task (arming/health/risk vs. screener vs. positions vs. history).
  type SubTab = "overview" | "pipeline" | "screener" | "positions" | "history" | "dhanorders" | "charges" | "settings";
  const [subTab, setSubTab] = useState<SubTab>("overview");

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
      void loadDhanAccount();
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
        positionStocksApi.candidates().then(r => r.candidates),
        positionStocksApi.ledger().catch(() => null),   // don't let /ledger 500 kill the whole poll
        positionStocksApi.tradeHistory(200, tradeHistoryRange),
      ]);
      setStatus(s); setPositions(p); setCandidates(c);
      if (l) setLedger(l);
      setTradeHistory(h);
      setError(null);
      setLastRefreshed(new Date());
    } catch (e: any) {
      setError(e?.message || "Failed to reach position-stocks-service");
    }
  }, [tradeHistoryRange]);

  // Candidate audit log (backend session 12: GET /candidates/log) — why a
  // candidate was entered or skipped, including quality-gate scores. Pure
  // DB read, cheap enough to poll on the same cadence as everything else.
  const loadCandidateLog = useCallback(async () => {
    if (!getPositionStocksApiUrl()) return;
    try {
      const rows = await positionStocksApi.candidatesLog(100);
      setCandidateLog(rows); setCandidateLogError(null);
    } catch (e: any) {
      setCandidateLogError(e?.message || "Failed to load candidate log");
    }
  }, []);

  // Learned intraday-restricted symbol list (GET /candidates/restricted) —
  // symbols Dhan has actually rejected as "not allowed to trade in
  // Intraday" (T2T/ASM/GSM surveillance), recorded by orders/eod_squareoff.py
  // and orders/entry.py, then filtered out of every future cycle's
  // candidates before capital or a Dhan call is committed to them. No
  // static list — this is purely a record of what's already been learned.
  const loadRestrictedSymbols = useCallback(async () => {
    if (!getPositionStocksApiUrl()) return;
    try {
      const rows = await positionStocksApi.candidatesRestricted();
      setRestrictedSymbols(rows); setRestrictedSymbolsError(null);
    } catch (e: any) {
      setRestrictedSymbolsError(e?.message || "Failed to load restricted symbols");
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

  // Dhan Account card (client ID, token countdown, live funds) — same shared
  // account real-trade-service's Real Automatic Trade tab already shows;
  // requires admin auth (it's a live Dhan API call, not just a DB read).
  const loadDhanAccount = useCallback(async () => {
    if (!getSessionToken()) return;
    try {
      const d = await positionStocksApi.dhanAccount();
      setDhanAccount(d);
    } catch {
      // Non-fatal — the rest of the dashboard doesn't depend on this card,
      // and a 401 here is already handled by the shared session-expired hook.
    }
  }, []);

  // Real Trade Service's account figures, for the Capital Split card below.
  // No admin login required — gateStatus() is a public read (see
  // realTradeApi.ts) — and no auth token is shared between the two
  // services anyway, so this only ever needs real-trade-service's own URL
  // to be configured somewhere in this browser (Real Automatic Trade tab's
  // own Settings). Best-effort: if that URL isn't set, or the service is
  // unreachable, the card just shows the computed reference split instead.
  const loadRealTradeAccount = useCallback(async () => {
    if (!getRealTradeApiUrl()) {
      setRtAccountError("Real Trade Service URL not configured in this browser.");
      return;
    }
    try {
      const s = await realTradeApi.gateStatus("REAL");
      setRtCashAvailable(s.account.cash_available);
      setRtAccountError(null);
    } catch (e: any) {
      setRtCashAvailable(null);
      setRtAccountError(e?.message || "Failed to reach Real Trade Service");
    }
  }, []);

  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    void loadAll();
    void loadDhanLive();
    void loadDhanAccount();
    void loadCandidateLog();
    void loadRestrictedSymbols();
    void loadRealTradeAccount();
    const t = setInterval(() => void loadAll(), 15_000);
    const td = setInterval(() => void loadDhanLive(), 30_000);
    const ta = setInterval(() => void loadDhanAccount(), 30_000);
    const tc = setInterval(() => void loadCandidateLog(), 15_000);
    const tir = setInterval(() => void loadRestrictedSymbols(), 30_000);
    const tr = setInterval(() => void loadRealTradeAccount(), 30_000);
    return () => { clearInterval(t); clearInterval(td); clearInterval(ta); clearInterval(tc); clearInterval(tir); clearInterval(tr); };
  }, [loadAll, loadDhanLive, loadDhanAccount, loadCandidateLog, loadRestrictedSymbols, loadRealTradeAccount]);

  // ADDED (session48 — "no live process shows and no stock name shows"):
  // polls GET /pipeline/status every 2s while the Pipeline subtab is open,
  // same cadence/pattern as real-trade-service's own live pipeline poll.
  // Stops the moment the tab isn't open, same as every other tab-scoped
  // poll in this file.
  useEffect(() => {
    if (subTab !== "pipeline" || !getPositionStocksApiUrl()) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const p = await positionStocksApi.pipelineStatus();
        if (!cancelled) setPipelineLive(p);
      } catch {
        // best-effort — a failed poll just leaves the last known status showing
      }
    };
    void poll();
    const id = setInterval(poll, 2000);
    return () => { cancelled = true; clearInterval(id); };
  }, [subTab]);

  const saveApiUrl = () => { setPositionStocksApiUrl(apiUrlInput); void loadAll(); };

  const doAction = async (action: string) => {
    setBusy(action); setError(null);
    try {
      if (action === "arm") await positionStocksApi.arm();
      else if (action === "disarm") await positionStocksApi.disarm();
      else if (action === "kill") { await positionStocksApi.kill(); setConfirmKill(false); }
      else if (action === "sync") await positionStocksApi.syncLedger();
      else if (action === "reset_daily") { await positionStocksApi.resetLedgerDaily(); setConfirmResetDaily(false); }
      else if (action === "reconcile") await positionStocksApi.reconcile();
      else if (action === "service_enable") await positionStocksApi.serviceEnable();
      else if (action === "service_disable") await positionStocksApi.serviceDisable();
      else if (action === "autopilot_enable") await positionStocksApi.autopilotEnable();
      else if (action === "autopilot_disable") await positionStocksApi.autopilotDisable();
      else if (action === "stagnation_exit_enable") await positionStocksApi.stagnationExitEnable();
      else if (action === "stagnation_exit_disable") await positionStocksApi.stagnationExitDisable();
      else if (action === "breakeven_stop_enable") await positionStocksApi.breakevenStopEnable();
      else if (action === "breakeven_stop_disable") await positionStocksApi.breakevenStopDisable();
      else if (action === "run_cycle") { const r = await positionStocksApi.runCycle(); setLastCycleResult(r); }
      await loadAll();
    } catch (e: any) {
      setError(e?.message || `${action} failed`);
    } finally { setBusy(null); }
  };

  // ADDED (this session — manual Buy / Exit controls on the Positions tab):
  // separate from doAction() above since these two take arguments (symbol,
  // optional quantity; a position id) rather than being fixed actions.
  const doManualBuy = async () => {
    const symbol = manualBuySymbol.trim().toUpperCase();
    if (!symbol) { setError("Enter a symbol to buy."); return; }
    const qty = manualBuyQty.trim() ? parseInt(manualBuyQty.trim(), 10) : undefined;
    if (manualBuyQty.trim() && (!qty || qty <= 0)) { setError("Quantity must be a positive whole number."); return; }
    setBusy("manual_buy"); setError(null);
    try {
      const r = await positionStocksApi.manualBuy(symbol, qty);
      setManualBuySymbol(""); setManualBuyQty("");
      await loadAll();
      setError(null);
      // eslint-disable-next-line no-console
      console.info(`Manual BUY placed: ${r.symbol} x${r.quantity} @ ₹${r.entry_price}`);
    } catch (e: any) {
      setError(e?.message || "Manual buy failed");
    } finally { setBusy(null); }
  };

  const doClosePosition = async (id: number, symbol: string) => {
    if (!window.confirm(`Exit ${symbol} right now at market price? This bypasses its own target/stop and cannot be undone.`)) return;
    setBusy(`close_${id}`); setError(null);
    try {
      await positionStocksApi.closePosition(id);
      await loadAll();
    } catch (e: any) {
      setError(e?.message || "Manual exit failed");
    } finally { setBusy(null); }
  };

  const dhanTokenSecondsRemaining = dhanAccount?.token_expires_at
    ? Math.max(0, Math.floor((new Date(dhanAccount.token_expires_at).getTime() - nowTick) / 1000))
    : null;

  // BUG FIX (this session): was `p.status === "OPEN"` only, so an
  // EXIT_LEGS_REJECTED position — real shares still held at the broker,
  // no working target/stop leg — was excluded from Open Positions, the
  // Positions tab count, and the open-slots-used metric, AND fell through
  // into closedToday below as if it were a finished trade. Backend's own
  // capacity gate (orders/entry.py::_count_open_positions) already treats
  // OPEN + EXIT_LEGS_REJECTED as occupied slots; the frontend now matches
  // that instead of showing live risk as if it were closed and free.
  const openPositions = useMemo(
    () => positions.filter(p => p.status === "OPEN" || p.status === "EXIT_LEGS_REJECTED"),
    [positions]
  );
  // AUDIT FIX (prior session): this used to be `positions.filter(p => p.status
  // !== "OPEN")` with zero date filtering — GET /positions returns the last
  // 50 rows ordered by opened_at desc, so on a quiet day (or after a fresh
  // deploy with few trades since) this section's "Closed Today (N)" header
  // and its "No positions closed yet today" empty state could both show
  // positions that actually closed on an earlier calendar day, mislabeled
  // as today's. Now actually filters on the position's own closed_at (IST
  // calendar date), falling back to opened_at only in the arguably-
  // impossible case a non-OPEN row has no closed_at.
  const todayIst = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
  const closedToday = useMemo(
    () => positions.filter(p => {
      // BUG FIX (this session): exclude EXIT_LEGS_REJECTED here too — it's
      // still live exposure (see openPositions above), not a closed trade.
      if (p.status === "OPEN" || p.status === "EXIT_LEGS_REJECTED") return false;
      const ts = p.closed_at ?? p.opened_at;
      if (!ts) return false;
      return new Date(ts).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" }) === todayIst;
    }),
    [positions, todayIst]
  );
  const filteredCandidates = useMemo(
    () => windowFilter === "all" ? candidates : candidates.filter(c => c.window === windowFilter),
    [candidates, windowFilter]
  );
  const grouped = useMemo(() => {
    const g: Record<Window, ScalpCandidateRow[]> = { "1m": [], "5m": [], "15m": [], "60m": [] };
    for (const c of filteredCandidates) g[c.window]?.push(c);
    return g;
  }, [filteredCandidates]);
  // BUG FIX (this session — "window tabs / refresh button don't do
  // anything on the Screener tab"): the 1m/5m/15m/60m/ALL buttons above
  // only ever filtered the "Live Screener" section (grouped/filteredCandidates,
  // which reads `candidates` from GET /candidates — empty whenever nothing
  // has cleared the quality gate recently, as in the reported screenshots).
  // The "Candidate Log — Why Entered / Skipped" table right below it always
  // rendered the full unfiltered `candidateLog` (GET /candidates/log)
  // regardless of which window tab was selected, so with the top section
  // empty, that table was the only visible content and it never changed no
  // matter which tab was clicked or how many times Refresh was pressed —
  // exactly the "window not switching" / "refresh not working" symptom.
  const filteredCandidateLog = useMemo(
    () => windowFilter === "all" ? candidateLog : candidateLog.filter(c => c.window_source === windowFilter),
    [candidateLog, windowFilter]
  );
  // AUDIT FIX (session62, issue #5 — "quality gate filtering real movers,
  // needs admin visibility into what's being dropped"): isolates just the
  // quality-gate rejections out of the mixed candidateLog (which also logs
  // SERVICE_NOT_ARMED/INSUFFICIENT_CAPITAL/MAX_POSITIONS/etc skips) so it's
  // obvious at a glance how many otherwise-real movers the fundamental/
  // technical/market-cap floor is dropping, and by how much they missed it.
  const qualityGateDropped = useMemo(
    () => candidateLog.filter(c => c.decision === "SKIPPED" && c.reason?.startsWith("QUALITY_GATE:")),
    [candidateLog]
  );

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
        <p className="dash-section-title">Position Stocks — 1m / 5m / 15m / 60m Scalp Pool</p>
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

      {/* ── Sub-tab nav ── */}
      <div className="flex flex-wrap gap-1 border-b border-slate pb-2">
        {([
          { id: "overview", label: "Overview" },
          { id: "pipeline", label: "Pipeline" },
          { id: "screener", label: "Screener" },
          { id: "positions", label: `Positions (${openPositions.length})` },
          { id: "history", label: "Trade History" },
          { id: "dhanorders", label: "Dhan Live Orders" },
          { id: "charges", label: "Charges" },
          { id: "settings", label: "Settings" },
        ] as { id: SubTab; label: string }[]).map(t => (
          <button
            key={t.id}
            onClick={() => setSubTab(t.id)}
            className={`px-3 py-1.5 rounded-lg font-display tabular-nums text-[11px] uppercase tracking-wide border transition-colors ${
              subTab === t.id
                ? "bg-signal-prepare/20 border-signal-prepare/40 text-signal-prepare"
                : "border-transparent text-mist hover:text-paper"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {subTab === "overview" && (
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
          <GateStep n={1} label="Admin authenticated" done={loggedIn} />
          <GateStep n={2} label="Dhan connected" done={!!dhanAccount?.connected} />
          <GateStep n={3} label="Risk config confirmed" done={!!status?.risk_confirmed} />
          <GateStep n={4} label="Armed" done={!!status?.armed} />
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

      {/* ── Capital split (this service + Real Trade Service) ── */}
      <CapitalSplitCard
        totalBalance={pickNum(dhanAccount?.funds, "availabelBalance", "availableBalance", "availableCash")}
        splitPct={status?.scalp_pool_capital_share_pct ?? null}
        positionStocksAllocated={ledger?.total_allocated_capital ?? null}
        positionStocksAvailable={ledger?.available_capital ?? null}
        realTradeCashAvailable={rtCashAvailable}
        realTradeError={rtAccountError}
        highlight="position_stocks"
      />

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
      </>
      )}

      {subTab === "pipeline" && (
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
          Log in above to enable action buttons. All controls require the same admin password as Real Automatic Trade.
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
      )}

      {subTab === "screener" && (
      <>
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

      {/* ── Quality gate — recently dropped (session62, issue #5) ── */}
      {qualityGateDropped.length > 0 && (
        <div className="bg-graphite border border-signal-hold/40 rounded-2xl p-4">
          <div className="flex items-center justify-between mb-2">
            <p className="dash-section-title text-signal-hold">
              Quality Gate — {qualityGateDropped.length} recently dropped
            </p>
            <span className="font-display tabular-nums text-[9px] text-mist">
              Floor: Fund ≥ {status?.pipeline_config?.min_fundamental_score ?? 40}, Tech ≥{" "}
              {status?.pipeline_config?.min_technical_score ?? 40}, Mcap ≥ ₹
              {status?.pipeline_config?.min_market_cap_cr ?? 500}cr
            </span>
          </div>
          <p className="font-display tabular-nums text-[10px] text-mist mb-2">
            Candidates the price/volume screener ranked highly, but the fundamental/technical/market-cap floor
            rejected before capital was ever checked. If a symbol here looks like a genuine mover, MIN_FUNDAMENTAL_SCORE
            / MIN_TECHNICAL_SCORE / MIN_MARKET_CAP_CR are tunable env vars on this service (see docker-compose.yml) —
            no code change needed to loosen the floor.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-display tabular-nums">
              <thead>
                <tr className="text-mist text-left border-b border-slate">
                  <th className="py-1 pr-3">Symbol</th>
                  <th className="py-1 pr-3">%Chg</th>
                  <th className="py-1 pr-3">Fund</th>
                  <th className="py-1 pr-3">Tech</th>
                  <th className="py-1 pr-3">Mcap ₹cr</th>
                  <th className="py-1 pr-3">Why</th>
                  <th className="py-1">When</th>
                </tr>
              </thead>
              <tbody>
                {qualityGateDropped.slice(0, 20).map(c => (
                  <tr key={c.id} className="border-b border-slate/50">
                    <td className="py-1 pr-3 text-paper font-bold">{c.symbol}</td>
                    <td className={`py-1 pr-3 ${c.pct_change >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                      {c.pct_change >= 0 ? "+" : ""}{c.pct_change.toFixed(2)}%
                    </td>
                    <td className="py-1 pr-3 text-mist">{c.fundamental_score != null ? c.fundamental_score.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.technical_score != null ? c.technical_score.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.market_cap_cr != null ? c.market_cap_cr.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-signal-hold">{c.reason?.replace("QUALITY_GATE:", "") ?? "—"}</td>
                    <td className="py-1 text-mist">{fmtDateTimeIst(c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Candidate log (why entered/skipped) ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="dash-section-title">Candidate Log — Why Entered / Skipped</p>
          <button onClick={() => void loadCandidateLog()}
            className="px-3 py-1 rounded-lg bg-graphite border border-slate font-display tabular-nums text-[10px] text-mist">
            Refresh
          </button>
        </div>
        {candidateLogError ? (
          <p className="font-display tabular-nums text-[11px] text-signal-sell">{candidateLogError}</p>
        ) : filteredCandidateLog.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">
            {windowFilter === "all" ? "No candidates logged yet this session." : `No ${windowFilter} candidates logged yet this session.`}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-display tabular-nums">
              <thead>
                <tr className="text-mist text-left border-b border-slate">
                  <th className="py-1 pr-3">Symbol</th>
                  <th className="py-1 pr-3">Window</th>
                  <th className="py-1 pr-3">%Chg</th>
                  <th className="py-1 pr-3">Score</th>
                  <th className="py-1 pr-3">Decision</th>
                  <th className="py-1 pr-3">Reason</th>
                  <th className="py-1 pr-3">Fund</th>
                  <th className="py-1 pr-3">Tech</th>
                  <th className="py-1 pr-3">Mcap ₹cr</th>
                  <th className="py-1 pr-3">Catalyst</th>
                  <th className="py-1">When</th>
                </tr>
              </thead>
              <tbody>
                {filteredCandidateLog.map(c => (
                  <tr key={c.id} className="border-b border-slate/50">
                    <td className="py-1 pr-3 text-paper font-bold">{c.symbol}</td>
                    <td className="py-1 pr-3 text-mist">{c.window_source}</td>
                    <td className={`py-1 pr-3 ${c.pct_change >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                      {c.pct_change >= 0 ? "+" : ""}{c.pct_change.toFixed(2)}%
                    </td>
                    <td className="py-1 pr-3 text-mist">{c.composite_score != null ? c.composite_score.toFixed(1) : "—"}</td>
                    <td className={`py-1 pr-3 font-bold ${c.decision === "ENTERED" ? "text-signal-buy" : "text-signal-hold"}`}>{c.decision}</td>
                    <td className="py-1 pr-3 text-mist">{c.reason ?? "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.fundamental_score != null ? c.fundamental_score.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.technical_score != null ? c.technical_score.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.market_cap_cr != null ? c.market_cap_cr.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.has_positive_catalyst == null ? "—" : c.has_positive_catalyst ? "Yes" : "No"}</td>
                    <td className="py-1 text-mist">{fmtDateTimeIst(c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="font-display tabular-nums text-[9px] text-mist mt-2">
          Full audit trail of every scanned candidate the quality gate looked at and why it was taken or skipped — not just the final screener output above.
        </p>
      </div>

      {/* ── Learned intraday-restricted symbols (T2T/ASM/GSM) ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="dash-section-title">Intraday-Restricted Symbols</p>
          <button onClick={() => void loadRestrictedSymbols()}
            className="px-3 py-1 rounded-lg bg-graphite border border-slate font-display tabular-nums text-[10px] text-mist">
            Refresh
          </button>
        </div>
        {restrictedSymbolsError ? (
          <p className="font-display tabular-nums text-[11px] text-signal-sell">{restrictedSymbolsError}</p>
        ) : restrictedSymbols.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">No restricted symbols learned yet — none of Dhan's SELL rejections so far have been intraday-eligibility related.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-display tabular-nums">
              <thead>
                <tr className="text-mist text-left border-b border-slate">
                  <th className="py-1 pr-3">Symbol</th>
                  <th className="py-1 pr-3">Hits</th>
                  <th className="py-1 pr-3">First Seen</th>
                  <th className="py-1 pr-3">Last Seen</th>
                  <th className="py-1">Last Rejection Detail</th>
                </tr>
              </thead>
              <tbody>
                {restrictedSymbols.map(r => (
                  <tr key={r.symbol} className="border-b border-slate/50">
                    <td className="py-1 pr-3 text-paper font-bold">{r.symbol}</td>
                    <td className="py-1 pr-3 text-signal-sell">{r.hit_count}</td>
                    <td className="py-1 pr-3 text-mist">{fmtDateTimeIst(r.first_detected_at)}</td>
                    <td className="py-1 pr-3 text-mist">{fmtDateTimeIst(r.last_detected_at)}</td>
                    <td className="py-1 text-mist">{r.last_detail ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="font-display tabular-nums text-[9px] text-mist mt-2">
          Symbols Dhan has actually rejected as "not allowed to trade in Intraday" (T2T/ASM/GSM surveillance) — learned
          from live SELL rejections, not a static list. These are filtered out of every future cycle's candidates
          before capital or a Dhan call is committed to them.
        </p>
      </div>
      </>
      )}

      {subTab === "positions" && (
      <>
      {/* ── ADDED (this session): manual controls — Refresh / Check Exits
          Now (reconcile) / Manual Buy — directly on the Positions tab, so
          you don't have to switch to Pipeline to reconcile or wait 15s
          for the next poll to see a fresh price. Manual Exit lives per-row
          below instead, since it needs a specific position id. */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">Manual Controls</p>
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <button disabled={busy !== null} onClick={() => void loadAll()}
            className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist disabled:opacity-40">
            ↻ Refresh
          </button>
          <button disabled={!loggedIn || busy !== null} onClick={() => doAction("reconcile")}
            className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist disabled:opacity-40">
            {busy === "reconcile" ? "Checking…" : "Check Exits Now (Reconcile)"}
          </button>
          {lastRefreshed && (
            <span className="font-display tabular-nums text-[9px] text-mist">
              Updated {lastRefreshed.toLocaleTimeString()}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest mb-1">Symbol</p>
            <input value={manualBuySymbol} onChange={e => setManualBuySymbol(e.target.value)}
              placeholder="e.g. RELIANCE" disabled={!loggedIn || busy !== null}
              className="px-3 py-2 rounded-xl bg-ink border border-slate font-display tabular-nums text-xs text-paper w-36 disabled:opacity-40" />
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest mb-1">Qty (optional — blank = auto-size)</p>
            <input value={manualBuyQty} onChange={e => setManualBuyQty(e.target.value)}
              placeholder="auto" inputMode="numeric" disabled={!loggedIn || busy !== null}
              className="px-3 py-2 rounded-xl bg-ink border border-slate font-display tabular-nums text-xs text-paper w-24 disabled:opacity-40" />
          </div>
          <button disabled={!loggedIn || busy !== null || !manualBuySymbol.trim()} onClick={() => void doManualBuy()}
            className="px-4 py-2 rounded-xl bg-profit/20 border border-profit font-display tabular-nums text-xs text-profit disabled:opacity-40">
            {busy === "manual_buy" ? "Placing…" : "Buy Now"}
          </button>
        </div>
        <p className="font-display tabular-nums text-[9px] text-mist mt-2">
          Manual BUY still goes through every real gate an automatic entry does — armed, kill switch,
          order budget, max concurrent positions, and available capital. It just skips the screener's
          own symbol selection.
        </p>
      </div>

      {/* ── Open positions ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">Open Positions ({openPositions.length}/{status?.max_concurrent_scalp_positions ?? 5})</p>
        {openPositions.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">No open scalp positions.</p>
        ) : (
          <div className="space-y-2">{openPositions.map(p => <PositionRow key={p.id} p={p} onClose={doClosePosition} busy={busy} loggedIn={loggedIn} />)}</div>
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

      </>
      )}

      {subTab === "history" && (
      <>
      {/* ── Trade history ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="dash-section-title">Trade History — Buy vs Sell, Win Rate</p>
          {/* this session: Today/Last-3-days subtabs the user asked for.
              Backend also only retains 3 days (see config.
              TRADE_HISTORY_RETENTION_DAYS + orders/reconcile.py::
              run_retention_cleanup()), so "3d" is effectively "all". */}
          <div className="flex gap-1">
            {(["today", "3d"] as const).map(r => (
              <button
                key={r}
                onClick={() => setTradeHistoryRange(r)}
                className={`px-2.5 py-1 rounded-lg font-display tabular-nums text-[10px] uppercase border ${
                  tradeHistoryRange === r
                    ? "bg-signal-buy/20 border-signal-buy/40 text-signal-buy"
                    : "border-slate text-mist"
                }`}
              >
                {r === "today" ? "Today" : "Last 3 Days"}
              </button>
            ))}
          </div>
        </div>
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
                      <th className="py-1 pr-3">Qty</th>
                      <th className="py-1 pr-3">Buy ₹</th>
                      <th className="py-1 pr-3">Sell ₹</th>
                      <th className="py-1 pr-3">Buy Total</th>
                      <th className="py-1 pr-3">Sell Total</th>
                      <th className="py-1 pr-3">P&L</th>
                      <th className="py-1 pr-3">Status</th>
                      <th className="py-1 pr-3">Closed</th>
                      {/* AUDIT ADD (session22 cont'd): the table had no
                          order-ID column at all — dhan_super_order_id was
                          only ever shown in the Overview subtab's
                          PositionRow cards, not here, even though the
                          backend now returns entry and exit order ids too
                          (see main.py::trades_history). */}
                      <th className="py-1">Order IDs</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tradeHistory.trades.map(t => (
                      <tr key={t.id} className="border-b border-slate/50">
                        <td className="py-1 pr-3 text-paper font-bold">{t.symbol}</td>
                        <td className="py-1 pr-3 text-mist">{t.window_source}</td>
                        <td className="py-1 pr-3 text-mist">{t.quantity}</td>
                        <td className="py-1 pr-3 text-paper">₹{t.entry_price.toFixed(2)}</td>
                        <td className="py-1 pr-3 text-paper">{t.exit_price != null ? `₹${t.exit_price.toFixed(2)}` : "—"}</td>
                        {/* AUDIT ADD (this session): buy/sell VALUE (price × qty), not
                            just per-share price — "total price" the user actually paid/
                            received on the leg, same distinction real-trade-service's
                            Charges tab makes between per-share price and order value. */}
                        <td className="py-1 pr-3 text-mist">{fmtInr(t.entry_price * t.quantity)}</td>
                        <td className="py-1 pr-3 text-mist">{t.exit_price != null ? fmtInr(t.exit_price * t.quantity) : "—"}</td>
                        <td className={`py-1 pr-3 ${t.realized_pnl != null && t.realized_pnl >= 0 ? "text-signal-buy" : t.realized_pnl != null ? "text-signal-sell" : "text-mist"}`}>
                          {t.realized_pnl != null ? `${fmtInr(t.realized_pnl)} (${(t.realized_pnl_pct ?? 0).toFixed(2)}%)` : "—"}
                        </td>
                        <td className={`py-1 pr-3 ${statusColor(t.status)}`}>{t.status}</td>
                        <td className="py-1 pr-3 text-mist">{fmtDateTimeIst(t.closed_at)}</td>
                        <td className="py-1 text-mist text-[9px] leading-tight">
                          {t.dhan_super_order_id ? <div>Entry {t.dhan_super_order_id}</div> : null}
                          {t.dhan_exit_order_id && t.dhan_exit_order_id !== t.dhan_super_order_id
                            ? <div>Exit {t.dhan_exit_order_id}</div>
                            : null}
                          {!t.dhan_super_order_id && !t.dhan_exit_order_id ? "—" : null}
                        </td>
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

      </>
      )}

      {subTab === "dhanorders" && (
      <>
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

      </>
      )}

      {subTab === "charges" && (() => {
        // AUDIT ADD (this session), ported from Real Automatic Trade's
        // Charges tab — same Dhan NSE-equity rate card, but this service's
        // orders are ALWAYS intraday (config.SCALP_PRODUCT_TYPE = "INTRADAY",
        // FIXED session38 — was "INTRA", an invalid Dhan productType enum value,
        // never "CNC" — see main.py's /status pipeline_config), so there's
        // no delivery/intraday branch to pick here, unlike real-trade-
        // service which trades both. Built from dhanLive (Dhan's own
        // super-order-leg book, broker-side truth) — an ENTRY_LEG that
        // TRADED is the BUY, whichever of TARGET_LEG/STOP_LOSS_LEG actually
        // TRADED is the SELL.
        const BROKERAGE_CAP = 20;
        const BROKERAGE_PCT = 0.03 / 100;
        const STT_INTRA_PCT = 0.025 / 100; // both sides, intraday
        const EXCHANGE_PCT = 0.00345 / 100;
        const SEBI_PCT = 0.0001 / 100;
        const GST_PCT = 0.18;
        const STAMP_INTRA_PCT = 0.003 / 100; // buy side only

        interface ChargeBreakdown {
          brokerage: number; stt: number; exchange: number; sebi: number; gst: number; stamp: number; total: number;
        }
        function calcCharges(buyVal: number, sellVal: number): ChargeBreakdown {
          const turnover = buyVal + sellVal;
          const brokerage = Math.min(buyVal * BROKERAGE_PCT, BROKERAGE_CAP) + Math.min(sellVal * BROKERAGE_PCT, BROKERAGE_CAP);
          const stt = turnover * STT_INTRA_PCT;
          const exchange = turnover * EXCHANGE_PCT;
          const sebi = turnover * SEBI_PCT;
          const gst = (brokerage + exchange) * GST_PCT;
          const stamp = buyVal * STAMP_INTRA_PCT;
          return { brokerage, stt, exchange, sebi, gst, stamp, total: brokerage + stt + exchange + sebi + gst + stamp };
        }

        interface LegCharge { symbol: string; leg: string; side: string; qty: number; price: number; charges: ChargeBreakdown; }
        const filledLegs = (dhanLive?.orders ?? []).filter(o => {
          const qty = Number(o.quantity || 0);
          const price = Number(o.price || 0);
          const st = String(o.orderStatus ?? "").toUpperCase();
          return qty > 0 && price > 0 && (st === "TRADED" || st === "FILLED" || st === "COMPLETE");
        });
        const legCharges: LegCharge[] = filledLegs.map(o => {
          const side = String(o.transactionType ?? "").toUpperCase();
          const qty = Number(o.quantity || 0);
          const price = Number(o.price || 0);
          const val = qty * price;
          return {
            symbol: String(o.tradingSymbol ?? "—"), leg: String(o.legName ?? "—"), side, qty, price,
            charges: calcCharges(side === "BUY" ? val : 0, side === "SELL" ? val : 0),
          };
        });
        const totalBrokerage = legCharges.reduce((s, o) => s + o.charges.brokerage, 0);
        const totalSTT = legCharges.reduce((s, o) => s + o.charges.stt, 0);
        const totalExchange = legCharges.reduce((s, o) => s + o.charges.exchange, 0);
        const totalSEBI = legCharges.reduce((s, o) => s + o.charges.sebi, 0);
        const totalGST = legCharges.reduce((s, o) => s + o.charges.gst, 0);
        const totalStamp = legCharges.reduce((s, o) => s + o.charges.stamp, 0);
        const grandTotal = legCharges.reduce((s, o) => s + o.charges.total, 0);

        return (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <p className="dash-section-title">Dhan charges — Position Stocks (intraday only)</p>
              <button onClick={() => void loadDhanLive()}
                className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist">
                ↻ Refresh
              </button>
            </div>

            <div className="bg-graphite border border-slate rounded-2xl p-4">
              <p className="font-display tabular-nums text-[10px] text-mist uppercase tracking-widest mb-2">Live Dhan order book, right now</p>
              <div className="grid grid-cols-2 gap-2 mb-3">
                <div className="bg-ink border border-slate rounded-xl p-3">
                  <p className="text-[9px] text-mist uppercase tracking-widest">Total charges</p>
                  <p className="font-display tabular-nums font-bold text-sm text-signal-sell">-{fmtInr(grandTotal, 2)}</p>
                  <p className="text-[9px] text-mist mt-0.5">{legCharges.length} filled legs</p>
                </div>
                <div className="bg-ink border border-slate rounded-xl p-3">
                  <p className="text-[9px] text-mist uppercase tracking-widest">Brokerage + GST</p>
                  <p className="font-display tabular-nums font-bold text-sm text-signal-sell">-{fmtInr(totalBrokerage + totalGST, 2)}</p>
                  <p className="text-[9px] text-mist mt-0.5">₹20 cap per leg, 18% GST</p>
                </div>
              </div>
              {ledger && (
                <div className="bg-ink border border-slate rounded-xl p-3 mb-3">
                  <div className="flex items-center justify-between font-display tabular-nums text-[11px] text-mist mb-1">
                    <span>Realized P&L today (price only)</span>
                    <span className={ledger.realized_pnl_today >= 0 ? "text-signal-buy" : "text-signal-sell"}>
                      {ledger.realized_pnl_today >= 0 ? "+" : ""}{fmtInr(ledger.realized_pnl_today, 2)}
                    </span>
                  </div>
                  <div className="flex items-center justify-between font-display tabular-nums text-[11px] text-mist mb-1">
                    <span>− Charges (this order book snapshot)</span>
                    <span className="text-signal-sell">-{fmtInr(grandTotal, 2)}</span>
                  </div>
                  <div className="flex items-center justify-between font-display tabular-nums text-xs font-bold border-t border-slate pt-1.5 mt-1">
                    <span className="text-paper">Net (approx, after these charges)</span>
                    <span className={(ledger.realized_pnl_today - grandTotal) >= 0 ? "text-signal-buy" : "text-signal-sell"}>
                      {(ledger.realized_pnl_today - grandTotal) >= 0 ? "+" : ""}{fmtInr(ledger.realized_pnl_today - grandTotal, 2)}
                    </span>
                  </div>
                </div>
              )}
              <div className="border-t border-slate pt-3">
                <p className="font-display tabular-nums text-[10px] text-mist mb-2 uppercase tracking-widest">Full breakdown</p>
                <div className="space-y-1.5 font-display tabular-nums text-[11px]">
                  {[
                    { label: "Brokerage", val: totalBrokerage, note: "₹20 or 0.03%/leg, whichever lower" },
                    { label: "STT", val: totalSTT, note: "0.025% intraday, both sides" },
                    { label: "Exchange Txn", val: totalExchange, note: "0.00345% NSE" },
                    { label: "SEBI charges", val: totalSEBI, note: "0.0001% on turnover" },
                    { label: "GST", val: totalGST, note: "18% on brokerage + exchange fees" },
                    { label: "Stamp duty", val: totalStamp, note: "0.003% intraday (buy side)" },
                  ].map(row => (
                    <div key={row.label} className="flex items-center justify-between">
                      <div><span className="text-paper">{row.label}</span><span className="text-mist text-[9px] ml-2">{row.note}</span></div>
                      <span className={row.val > 0 ? "text-signal-sell" : "text-mist"}>{row.val > 0 ? `-${fmtInr(row.val, 2)}` : "—"}</span>
                    </div>
                  ))}
                  <div className="flex items-center justify-between border-t border-slate pt-2 mt-1">
                    <span className="font-bold text-paper">Total charges</span>
                    <span className="font-bold text-signal-sell">-{fmtInr(grandTotal, 2)}</span>
                  </div>
                </div>
              </div>
            </div>

            {legCharges.length > 0 ? (
              <div className="bg-graphite border border-slate rounded-2xl p-4">
                <p className="dash-section-title mb-3">Per-leg charges</p>
                <div className="space-y-2">
                  {legCharges.map((oc, i) => (
                    <div key={i} className="bg-ink border border-slate rounded-xl p-3">
                      <div className="flex items-center justify-between mb-1">
                        <div className="flex items-center gap-2">
                          <span className={`font-display tabular-nums text-xs font-bold ${oc.side === "BUY" ? "text-signal-buy" : "text-signal-sell"}`}>{oc.side}</span>
                          <span className="font-display tabular-nums text-sm font-bold text-paper">{oc.symbol}</span>
                          <span className="font-display tabular-nums text-[9px] text-mist">{oc.leg} · MIS</span>
                        </div>
                        <span className="font-display tabular-nums text-xs text-signal-sell font-bold">-{fmtInr(oc.charges.total, 2)}</span>
                      </div>
                      <div className="flex gap-3 font-display tabular-nums text-[10px] text-mist">
                        <span>Qty <span className="text-paper">{oc.qty}</span></span>
                        <span>Price <span className="text-paper">₹{oc.price.toFixed(2)}</span></span>
                        <span>Value <span className="text-paper">{fmtInr(oc.qty * oc.price, 0)}</span></span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                <p className="font-display tabular-nums text-sm text-mist">No filled legs in the current Dhan order book snapshot.</p>
                <p className="font-display tabular-nums text-[10px] text-mist mt-1">Refresh Dhan Live Orders first, or check the Trade History tab for older closed trades (charges aren't retroactively computed there).</p>
              </div>
            )}
          </div>
        );
      })()}

      {subTab === "settings" && (
      <>
      {/* ── Settings ── */}
      <details open className="bg-graphite border border-slate rounded-2xl p-4">
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
      </>
      )}
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

function PositionRow({ p, onClose, busy, loggedIn }: {
  p: ScalpPositionRow;
  onClose?: (id: number, symbol: string) => void;
  busy?: string | null;
  loggedIn?: boolean;
}) {
  const pnlPct = p.realized_pnl_pct ?? (p.exit_price && p.entry_price
    ? ((p.exit_price - p.entry_price) / p.entry_price) * 100
    : null);
  // ADDED (this session — "manual exit if needed"): only offer Exit Now
  // for a position that's actually still open at the broker (OPEN, or
  // EXIT_LEGS_REJECTED — a position whose bracket legs failed to place
  // and so has no working target/stop at all, the case that most needs
  // a manual way out).
  const canClose = onClose && (p.status === "OPEN" || p.status === "EXIT_LEGS_REJECTED");
  return (
    <div className="border border-slate rounded-xl p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="font-display tabular-nums font-bold text-sm text-paper">{p.symbol}</span>
        <div className="flex items-center gap-2">
          <span className="font-display tabular-nums text-[10px] text-mist">{p.window_source}</span>
          <span className={`font-display tabular-nums text-[10px] uppercase font-bold ${statusColor(p.status)}`}>{p.status}</span>
          {canClose && (
            <button disabled={!loggedIn || busy !== null} onClick={() => onClose!(p.id, p.symbol)}
              className="px-2 py-1 rounded-lg bg-signal-sell/20 border border-signal-sell font-display tabular-nums text-[10px] text-signal-sell disabled:opacity-40">
              {busy === `close_${p.id}` ? "Exiting…" : "Exit Now"}
            </button>
          )}
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 text-[11px]">
        <div><span className="text-mist">Buy </span><span className="tabular-nums text-paper">₹{p.entry_price.toFixed(2)}</span></div>
        <div><span className="text-mist">Sell </span><span className="tabular-nums text-paper">{p.exit_price != null ? `₹${p.exit_price.toFixed(2)}` : "—"}</span></div>
        <div><span className="text-mist">Qty </span><span className="tabular-nums text-paper">{p.quantity}</span></div>
        {/* AUDIT ADD (this session): order value (price × qty) — the actual
            rupee amount bought/sold, distinct from the per-share price
            already shown above, same distinction Trade History's Buy/Sell
            Total columns make. */}
        <div><span className="text-mist">Total </span><span className="tabular-nums text-paper">
          {fmtInr(p.entry_price * p.quantity)}{p.exit_price != null ? ` → ${fmtInr(p.exit_price * p.quantity)}` : ""}
        </span></div>
        <div><span className="text-mist">Target </span><span className="tabular-nums text-signal-buy">₹{p.target_price.toFixed(2)} <span className="text-[9px]">({p.adaptive_target_pct.toFixed(1)}%)</span></span></div>
        <div><span className="text-mist">Stop </span><span className="tabular-nums text-signal-sell">₹{p.stop_price.toFixed(2)} <span className="text-[9px]">({p.adaptive_stop_pct.toFixed(1)}%)</span></span>
          {/* this session: shows whether/where breakeven-stop will move
              (or already moved) this position's stop — see
              orders/breakeven.py. Only rendered when a trigger was
              actually recorded (positions predating the feature have
              none — see models.py comment). */}
          {p.breakeven_trigger_pct != null && (
            <span className={`ml-1 text-[9px] ${p.stop_moved_to_breakeven ? "text-signal-buy" : "text-mist"}`}>
              {p.stop_moved_to_breakeven ? "🔒 breakeven" : `BE@${p.breakeven_trigger_pct.toFixed(0)}%`}
            </span>
          )}
        </div>
      </div>
      {/* AUDIT ADD (session60): live current price / unrealized P&L for an
          OPEN position — mirrors real-trade-service's Positions tab, which
          has always shown Current + a live +/-% next to Entry/Stop/Target.
          Only rendered for OPEN rows since current_price is only populated
          server-side while a position is open. "—" when this service's
          Angel One feed hasn't ticked the symbol yet (same fail-open
          convention as the rest of this dashboard). */}
      {/* BUG FIX (this session): was `p.status === "OPEN"` only — now also
          shown for EXIT_LEGS_REJECTED, which is real live exposure too
          (see openPositions fix above) and now gets a live price from the
          backend as well. */}
      {(p.status === "OPEN" || p.status === "EXIT_LEGS_REJECTED") && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] mt-2 pt-2 border-t border-slate/50">
          <div><span className="text-mist">Current </span><span className="tabular-nums text-paper">{p.current_price != null ? `₹${p.current_price.toFixed(2)}` : "—"}</span></div>
          <div><span className="text-mist">Value </span><span className="tabular-nums text-paper">{p.current_amount != null ? fmtInr(p.current_amount) : "—"}</span></div>
          <div>
            <span className="text-mist">Unrealized </span>
            {p.unrealized_pnl != null ? (
              <span className={`tabular-nums font-bold ${p.unrealized_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                {fmtInr(p.unrealized_pnl)} ({p.unrealized_pnl_pct?.toFixed(2)}%)
              </span>
            ) : <span className="tabular-nums text-paper">—</span>}
          </div>
          <div><span className="text-mist">To target/stop </span><span className="tabular-nums text-paper">
            {p.target_distance_pct != null && p.stop_distance_pct != null
              ? `${p.target_distance_pct.toFixed(1)}% / ${p.stop_distance_pct.toFixed(1)}%`
              : "—"}
          </span></div>
        </div>
      )}
      {p.realized_pnl != null && (
        <p className={`font-display tabular-nums text-xs mt-2 font-bold ${p.realized_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
          P&L {fmtInr(p.realized_pnl)} {pnlPct != null ? `(${pnlPct.toFixed(2)}%)` : ""}
        </p>
      )}
      {/* AUDIT ADD (this session): an ERROR row renders Buy price/Total
          identically to a real trade with no sell yet — but the entry
          order was actually rejected/cancelled by Dhan and never filled,
          so no money was ever at risk. Without this note it reads as an
          open or lost trade. */}
      {p.status === "ERROR" && (
        <p className="font-display tabular-nums text-[10px] text-signal-avoid mt-1">
          ⚠ Entry order was rejected/never filled — no shares bought, no capital was at risk. Buy price/Total above are the attempted order, not a real fill.
        </p>
      )}
      <p className="font-display tabular-nums text-[9px] text-mist mt-1">
        Opened {fmtDateTimeIst(p.opened_at)}
        {p.closed_at ? ` · Closed ${fmtDateTimeIst(p.closed_at)}` : ""}
        {p.is_first_live_order ? " · 🟡 FIRST LIVE ORDER" : ""}
        {p.dhan_super_order_id ? ` · Order ${p.dhan_super_order_id}` : ""}
        {/* AUDIT FIX (session22 cont'd): dhan_exit_order_id was captured by
            reconcile.py for every closed position but never returned by the
            API or shown here — only the entry/bracket order id was visible,
            so there was no way to see which specific leg (target or stop)
            actually filled. Only shown when it's a real, distinct order id
            (reconcile.py falls back to the super order id itself when
            Dhan's leg payload omits its own orderId, in which case there's
            nothing new to show). */}
        {p.dhan_exit_order_id && p.dhan_exit_order_id !== p.dhan_super_order_id
          ? ` · Exit Order ${p.dhan_exit_order_id}`
          : ""}
      </p>
    </div>
  );
}
