import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MoneyModeBanner } from "./ui/MoneyModeBanner";   // design v5
import { setVisibleInterval } from "../visibleInterval";
import {
  realTradeApi, getRealTradeApiUrl, setRealTradeApiUrl, type CumulativeBrokerage,
  getSessionToken, setSessionToken, setSessionExpiredHandler,
  type GateStatus, type AuditLogRow, type Position, type OrderRow, type CycleResult, type DhanStatus, type DhanEdisSummary,
  type PipelineStatus, type CandidateRow, type WatchlistEntry, type ResilienceStatus,
} from "../realTradeApi";
import { SubTabs } from "./ui/SubTabs";
import { NavIcon } from "./ui/NavIcon";
import { PageHero } from "./ui/PageHero";   // design v6
import { KpiStrip, type Kpi } from "./ui/KpiStrip";   // design v6
import { AiPulse, type PulseChip } from "./ui/AiPulse";   // design v6
import { signedInr } from "./realAuto/history";   // E122
// Cross-service, read-only: position-stocks-service's own capital ledger
// and split percentage, used only to populate the "Position Stocks" side
// of CapitalSplitCard below (see that component's docstring for why this
// is a best-effort, non-blocking call).
import { positionStocksApi, getPositionStocksApiUrl } from "../positionStocksApi";
import type { PriceLevel } from "../chartContext";
// E31: split out of this file (components/realAuto/)
import { Dot } from "./realAuto/parts";
import { type Mode, type Tab } from "./realAuto/types";
import { OrdersTab } from "./realAuto/tabs/OrdersTab";
import { PositionsTab } from "./realAuto/tabs/PositionsTab";
import { WatchlistTab } from "./realAuto/tabs/WatchlistTab";
import { LiveTab } from "./realAuto/tabs/LiveTab";
import { PipelineTab } from "./realAuto/tabs/PipelineTab";
import { ChargesTab } from "./realAuto/tabs/ChargesTab";
import { LogTab } from "./realAuto/tabs/LogTab";
import { HistoryTab } from "./realAuto/tabs/HistoryTab";   // E122: closed-trade records, same idea as Position Stocks' Trade History
import { OverviewTab, type RiskForm } from "./realAuto/tabs/OverviewTab";
import { BoardTab } from "./realAuto/tabs/BoardTab";
import { openLivePositions, type LivePositionRow } from "./realAuto/liveParts";
// kept as the public surface of this module (the types and helper used to be declared here)
export { openLivePositions };
export type { LivePositionRow };







// ── Main component ───────────────────────────────────────────────────────────
export default function RealAutoTrade() {
  const [mode, setMode] = useState<Mode>("REAL");
  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const [apiUrlInput, setApiUrlInput] = useState(getRealTradeApiUrl());

  const [status, setStatus] = useState<GateStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [regenLoading, setRegenLoading] = useState(false);

  const [loggedIn, setLoggedIn] = useState(!!getSessionToken());
  const [showCloseAll, setShowCloseAll] = useState(false);   // group 466
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");

  const [dhanClientId, setDhanClientId] = useState("");
  const [dhanToken, setDhanToken] = useState("");
  const [dhanAccount, setDhanAccount] = useState<(DhanStatus & { funds: any; funds_error: string | null }) | null>(null);
  // Position Stocks Service's own capital ledger + split pct, fetched
  // cross-service purely to populate CapitalSplitCard below (see that
  // component's docstring) — nothing else on this dashboard depends on it.
  const [psAllocated, setPsAllocated] = useState<number | null>(null);
  const [psAvailable, setPsAvailable] = useState<number | null>(null);
  const [psSplitPct, setPsSplitPct] = useState<number | null>(null);
  const [psError, setPsError] = useState<string | null>(null);
  const [networkCheck, setNetworkCheck] = useState<{ outbound_ip: string | null; note: string } | null>(null);
  const [edisSummary, setEdisSummary] = useState<DhanEdisSummary | null>(null);
  const [edisBusy, setEdisBusy] = useState(false);
  const [networkCheckBusy, setNetworkCheckBusy] = useState(false);
  const [showDhanForm, setShowDhanForm] = useState(false);
  const [nowTick, setNowTick] = useState(() => Date.now());

  // Live Dhan data
  const [livePositions, setLivePositions] = useState<LivePositionRow[]>([]);
  const [liveHoldings, setLiveHoldings] = useState<any[]>([]);
  const [liveDhanOrders, setLiveDhanOrders] = useState<any[]>([]);
  const [liveLoading, setLiveLoading] = useState(false);
  const [cumBrokerage, setCumBrokerage] = useState<CumulativeBrokerage | null>(null);
  const [cumBrokerageError, setCumBrokerageError] = useState<string | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);

  // 2026-09-11 fix — user ask: "if we sell something why it shows here"
  // (in Demat holdings). Dhan's holdings API reflects the broker's demat
  // book, which for a delivery (CNC) sell doesn't drop the quantity until
  // T+1 settlement — so a stock sold this morning correctly still appears
  // in holdings this afternoon, still showing its *unrealized* P&L against
  // the live LTP as if nothing happened, which is what actually looked
  // wrong. This computes today's filled sell qty/avg price per symbol so
  // the holdings table can mark that quantity as sold and show its
  // realized P&L instead of pretending it's still an open position.
  const soldTodayBySymbol = useMemo(() => {
    const map = new Map<string, { qty: number; avgPrice: number }>();
    const FILLED = new Set(["TRADED", "FILLED", "COMPLETE"]);
    const bySymbol = new Map<string, any[]>();
    for (const o of liveDhanOrders) {
      const sym = o.tradingSymbol || o.symbol || "—";
      if (!bySymbol.has(sym)) bySymbol.set(sym, []);
      bySymbol.get(sym)!.push(o);
    }
    for (const [sym, symOrders] of bySymbol.entries()) {
      let qty = 0, value = 0;
      for (const o of symOrders) {
        const side = (o.transactionType || o.side || "").toUpperCase();
        const status = (o.orderStatus || o.status || "").toUpperCase();
        if (side !== "SELL" || !FILLED.has(status)) continue;
        const q = Number(o.quantity || o.tradedQuantity || 0);
        const p = Number(o.averageTradedPrice || o.price || 0);
        if (q <= 0 || p <= 0) continue;
        qty += q;
        value += q * p;
      }
      if (qty > 0) map.set(sym, { qty, avgPrice: value / qty });
    }
    return map;
  }, [liveDhanOrders]);

  // Service positions/orders
  const [positions, setPositions] = useState<Position[]>([]);
  const [orders, setOrders] = useState<OrderRow[]>([]);
  const [orderRange, setOrderRange] = useState<"today" | "all">("today");
  // AUDIT FIX (session64, issue #8 follow-up): split for the Positions tab's
  // two sub-sections — see the "Demat Positions" grouping below.
  const autoPilotPositions = useMemo(() => positions.filter(p => !p.broker_imported), [positions]);
  const dematPositions = useMemo(() => positions.filter(p => p.broker_imported), [positions]);

  const [auditRows, setAuditRows] = useState<AuditLogRow[]>([]);
  const [cycleBusy, setCycleBusy] = useState(false);
  const [afterhoursRunBusy, setAfterhoursRunBusy] = useState(false);
  const [autoPilotBusy, setAutoPilotBusy] = useState(false);
  const [featureBusy, setFeatureBusy] = useState<string | null>(null);
  const [cycleResult, setCycleResult] = useState<CycleResult | null>(null);
  const [pipeline, setPipeline] = useState<PipelineStatus | null>(null);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [candidates, setCandidates] = useState<CandidateRow[]>([]);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [expandedCandidateId, setExpandedCandidateId] = useState<number | null>(null);

  // Editable risk configuration form — separate from `status.risk_config`
  // (the last-saved server value) so typing doesn't fight a background
  // status poll; only synced FROM the server on load/mode-change/save, and
  // only ever sent back on an explicit Save click.
  const [riskForm, setRiskForm] = useState<RiskForm | null>(null);
  // Server-computed fallback values shown as input placeholders when the
  // field above is blank (i.e. the DB row has no override yet) — lets the
  // admin see what's actually in effect right now, not just an empty box.
  const [riskDefaults, setRiskDefaults] = useState<{ min_trade_value: number; min_edge_to_cost_ratio: number }>({
    min_trade_value: 3000, min_edge_to_cost_ratio: 3.0,
  });
  const [riskSaving, setRiskSaving] = useState(false);
  const [riskMsg, setRiskMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // Tick for countdown
  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const secondsRemaining = dhanAccount?.token_expires_at
    ? Math.max(0, Math.floor((new Date(dhanAccount.token_expires_at).getTime() - nowTick) / 1000))
    : null;

  const loadStatus = useCallback(async (m: Mode) => {
    try {
      const s = await realTradeApi.gateStatus(m);
      setStatus(s);
      // A "not armed" error (from a Run Cycle / manual-BUY attempt made
      // before arming) is a snapshot of that moment, not a live-bound
      // banner — it doesn't re-check itself against current state, so it
      // can keep showing after the user goes on to arm successfully. Every
      // successful status poll is a fresher truth than that old toast, so
      // once the gate now says armed, clear it rather than leaving a
      // contradictory "not armed" message next to a green ARMED badge.
      setError(prev => (prev && s.armed && /not armed/i.test(prev)) ? null : prev);
      if (s.risk_config) {
        setRiskForm({
          risk_per_trade_pct: String(s.risk_config.risk_per_trade_pct ?? ""),
          max_daily_loss_pct: String(s.risk_config.max_daily_loss_pct ?? ""),
          max_concurrent_positions: String(s.risk_config.max_concurrent_positions ?? ""),
          max_portfolio_risk_pct: String(s.risk_config.max_portfolio_risk_pct ?? ""),
          stale_data_seconds: String(s.risk_config.stale_data_seconds ?? ""),
          max_tick_volatility_mult: String(s.risk_config.max_tick_volatility_mult ?? ""),
          allow_pyramiding: !!s.risk_config.allow_pyramiding,
          min_trade_value: String(s.risk_config.min_trade_value ?? ""),
          min_edge_to_cost_ratio: String(s.risk_config.min_edge_to_cost_ratio ?? ""),
        });
        setRiskDefaults({
          min_trade_value: s.risk_config.min_trade_value_default ?? 3000,
          min_edge_to_cost_ratio: s.risk_config.min_edge_to_cost_ratio_default ?? 3.0,
        });
      }
      if (m === "REAL" && getSessionToken()) {
        try {
          const d = await realTradeApi.dhanAccount();
          setDhanAccount(d.connected ? d : null);
          if (d.connected) {
            try { setEdisSummary(await realTradeApi.dhanEdisSummary()); }
            catch { setEdisSummary(null); }
          } else {
            setEdisSummary(null);
          }
        } catch { setDhanAccount(null); setEdisSummary(null); }
      } else {
        setDhanAccount(null);
        setEdisSummary(null);
      }
    } catch (e: any) {
      setError(e?.message || "Failed to load status");
    }
  }, []);

  const loadPositionsAndOrders = useCallback(async (m: Mode) => {
    try {
      // 100 (not 20) so the "All" orders sub-tab has real history to show —
      // "Today" is just a client-side filter over this same fetch, not a
      // separate call.
      const [p, o] = await Promise.all([realTradeApi.positions(m), realTradeApi.orders(m, 2)]);
      setPositions(p);
      setOrders(o);
    } catch { /* best-effort */ }
  }, []);

  // Watchlist tab — what the engine has fetched/evaluated recently, with a
  // live price alongside whatever it's waiting on. The tab itself polls
  // every 5s while open (see effect below); this is the one-shot loader
  // also used just to populate the tab count badge on mode change.
  const loadCandidates = useCallback(async (m: Mode) => {
    setCandidatesLoading(true);
    try {
      setCandidates(await realTradeApi.candidates(m, 40));
    } catch { /* best-effort */ }
    finally { setCandidatesLoading(false); }
  }, []);

  const loadLiveDhanData = async () => {
    if (!getSessionToken() || mode !== "REAL") return;
    setLiveLoading(true);
    setLiveError(null);
    try {
      const base = getRealTradeApiUrl();
      const token = getSessionToken();
      const h = { "Content-Type": "application/json", "Authorization": `Bearer ${token}` };
      const [posRes, holdRes, ordRes] = await Promise.all([
        fetch(`${base}/dhan/positions`, { headers: h }).then(r => r.json()),
        fetch(`${base}/dhan/holdings`, { headers: h }).then(r => r.json()),
        fetch(`${base}/dhan/orders`, { headers: h }).then(r => r.json()),
      ]);
      setLivePositions(openLivePositions(posRes));
      setLiveHoldings(holdRes.holdings || []);
      setLiveDhanOrders(ordRes.orders || []);
    } catch (e: any) {
      setLiveError(e?.message || "Failed to load live Dhan data");
    } finally {
      setLiveLoading(false);
    }
  };

  // group 258: brokerage since the first filled order (trade_orders/trade_fills are never purged).
  const loadCumBrokerage = async () => {
    if (!getSessionToken() || mode !== "REAL") return;
    try {
      setCumBrokerage(await realTradeApi.cumulativeBrokerage("REAL", 14));
      setCumBrokerageError(null);
    } catch (e: any) {
      setCumBrokerageError(e?.message || "Failed to load cumulative brokerage");
    }
  };

  // Position Stocks Service's capital ledger + split pct, for the Capital
  // Split card below. Both GET /ledger and GET /status on that service are
  // public reads (no admin session needed there) — this only ever needs
  // position-stocks-service's own URL to be configured somewhere in this
  // browser (its own Settings tab). Best-effort: if that URL isn't set, or
  // the service is unreachable, the card falls back to a computed 50/50
  // reference split instead.
  const loadPositionStocksLedger = async () => {
    if (!getPositionStocksApiUrl()) {
      setPsError("Position Stocks Service URL not configured in this browser.");
      return;
    }
    try {
      const [l, s] = await Promise.all([positionStocksApi.ledger(), positionStocksApi.status()]);
      setPsAllocated(l.total_allocated_capital);
      setPsAvailable(l.available_capital);
      setPsSplitPct(s.scalp_pool_capital_share_pct);
      setPsError(null);
    } catch (e: any) {
      setPsAllocated(null); setPsAvailable(null);
      setPsError(e?.message || "Failed to reach Position Stocks Service");
    }
  };

  useEffect(() => {
    if (getRealTradeApiUrl()) void loadStatus(mode);
  }, [mode, loadStatus]);

  // Registers once: the moment ANY request (including a background poll the
  // user never directly triggered) discovers the session has expired, flip
  // `loggedIn` immediately instead of leaving it stuck true from the last
  // explicit login. See the long comment on setSessionExpiredHandler in
  // realTradeApi.ts for why this used to go stale.
  useEffect(() => {
    setSessionExpiredHandler(() => {
      setLoggedIn(false);
      setError("Session expired — log in again to arm, run a cycle, or change settings. Auto-Pilot (if it was on) keeps running.");
    });
    return () => setSessionExpiredHandler(null);
  }, []);

  useEffect(() => {
    // Item 29 (group102): this used to also load when the gate was merely armed, on the idea that auto-pilot
    // runs with the dashboard logged out. But REAL positions/orders/candidates/pipeline are admin-only on the
    // server (require_admin_if_real), so armed-but-logged-out could only ever produce 401s (and the 2 s / 5 s /
    // 10 s pollers below made that a steady stream). Auto-pilot is unaffected: it runs server-side.
    if (getRealTradeApiUrl() && (mode === "DEMO" || loggedIn)) {
      void loadPositionsAndOrders(mode);
      void loadCandidates(mode); // one-shot, just for the tab count badge — the watchlist tab itself polls
    }
  }, [mode, loggedIn, loadPositionsAndOrders, loadCandidates]);

  useEffect(() => {
    if ((activeTab === "live" || activeTab === "charges") && mode === "REAL" && loggedIn) {
      void loadLiveDhanData();
      void loadPositionStocksLedger();
    }
    if ((activeTab === "charges" || activeTab === "overview") && mode === "REAL" && loggedIn) void loadCumBrokerage();
    if (activeTab === "log") void loadAudit();
  }, [activeTab, mode, loggedIn]);

  // Live pipeline polling — shows what the cycle (manual OR Auto-Pilot,
  // even if triggered while this tab wasn't open) is doing right now: which
  // stage, which symbol, how long each stage took, plus recent history.
  // Polls every 2s while the Pipeline tab is open; stops the moment it
  // isn't, so this never runs up requests in the background.
  useEffect(() => {
    // REAL needs a login session even when armed (admin-only on the server); DEMO is open
    // Also polls while the Positions tab is open (session48 addition) so
    // PipelineLiveStatus can be shown there too — see the Positions tab
    // render block below.
    if ((activeTab !== "pipeline" && activeTab !== "positions") || !getRealTradeApiUrl() || (mode === "REAL" && !loggedIn)) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const p = await realTradeApi.pipelineStatus(mode);
        if (!cancelled) setPipeline(p);
      } catch {
        // best-effort — a failed poll just leaves the last known status showing
      }
    };
    void poll();
    const stop = setVisibleInterval(poll, 2000);
    return () => { cancelled = true; stop(); };
  }, [activeTab, mode, loggedIn]);

  useEffect(() => {
    if (activeTab !== "watchlist" || !getRealTradeApiUrl() || (mode === "REAL" && !loggedIn)) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const c = await realTradeApi.candidates(mode, 40);
        if (!cancelled) setCandidates(c);
      } catch { /* best-effort */ }
    };
    void poll();
    const stop = setVisibleInterval(poll, 5000);
    return () => { cancelled = true; stop(); };
  }, [activeTab, mode, loggedIn]);

  // BUG FIX (this session — "refresh button not working"/positions tab
  // looks frozen): unlike the Pipeline (2s) and Watchlist (5s) tabs above,
  // the Positions tab had NO polling loop at all — it only ever loaded once
  // on mode/login change, relying entirely on the manual ↻ click for any
  // update. Combined with the actionMsg banner bug fixed above, a position
  // whose price/P&L was moving live still looked completely static unless
  // the user kept clicking refresh by hand. Poll every 10s while the tab is
  // open, same "stop the moment it isn't" pattern as the other two tabs.
  useEffect(() => {
    if (activeTab !== "positions" || !getRealTradeApiUrl() || (mode === "REAL" && !loggedIn)) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const [p, o] = await Promise.all([realTradeApi.positions(mode), realTradeApi.orders(mode, 2)]);
        if (!cancelled) { setPositions(p); setOrders(o); }
      } catch { /* best-effort */ }
    };
    const stop = setVisibleInterval(poll, 10_000);
    return () => { cancelled = true; stop(); };
  }, [activeTab, mode, loggedIn]);

  const saveApiUrl = () => { setRealTradeApiUrl(apiUrlInput); void loadStatus(mode); };

  const doLogin = async () => {
    setLoading(true); setError(null);
    try {
      const res = await realTradeApi.login(username, password);
      setSessionToken(res.token); setLoggedIn(true); setPassword("");
      await loadStatus(mode);
    } catch (e: any) { setError(e?.message || "Login failed"); }
    finally { setLoading(false); }
  };

  const doLogout = async () => {
    try { await realTradeApi.logout(); } catch { }
    setSessionToken(null); setLoggedIn(false); void loadStatus(mode);
  };

  const doConnectDhan = async () => {
    setLoading(true); setError(null);
    try {
      await realTradeApi.connectDhan(dhanClientId, dhanToken);
      setDhanToken(""); setDhanClientId(""); setShowDhanForm(false);
      await loadStatus(mode);
    } catch (e: any) { setError(e?.message || "Failed to connect Dhan"); }
    finally { setLoading(false); }
  };

  // Server-side auto-regenerate — only succeeds when DHAN_TOTP_ENABLED=true.
  // Own loading flag so it doesn't freeze the other buttons on this panel;
  // errors (including the expected 409 in manual-paste mode) surface via
  // the existing shared error banner.
  const doRegenerateToken = async () => {
    setRegenLoading(true); setError(null);
    try {
      await realTradeApi.dhanRegenerateToken();
      await loadStatus(mode);
    } catch (e: any) { setError(e?.message || "Failed to regenerate Dhan token"); }
    finally { setRegenLoading(false); }
  };

  const doArm = async () => {
    setLoading(true); setError(null);
    try { await realTradeApi.arm(mode); await loadStatus(mode); }
    catch (e: any) { setError(e?.message || "Failed to arm"); }
    finally { setLoading(false); }
  };

  const doDisarm = async () => {
    try { await realTradeApi.disarm(mode); await loadStatus(mode); }
    catch (e: any) { setError(e?.message || "Failed to disarm"); }
  };

  const doEmergencyPause = async () => {
    if (!window.confirm("Pause ALL trading (DEMO + REAL)? Disarms both modes immediately.")) return;
    try { await realTradeApi.emergencyPause(); await loadStatus(mode); }
    catch (e: any) { setError(e?.message || "Failed to pause"); }
  };

  const doConfirmRisk = async () => {
    try { await realTradeApi.confirmRiskConfig(mode); await loadStatus(mode); }
    catch (e: any) { setError(e?.message || "Failed to confirm risk config"); }
  };

  const doEdisRequestTpin = async () => {
    setEdisBusy(true);
    try {
      const r = await realTradeApi.dhanEdisRequestTpin();
      setError(null);
      alert(r.detail); // one-time SMS prompt — a toast would disappear before they read it
    } catch (e: any) {
      setError(e?.message || "T-PIN request failed");
    } finally { setEdisBusy(false); }
  };

  const doEdisAuthorize = async () => {
    setEdisBusy(true);
    try {
      const html = await realTradeApi.dhanEdisAuthorizeFormHtml();
      // 2026-09-12 fix: this used to inject an <iframe srcdoc="..."> in-page.
      // That can never work — CDSL's own page (edis.cdslindia.com) sends
      // X-Frame-Options / CSP frame-ancestors specifically to refuse being
      // loaded inside anyone else's iframe (anti-clickjacking, protecting
      // the TPIN-entry page). Chrome surfaces that as "edis.cdslindia.com
      // refused to connect" — not a bug in the fetched HTML, a hard block
      // on framing at all. CDSL has to be the top-level document of its
      // own window instead, which is exactly what realTradeApi.ts's own
      // comment on dhanEdisAuthorizeFormHtml already describes: open a
      // blank popup and write the self-submitting form's HTML directly
      // into it. The form's onload JS then redirects that popup itself
      // (top-level navigation inside its own window, not a frame) to
      // CDSL's TPIN page, which CDSL allows.
      const popup = window.open("", "_blank", "width=480,height=720");
      if (!popup) {
        setError(
          "Popup blocked — allow popups for this site in your browser, " +
          "then click '2. Authorize on CDSL' again."
        );
        return;
      }
      popup.document.open();
      popup.document.write(html);
      popup.document.close();
      setError(null);
    } catch (e: any) {
      const msg: string = e?.message || "Could not open CDSL authorization form";
      // session73 fix: "no demat holdings" (backend now returns 409 for
      // this specific case, see main.py) is a normal state — nothing was
      // carried overnight, so there's nothing to authorize today. Showing
      // it as a red error banner is misleading; a plain heads-up is enough.
      if (msg.startsWith("409")) {
        setError(null);
        alert("Nothing to authorize on CDSL today — you currently have no demat holdings.");
      } else {
        setError(msg);
      }
    } finally { setEdisBusy(false); }
  };

  const doNetworkCheck = async () => {
    setNetworkCheckBusy(true);
    try { setNetworkCheck(await realTradeApi.dhanNetworkCheck()); }
    catch (e: any) { setNetworkCheck({ outbound_ip: null, note: e?.message || "Lookup failed" }); }
    finally { setNetworkCheckBusy(false); }
  };

  const doSaveRiskConfig = async () => {
    if (!riskForm) return;
    setRiskSaving(true); setRiskMsg(null);
    try {
      await realTradeApi.updateRiskConfig(mode, {
        risk_per_trade_pct: Number(riskForm.risk_per_trade_pct),
        max_daily_loss_pct: Number(riskForm.max_daily_loss_pct),
        max_concurrent_positions: Number(riskForm.max_concurrent_positions),
        max_portfolio_risk_pct: Number(riskForm.max_portfolio_risk_pct),
        stale_data_seconds: Number(riskForm.stale_data_seconds),
        max_tick_volatility_mult: Number(riskForm.max_tick_volatility_mult),
        allow_pyramiding: riskForm.allow_pyramiding,
        // 2026-09-18 fix (follow-on item #5): blank = "use the default",
        // so only send these when the admin actually typed an override —
        // sending Number("") (NaN) would otherwise wipe a previously-set
        // override back to NaN instead of leaving it alone.
        ...(riskForm.min_trade_value.trim() !== "" ? { min_trade_value: Number(riskForm.min_trade_value) } : {}),
        ...(riskForm.min_edge_to_cost_ratio.trim() !== "" ? { min_edge_to_cost_ratio: Number(riskForm.min_edge_to_cost_ratio) } : {}),
      });
      setRiskMsg({ ok: true, text: "Risk configuration saved." });
      await loadStatus(mode);
    } catch (e: any) {
      setRiskMsg({ ok: false, text: e?.message || "Failed to save risk configuration" });
    } finally { setRiskSaving(false); }
  };

  const loadAudit = async () => {
    try { setAuditRows(await realTradeApi.auditLog(mode, 30)); } catch { }
  };

  const doClosePosition = async (p: Position) => {
    const key = `close:${p.id}`;
    setActionBusy(key); setActionMsg(null);
    try {
      const res = await realTradeApi.closePosition(mode, p.id);
      setActionMsg({ ok: true, text: res.status === "pending_broker_confirmation"
        ? `${p.symbol} close sent to Dhan.`
        : `${p.symbol} closed (P&L ${res.pnl?.toFixed(2)}).` });
      await loadPositionsAndOrders(mode);
    } catch (e: any) { setActionMsg({ ok: false, text: e?.message || `Failed to close ${p.symbol}` }); }
    finally { setActionBusy(null); }
  };

  const doCancelOrder = async (o: OrderRow) => {
    const key = `cancel:${o.id}`;
    setActionBusy(key); setActionMsg(null);
    try {
      await realTradeApi.cancelOrder(mode, o.id);
      setActionMsg({ ok: true, text: `${o.symbol} order cancelled.` });
      await loadPositionsAndOrders(mode);
    } catch (e: any) { setActionMsg({ ok: false, text: e?.message || `Failed to cancel` }); }
    finally { setActionBusy(null); }
  };

  const doReconcile = async () => {
    setActionBusy("reconcile"); setActionMsg(null);
    try {
      const res = await realTradeApi.reconcile(mode);
      let text = res.note || `Checked ${res.checked ?? 0} · filled ${res.entries_filled ?? 0} · exits ${res.exits_confirmed ?? 0}`;
      // 2026-09-04: surface the two new self-heal tallies too — otherwise a
      // cycle that unstuck a PENDING_EXIT position or imported pre-existing
      // Dhan holdings still reads as "nothing happened" (checked 0 · filled
      // 0 · exits 0), which is exactly what prompted this fix in the first
      // place (dashboard showed that banner while ASHOKLEY/ADANIPOWER were
      // silently stuck). Only appended when non-zero, so the common case
      // (nothing to repair) stays exactly as terse as before.
      const extras: string[] = [];
      if (res.positions_unstuck) extras.push(`${res.positions_unstuck} stuck position${res.positions_unstuck === 1 ? "" : "s"} restored`);
      if (res.holdings_imported) extras.push(`${res.holdings_imported} holding${res.holdings_imported === 1 ? "" : "s"} imported`);
      if (extras.length) text += ` · ${extras.join(" · ")}`;
      setActionMsg({ ok: true, text });
      await loadPositionsAndOrders(mode);
    } catch (e: any) { setActionMsg({ ok: false, text: e?.message || "Reconcile failed" }); }
    finally { setActionBusy(null); }
  };

  const doRunCycle = async () => {
    setCycleBusy(true); setError(null);
    try {
      let res;
      try {
        res = await realTradeApi.runCycle(mode);
      } catch (e: any) {
        // group234: the server refuses a REAL cycle outside market hours; let the admin override it explicitly.
        if (mode === "REAL" && /outside market hours/i.test(e?.message || "")
            && window.confirm("The market is closed, so a REAL cycle would run on last-close prices and no order can fill. Run it anyway?")) {
          res = await realTradeApi.runCycle(mode, true);
        } else {
          throw e;
        }
      }
      setCycleResult(res);
      if (res?.timed_out) {
        setError(`Cycle finished with partial results - timed out: ${(res.stage_timeouts || []).join(", ")}`);
      }
      await Promise.all([loadStatus(mode), loadPositionsAndOrders(mode)]);
    } catch (e: any) { setError(e?.message || "Cycle failed"); }
    finally { setCycleBusy(false); }
  };

  const doToggleAutoPilot = async () => {
    setAutoPilotBusy(true); setError(null);
    try {
      if (status?.auto_pilot_enabled) {
        await realTradeApi.disableAutoPilot(mode);
      } else {
        await realTradeApi.enableAutoPilot(mode);
      }
      await loadStatus(mode);
    } catch (e: any) { setError(e?.message || "Auto-Pilot toggle failed"); }
    finally { setAutoPilotBusy(false); }
  };

  // 2026-09-17 (session58, user request): manual "run now" for the
  // after-hours news scan. Server bypasses the enabled toggle and the
  // 15:45–08:45 window for this path (an explicit manual override), so the
  // button works regardless of ON/OFF state above — matches how "Run Cycle"
  // works regardless of Auto-Pilot's own state.
  const doRunAfterhoursScan = async () => {
    setAfterhoursRunBusy(true); setError(null);
    try {
      await realTradeApi.runAfterhoursScanManual(mode);
      await loadStatus(mode);
    } catch (e: any) { setError(e?.message || "After-hours scan failed"); }
    finally { setAfterhoursRunBusy(false); }
  };

  const doToggleFeature = async (
    feature: "prepick" | "enter_at_open" | "eod_squareoff" | "eod_signal_scan" | "afterhours_news_scan" | "overnight_hold",
    currentlyEnabled: boolean,
  ) => {
    setFeatureBusy(feature); setError(null);
    try {
      await realTradeApi.setFeature(mode, feature, !currentlyEnabled);
      await loadStatus(mode);
    } catch (e: any) { setError(e?.message || "Automation toggle failed"); }
    finally { setFeatureBusy(null); }
  };

  // ── No URL configured ────────────────────────────────────────────────────
  if (!getRealTradeApiUrl()) {
    return (
      <div className="p-4 max-w-lg mx-auto">
        <p className="font-display tabular-nums text-xs text-mist mb-1 uppercase tracking-widest">Real Trade Service URL</p>
        <p className="font-display tabular-nums text-[11px] text-mist mb-3">Paste your real-trade-service URL (separate Render deploy from api-gateway).</p>
        <div className="flex gap-2">
          <input
            className="flex-1 bg-graphite border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none focus:border-slate"
            placeholder="https://stockky-real-trade.onrender.com"
            value={apiUrlInput}
            onChange={e => setApiUrlInput(e.target.value)}
          />
          <button onClick={saveApiUrl} className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy">Save</button>
        </div>
      </div>
    );
  }

  // ── Gate computation ─────────────────────────────────────────────────────
  const gate1 = status?.admin_authenticated ?? false;
  const gate2 = mode === "DEMO" ? true : (status?.dhan_connected ?? false);
  const gate3 = status?.risk_config_confirmed ?? false;
  const armed = status?.armed ?? false;

  const nextGateNeeded = !gate1 ? 1 : !gate2 ? 2 : !gate3 ? 3 : armed ? null : 4;

  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "overview", label: "Overview" },
    { id: "board", label: "Board" },
    { id: "live", label: "Live Dhan" },
    { id: "positions", label: "Positions", count: positions.length },
    { id: "orders", label: "Orders", count: orders.length },
    { id: "history", label: "Trade History" },
    { id: "watchlist", label: "Watchlist", count: candidates.length },
    { id: "pipeline", label: "Pipeline" },
    { id: "charges", label: "Charges" },
    { id: "log", label: "Activity" },
  ];

  // design v6: summary tiles + "what the engine is doing" strip. Both only re-state state this page already holds; nothing new is fetched.
  const openPnl = positions.reduce((sum, p) => sum + (p.unrealized_pnl || 0), 0);
  const pnlTone = (n: number): Kpi["tone"] => (Math.abs(n) < 0.5 ? "flat" : n > 0 ? "up" : "down");
  const kpis: Kpi[] = [
    { label: "Open positions", value: positions.length, sub: positions.length ? `${autoPilotPositions.length} by the bot · ${dematPositions.length} demat` : "nothing held", tone: "info" },
    { label: "Open P&L", value: signedInr(openPnl), sub: "unrealised, all shown positions", tone: pnlTone(openPnl) },
    { label: "Orders", value: orders.length, sub: orderRange === "today" ? "today" : "all time", tone: "flat" },
    { label: "Watchlist", value: candidates.length, sub: "candidates being judged", tone: "info" },
  ];
  const pulseWorking = !!pipeline?.running;
  const pulseChips: PulseChip[] = [
    { label: armed ? "Auto-orders armed" : "Auto-orders off", state: armed && mode === "REAL" ? "warn" : "idle" },
    { label: gate1 ? "Logged in" : "Not logged in", state: gate1 ? "ok" : "bad" },
    ...(mode === "REAL" ? [{ label: gate2 ? "Dhan connected" : "Dhan not connected", state: (gate2 ? "ok" : "bad") as PulseChip["state"] }] : []),
  ];
  const pulseLine = pulseWorking
    ? `Scanning${pipeline?.stage ? ` · ${pipeline.stage}` : ""}${pipeline?.symbols_total ? ` · ${pipeline.symbols_done ?? 0} of ${pipeline.symbols_total} symbols` : ""}`
    : armed ? "Armed and waiting for the next cycle" : "Idle · not placing orders";

  return (
    <div className="rt-page v6-page max-w-5xl mx-auto mode-frame" data-mode={mode}
      data-frame-label={mode === "REAL" ? "REAL MONEY · live Dhan orders" : "DEMO · simulated orders, no money"}>
      {/* ── Header bar ───────────────────────────────────────────────────── */}
      <PageHero icon="bot" kicker="AI-assisted · auto trading" title={<>Real Auto Trade{armed && <span className="rt-armed-pill animate-pulse">ARMED</span>}</>}
        subtitle={mode === "REAL" ? "Live Dhan orders" : "Simulated demo orders"}
        right={(
          <div className="rt-seg" role="group" aria-label="Trading mode">
            {(["DEMO", "REAL"] as Mode[]).map(m => (
              <button key={m} type="button" onClick={() => setMode(m)} data-mode={m} data-active={mode === m}
                aria-pressed={mode === m} className="rt-seg-btn">
                <Dot on={mode === m && armed} pulse /> {m}
              </button>
            ))}
          </div>
        )} />

      {/* design v5: plain-words money mode + safety chips (presentation only) */}
      <MoneyModeBanner mode={mode} chips={[{ label: armed ? "Auto-orders armed" : "Auto-orders off", state: armed && mode === "REAL" ? "warn" : "ok" }]} />

      {status != null && (
        <>
          <AiPulse title="Auto-trade engine" working={pulseWorking} line={pulseLine} chips={pulseChips} />
          <KpiStrip items={kpis} label="Auto trade summary" />
        </>
      )}

      {/* Phase notice */}
      <p className="rt-notice font-display tabular-nums">
        Phase 2 active — DEMO fully wired (candidates→risk→fill→exit). REAL places live Dhan orders & reconciles fills.
      </p>

      {/* Error banner */}
      {error && (
        <div className="rt-alert font-display tabular-nums" role="alert">
          <span>{error}</span>
          <button onClick={() => setError(null)} aria-label="Dismiss error">✕</button>
        </div>
      )}

      {/* ── Auth / session bar (REAL only) ──────────────────────────────── */}
      {mode === "REAL" && !loggedIn && !status?.armed ? (
        <div className="rt-login">
          <div className="rt-login-head">
            <span className="rt-login-icon" aria-hidden="true"><NavIcon name="lock" /></span>
            <div className="min-w-0">
              <h3 className="rt-login-title">Admin login required</h3>
              <p className="rt-login-sub">REAL mode places live Dhan orders, so it needs an admin session.</p>
            </div>
          </div>
          <div className="rt-field">
            <label className="rt-label" htmlFor="rt-admin-user">Admin username</label>
            <input id="rt-admin-user" type="text" autoComplete="username" autoCapitalize="none" autoCorrect="off" spellCheck={false}
              placeholder="Admin username" value={username} onChange={e => setUsername(e.target.value)} />
          </div>
          <div className="rt-field">
            <label className="rt-label" htmlFor="rt-admin-pass">Password</label>
            <input id="rt-admin-pass" type="password" autoComplete="current-password"
              placeholder="Password" value={password} onChange={e => setPassword(e.target.value)}
              onKeyDown={e => e.key === "Enter" && void doLogin()} />
          </div>
          <button onClick={() => void doLogin()} disabled={loading || !password} className="rt-btn rt-btn-primary">
            {loading ? "Authenticating…" : "Authenticate"}
          </button>
        </div>
      ) : (
        <>
          {mode === "REAL" && (
            loggedIn ? (
              <div className="rt-session rt-session-ok">
                <span className="font-display tabular-nums">
                  <Dot on={true} /> Admin session active ({username})
                </span>
                <button onClick={() => void doLogout()} className="font-display tabular-nums">
                  Log out
                </button>
              </div>
            ) : (
              <div className="rt-session rt-session-warn">
                <span className="font-display tabular-nums">
                  Session expired — auto-pilot still running. Log in to make changes.
                </span>
                <button onClick={() => setActiveTab("overview")} className="font-display tabular-nums">
                  Log in
                </button>
              </div>
            )
          )}

          <SubTabs items={tabs} value={activeTab} onChange={setActiveTab} label="Auto Trade sections" />

          {/* ═══════════════════════════════════════════════════════════════
              TAB: OVERVIEW
          ═══════════════════════════════════════════════════════════════ */}
          {/* group308: Market Board (display only). Heatmap data comes from position-stocks-service; the highlights below come from THIS
              service's own candidates / positions, so they still show if the heatmap service is down. No order can be placed from here. */}
          {activeTab === "board" && (
            <BoardTab mode={mode} status={status} positions={positions} candidates={candidates} />
          )}

          {activeTab === "overview" && (
            <OverviewTab mode={mode} status={status} loggedIn={loggedIn} loading={loading}
              gate1={gate1} gate2={gate2} gate3={gate3} armed={armed} nextGateNeeded={nextGateNeeded} positions={positions}
              username={username} setUsername={setUsername} password={password} setPassword={setPassword} doLogin={doLogin}
              doConfirmRisk={doConfirmRisk} doArm={doArm} doDisarm={doDisarm} doEmergencyPause={doEmergencyPause}
              showCloseAll={showCloseAll} setShowCloseAll={setShowCloseAll} loadStatus={loadStatus}
              dhanAccount={dhanAccount} secondsRemaining={secondsRemaining} showDhanForm={showDhanForm} setShowDhanForm={setShowDhanForm}
              dhanClientId={dhanClientId} setDhanClientId={setDhanClientId} dhanToken={dhanToken} setDhanToken={setDhanToken}
              doConnectDhan={doConnectDhan} doRegenerateToken={doRegenerateToken} regenLoading={regenLoading}
              edisSummary={edisSummary} edisBusy={edisBusy} doEdisRequestTpin={doEdisRequestTpin} doEdisAuthorize={doEdisAuthorize}
              networkCheck={networkCheck} networkCheckBusy={networkCheckBusy} doNetworkCheck={doNetworkCheck}
              riskForm={riskForm} setRiskForm={setRiskForm} riskDefaults={riskDefaults} riskSaving={riskSaving} riskMsg={riskMsg}
              doSaveRiskConfig={doSaveRiskConfig} cumBrokerage={cumBrokerage} loadPositionsAndOrders={loadPositionsAndOrders} />
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: LIVE DHAN DATA
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "live" && (
            <LiveTab mode={mode} loggedIn={loggedIn} status={status} dhanAccount={dhanAccount}
              livePositions={livePositions} liveHoldings={liveHoldings} liveDhanOrders={liveDhanOrders}
              liveLoading={liveLoading} liveError={liveError} loadLiveDhanData={loadLiveDhanData}
              psSplitPct={psSplitPct} psAllocated={psAllocated} psAvailable={psAvailable} psError={psError}
              soldTodayBySymbol={soldTodayBySymbol} />
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: POSITIONS (Stockky service DB)
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "positions" && (
            <PositionsTab mode={mode} loggedIn={loggedIn} pipeline={pipeline} positions={positions}
              autoPilotPositions={autoPilotPositions} dematPositions={dematPositions} armed={armed}
              actionBusy={actionBusy} actionMsg={actionMsg} setActionMsg={setActionMsg}
              loadPositionsAndOrders={loadPositionsAndOrders} doReconcile={doReconcile} doClosePosition={doClosePosition} />
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: ORDERS
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "orders" && (
            <OrdersTab mode={mode} orders={orders} orderRange={orderRange} setOrderRange={setOrderRange}
              loadPositionsAndOrders={loadPositionsAndOrders} doCancelOrder={doCancelOrder} actionBusy={actionBusy} />
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: WATCHLIST — every candidate the engine has recently
              fetched/evaluated, its latest WAIT/ENTER verdict, the limit
              price it's waiting on (if any), and a live price alongside it.
              This is the piece that used to be invisible between a cycle
              running and an order showing up in Orders/Positions.
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "history" && (
            <HistoryTab mode={mode} loggedIn={loggedIn} />
          )}

          {activeTab === "watchlist" && (
            <WatchlistTab mode={mode} loggedIn={loggedIn} candidates={candidates} candidatesLoading={candidatesLoading}
              expandedCandidateId={expandedCandidateId} setExpandedCandidateId={setExpandedCandidateId} loadCandidates={loadCandidates} />
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: PIPELINE (Run Cycle)
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "pipeline" && (
            <PipelineTab mode={mode} loggedIn={loggedIn} status={status} armed={armed} gate1={gate1}
              pipeline={pipeline} positions={positions} orders={orders} cycleBusy={cycleBusy} cycleResult={cycleResult}
              afterhoursRunBusy={afterhoursRunBusy} autoPilotBusy={autoPilotBusy} featureBusy={featureBusy}
              doRunCycle={doRunCycle} doRunAfterhoursScan={doRunAfterhoursScan} doToggleAutoPilot={doToggleAutoPilot}
              doToggleFeature={doToggleFeature} loadStatus={loadStatus} loadPositionsAndOrders={loadPositionsAndOrders} />
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: DHAN CHARGES
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "charges" && (
            <ChargesTab mode={mode} loggedIn={loggedIn} status={status} orders={orders} liveDhanOrders={liveDhanOrders}
              liveLoading={liveLoading} loadLiveDhanData={loadLiveDhanData} cumBrokerage={cumBrokerage} cumBrokerageError={cumBrokerageError} />
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: ACTIVITY LOG
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "log" && (
            <LogTab mode={mode} auditRows={auditRows} loadAudit={loadAudit} />
          )}
        </>
      )}
    </div>
  );
}