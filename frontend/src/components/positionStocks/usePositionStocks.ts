// E538 (redesign round 5b): all of Position Stocks' state, effects, handlers and derived lists, moved out of PositionStocksTab.tsx (code unchanged, same order).
// The ten sub-tab files read what they need from the object this returns; the page itself only draws the header, banners and tab bar.
import { useCallback, useEffect, useMemo, useState } from "react";
import { publishModeStrip } from "../app/modeStrip";
import { AUTH_CHANGED_EVENT } from "../../adminAuth";
import { setVisibleInterval } from "../../visibleInterval";
import { positionStocksApi, getPositionStocksApiUrl, setPositionStocksApiUrl, type ScalpCumulativeCharges, type ScalpChargeTrades, getSessionToken, setSessionExpiredHandler, type ScalpStatus, type ScalpPositionRow, type ScalpCandidateRow, type ScalpLedgerState, type ScalpTradeHistory, type DhanLiveOrders, type ScalpCycleResult, type DhanAccountStatus, type ScalpPipelineStatus, type ScalpCandidateLogRow, type ScalpIntradayRestrictedRow } from "../../positionStocksApi";
import { type Window } from "./parts";

export type SubTab = "overview" | "board" | "timemachine" | "pipeline" | "screener" | "positions" | "history" | "dhanorders" | "charges" | "settings";

export function usePositionStocks() {
  const [apiUrlInput, setApiUrlInput] = useState(getPositionStocksApiUrl());
  const [status, setStatus] = useState<ScalpStatus | null>(null);
  const [positions, setPositions] = useState<ScalpPositionRow[]>([]);
  const [candidates, setCandidates] = useState<ScalpCandidateRow[]>([]);
  const [ledger, setLedger] = useState<ScalpLedgerState | null>(null);
  // E535: tell the shell's mode strip what this page already knows (this page is always REAL money). The strip only reads it: no call, no handler.
  useEffect(() => {
    publishModeStrip("positionstocks", { mode: "REAL", armed: status ? status.armed : null, pnl: ledger ? ledger.realized_pnl_today : null });
  }, [status, ledger]);
  useEffect(() => () => publishModeStrip("positionstocks", null), []);
  const [tradeHistory, setTradeHistory] = useState<ScalpTradeHistory | null>(null);
  // this session — Trade History Today/Last-3-days sub-tabs the user asked
  // for. Defaults to "3d" since that's the full depth the backend retains
  // anyway (orders/reconcile.py::run_retention_cleanup(),
  // config.TRADE_HISTORY_RETENTION_DAYS).
  const [tradeHistoryRange, setTradeHistoryRange] = useState<"today" | "3d">("3d");
  const [dhanLive, setDhanLive] = useState<DhanLiveOrders | null>(null);
  const [dhanLiveError, setDhanLiveError] = useState<string | null>(null);
  const [cumCharges, setCumCharges] = useState<ScalpCumulativeCharges | null>(null);
  const [cumChargesError, setCumChargesError] = useState<string | null>(null);
  const [chargeTrades, setChargeTrades] = useState<ScalpChargeTrades | null>(null);   // group 263: today's stored per-trade rows
  const [lastCycleResult, setLastCycleResult] = useState<ScalpCycleResult | null>(null);
  // ADDED (session48 — "no live process shows and no stock name shows"):
  // polled snapshot of the current/last cycle from the new GET
  // /pipeline/status route (see positionStocksApi.ts + pipeline_status.py).
  const [pipelineLive, setPipelineLive] = useState<ScalpPipelineStatus | null>(null);
  const [windowFilter, setWindowFilter] = useState<Window | "all">("all");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmKill, setConfirmKill] = useState(false);
  const [showCloseAll, setShowCloseAll] = useState(false);   // group 466
  // AUDIT FIX (this session): backend POST /ledger/reset-daily existed with
  // no frontend wiring at all — see positionStocksApi.ts's resetLedgerDaily
  // comment. Same confirm-guard pattern as confirmKill, since this clears
  // today's realized P&L + kill switch on demand (an emergency/manual
  // override on top of the automatic midnight reset).
  const [confirmResetDaily, setConfirmResetDaily] = useState(false);
  // ADDED (this session — manual Buy control on the Positions tab)
  const [manualBuySymbol, setManualBuySymbol] = useState("");
  const [manualBuyQty, setManualBuyQty] = useState("");
  const [manualBuyReason, setManualBuyReason] = useState("");     // E30: optional note saved to the journal with the order
  const [manualBuyEmotion, setManualBuyEmotion] = useState("");
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const [dhanAccount, setDhanAccount] = useState<DhanAccountStatus | null>(null);
  const [nowTick, setNowTick] = useState(() => Date.now());
  const [candidateLog, setCandidateLog] = useState<ScalpCandidateLogRow[]>([]);
  const [candidateLogError, setCandidateLogError] = useState<string | null>(null);
  const [restrictedSymbols, setRestrictedSymbols] = useState<ScalpIntradayRestrictedRow[]>([]);
  const [restrictedSymbolsError, setRestrictedSymbolsError] = useState<string | null>(null);

  // Sub-tabs — the page used to be one long scroll of every section at
  // once; grouped into tabs so each screen only shows what's relevant to
  // that task (arming/health/risk vs. screener vs. positions vs. history).
  const [subTab, setSubTab] = useState<SubTab>("overview");

  const [loggedIn, setLoggedIn] = useState(!!getSessionToken());
  useEffect(() => {
    // E535: follow the stored token when the shared login (AdminGate / the top-bar Extend) announces a change
    const sync = () => { const has = !!getSessionToken(); setLoggedIn(has); if (has) void loadDhanAccount(); };
    window.addEventListener(AUTH_CHANGED_EVENT, sync);
    setSessionExpiredHandler(() => setLoggedIn(false));
    return () => { window.removeEventListener(AUTH_CHANGED_EVENT, sync); setSessionExpiredHandler(null); };
  }, []);

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

  // group 258: brokerage since the start (survives the 3-day trade-history retention).
  const loadCumCharges = useCallback(async () => {
    try {
      setCumCharges(await positionStocksApi.cumulativeCharges(14));
      setCumChargesError(null);
      try { setChargeTrades(await positionStocksApi.chargesTrades()); } catch { /* per-trade list is optional */ }
    } catch (e: any) {
      setCumChargesError(e?.message || "Failed to load cumulative charges");
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
    // group 184: the five data polls pause while the browser tab is hidden and run once when it is shown again.
    const stops = [
      setVisibleInterval(() => void loadAll(), 15_000),
      setVisibleInterval(() => void loadDhanLive(), 30_000),
      setVisibleInterval(() => void loadDhanAccount(), 30_000),
      setVisibleInterval(() => void loadCandidateLog(), 15_000),
      setVisibleInterval(() => void loadRestrictedSymbols(), 30_000),
    ];
    return () => { stops.forEach((stop) => stop()); };
  }, [loadAll, loadDhanLive, loadDhanAccount, loadCandidateLog, loadRestrictedSymbols]);

  // group 258: cumulative brokerage is only needed on the Charges sub-tab; refresh it every 60 s while that tab is open.
  // group 263: the Overview P&L tiles also show the figure after charges, so it loads there too.
  useEffect(() => {
    if (subTab !== "charges" && subTab !== "overview") return;
    void loadCumCharges();
    return setVisibleInterval(() => void loadCumCharges(), 60_000);
  }, [subTab, loadCumCharges]);

  // ADDED (session48 — "no live process shows and no stock name shows"):
  // polls GET /pipeline/status every 2s while the Pipeline sub-tab is open,
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
    const stop = setVisibleInterval(poll, 2000);
    return () => { cancelled = true; stop(); };
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
      const r = await positionStocksApi.manualBuy(symbol, qty, manualBuyReason.trim(), manualBuyEmotion);
      setManualBuySymbol(""); setManualBuyQty(""); setManualBuyReason(""); setManualBuyEmotion("");
      await loadAll();
      setError(r.journal_saved === false ? `${r.symbol} bought, but the journal note could not be saved. Add it again from the Board tab → Trade notes.` : null);
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
  // group192: an entry order Dhan rejected/cancelled never held any shares — it is not a closed trade. 13
  // identical HEGAM rejections used to inflate "Closed Today (13)"; they now get their own section below.
  const isRejectedEntry = (p: ScalpPositionRow) =>
    p.status === "ERROR" && (p.error_message ?? "").startsWith("Entry leg");
  const isToday = (p: ScalpPositionRow) => {
    const ts = p.closed_at ?? p.opened_at;
    if (!ts) return false;
    return new Date(ts).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" }) === todayIst;
  };
  const closedToday = useMemo(
    () => positions.filter(p => {
      // BUG FIX (this session): exclude EXIT_LEGS_REJECTED here too — it's
      // still live exposure (see openPositions above), not a closed trade.
      if (p.status === "OPEN" || p.status === "EXIT_LEGS_REJECTED") return false;
      if (isRejectedEntry(p)) return false;
      return isToday(p);
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [positions, todayIst]
  );
  const rejectedEntriesToday = useMemo(
    () => positions.filter(p => isRejectedEntry(p) && isToday(p)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
  const killSwitchTripped = status?.daily_loss_kill_switch || ledger?.daily_loss_kill_switch_tripped;

  return {
    apiUrlInput,
    setApiUrlInput,
    status,
    setStatus,
    positions,
    setPositions,
    candidates,
    setCandidates,
    ledger,
    setLedger,
    tradeHistory,
    setTradeHistory,
    tradeHistoryRange,
    setTradeHistoryRange,
    dhanLive,
    setDhanLive,
    dhanLiveError,
    setDhanLiveError,
    cumCharges,
    setCumCharges,
    cumChargesError,
    setCumChargesError,
    chargeTrades,
    setChargeTrades,
    lastCycleResult,
    setLastCycleResult,
    pipelineLive,
    setPipelineLive,
    windowFilter,
    setWindowFilter,
    busy,
    setBusy,
    error,
    setError,
    confirmKill,
    setConfirmKill,
    showCloseAll,
    setShowCloseAll,
    confirmResetDaily,
    setConfirmResetDaily,
    manualBuySymbol,
    setManualBuySymbol,
    manualBuyQty,
    setManualBuyQty,
    manualBuyReason,
    setManualBuyReason,
    manualBuyEmotion,
    setManualBuyEmotion,
    lastRefreshed,
    setLastRefreshed,
    dhanAccount,
    setDhanAccount,
    nowTick,
    setNowTick,
    candidateLog,
    setCandidateLog,
    candidateLogError,
    setCandidateLogError,
    restrictedSymbols,
    setRestrictedSymbols,
    restrictedSymbolsError,
    setRestrictedSymbolsError,
    subTab,
    setSubTab,
    loggedIn,
    setLoggedIn,
    loadAll,
    loadCandidateLog,
    loadRestrictedSymbols,
    loadDhanLive,
    loadCumCharges,
    loadDhanAccount,
    saveApiUrl,
    doAction,
    doManualBuy,
    doClosePosition,
    dhanTokenSecondsRemaining,
    openPositions,
    todayIst,
    isRejectedEntry,
    isToday,
    closedToday,
    rejectedEntriesToday,
    filteredCandidates,
    grouped,
    filteredCandidateLog,
    qualityGateDropped,
    killSwitchTripped,
  };
}

export type PsState = ReturnType<typeof usePositionStocks>;
