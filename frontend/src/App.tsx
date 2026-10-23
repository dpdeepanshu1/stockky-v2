// frontend/src/App.tsx

import { Suspense, lazy, useState, useEffect, useRef, useCallback } from "react";
import { flushSync } from "react-dom";
import { api, getApiUrl, Decision, ScanResult, wakeService, startSessionKeepAlive, stopSessionKeepAlive } from "./api";
import WatchlistManager from "./components/WatchlistManager";
import SystemCheck from "./components/SystemCheck";
import ServiceManager from "./components/ServiceManager";
import { AdminGate } from "./components/AdminGate"; // group 460: one login for the three admin pages
import ErrorBoundary from "./components/ui/ErrorBoundary";
import { NavIcon } from "./components/ui/NavIcon";
import { PageHeader } from "./components/ui/PageHeader";
import CommandPalette, { CommandAction } from "./components/CommandPalette";
import StockPopup from "./components/board/StockPopup";
import type { BoardRowLite } from "./components/board/boardTypes";
import { useStockkyRealtime, RealtimeMessage } from "./useRealtime";
import { SettingsPage } from "./components/app/SettingsPage";
import { DesktopSidebar, MobileTopBar, MobileDrawer, DesktopTopBar, BackendDownBanner } from "./components/app/AppNav";
import { DashboardView } from "./components/app/DashboardView";
import type { NavItem, Tab, ViewState } from "./components/app/appTypes";

// E31: the heavy pages are separate chunks, fetched when their tab is first opened (the dashboard no longer carries the trading, training and
// calibration pages). They all sit inside the per-tab ErrorBoundary + Suspense below; a failed chunk download shows the boundary's retry, not a blank page.
const Training = lazy(() => import("./components/Training"));
const HotStocks = lazy(() => import("./components/HotStocks"));
const SurpriseStocks = lazy(() => import("./components/SurpriseStocks"));
const IpoTracker = lazy(() => import("./components/IpoTracker"));
const RealAutoTrade = lazy(() => import("./components/RealAutoTrade"));
const PositionStocksTab = lazy(() => import("./components/PositionStocksTab"));
const CalibrationControlTab = lazy(() => import("./components/CalibrationControlTab"));
const DataFeed = lazy(() => import("./components/DataFeed"));
const DataHealthAudit = lazy(() => import("./components/DataHealthAudit"));
const loadTrades = () => import("./components/Trades");          // also called before the open-deposit event, so the page exists to hear it
const Trades = lazy(loadTrades);
const NotificationsPanel = lazy(() => import("./components/NotificationsPanel"));



// The five tabs the mobile bottom bar shows, in order. Explicit so that adding
// a tab to navItems can never silently displace one of them.
const MOBILE_BOTTOM_TABS: Tab[] = ["dashboard", "realtrade", "positionstocks", "hot"];


export default function App() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    try {
      const t = localStorage.getItem("stockky_theme");
      if (t === "light" || t === "dark") return t;
    } catch {}
    // 2026-09-03 Groww-style UI upgrade: default to light (Groww opens
    // light-first). Existing users with a saved preference are unaffected.
    return "light";
  });
  useEffect(() => {
    document.documentElement.classList.toggle("light", theme === "light");
    try { localStorage.setItem("stockky_theme", theme); } catch {}
  }, [theme]);

  // Free-tier: soft keep-alive only while the UI tab is open/visible (every ~4.5 min).
  // Does not run deep service fan-out — avoids overload during scans/schedulers.
  useEffect(() => {
    startSessionKeepAlive();
    return () => stopSessionKeepAlive();
  }, []);

  // Wake every Neon-backed DB (gateway / training / cache) the instant the
  // page loads, so the first real query of the session isn't the one paying
  // Neon's cold-compute latency. Also exposed as a manual "Wake DB" button
  // (dashboard header + Settings tab) via wakeAllDbs() below.
  const [dbWakeState, setDbWakeState] = useState<"idle" | "waking" | "ok" | "error">("idle");
  const wakeAllDbs = useCallback(async () => {
    setDbWakeState("waking");
    // group302: one quiet retry before showing red. The pill used to turn red when the gateway was merely busy
    // (for example right after a restart) even though the database was fine.
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const res = await api.wakeAllDatabases();
        if (res?.ok) {
          setDbWakeState("ok");
          return;
        }
      } catch {
        /* fall through to the retry */
      }
      if (attempt === 0) await new Promise((r) => setTimeout(r, 6000));
    }
    setDbWakeState("error");
  }, []);
  useEffect(() => {
    void wakeAllDbs();
  }, [wakeAllDbs]);

  const [systemReady, setSystemReady] = useState(false);
  const [liteScan, setLiteScan] = useState(true);
  const [tab, setTab] = useState<Tab>("dashboard");
  const [query, setQuery] = useState("");
  const [view, setView] = useState<ViewState>({ mode: "idle" });
  const [signalFilter, setSignalFilter] = useState<"ALL" | "BUY" | "PREPARE" | "HOLD">("ALL");
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [showWatchlist, setShowWatchlist] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showServiceManager, setShowServiceManager] = useState(false);
  const [cmdOpen, setCmdOpen] = useState(false);
  const [boardSym, setBoardSym] = useState<BoardRowLite | null>(null);   // group311: Market Board popup opened from the command palette
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false);

  // Watchlist overlay must not stick when navigating Dashboard / other tabs
  useEffect(() => {
    if (tab !== "watchlist") setShowWatchlist(false);
    setMobileMoreOpen(false);
  }, [tab]);

  // Lock body scroll while mobile more sheet is open
  useEffect(() => {
    if (!mobileMoreOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, [mobileMoreOpen]);

  // Cmd/Ctrl+K command palette
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCmdOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const goTrades = (ev?: Event) => {
      setTab("trades");
      // Optional: ask Trades tab to open deposit panel
      try {
        const detail = (ev as CustomEvent)?.detail;
        if (detail?.openDeposit) {
          const fire = () => window.setTimeout(() => {
            window.dispatchEvent(new CustomEvent("stockky:open-deposit", { detail }));
          }, 150);
          loadTrades().then(fire, fire);          // E31: the Trades page is a lazy chunk; wait for it before asking it to open the panel
        }
      } catch {}
    };
    window.addEventListener("stockky:goto-trades", goTrades as EventListener);
    return () => window.removeEventListener("stockky:goto-trades", goTrades as EventListener);
  }, []);

  // Idle-aware free-tier load control:
  // - Track user activity
  // - After 5 min idle during market hours → one light /ops/idle-tick
  // - When user is active, prefer caches (no extra background work)
  useEffect(() => {
    let lastActive = Date.now();
    let idleSent = false;
    const mark = () => {
      lastActive = Date.now();
      idleSent = false;
    };
    const events = ["pointerdown", "keydown", "scroll", "touchstart", "visibilitychange"] as const;
    events.forEach((e) => window.addEventListener(e, mark, { passive: true }));

    const timer = window.setInterval(async () => {
      try {
        if (document.visibilityState === "hidden") return;
        const idleMs = Date.now() - lastActive;
        if (idleMs < 5 * 60 * 1000 || idleSent) return;
        // Only during local IST market-ish window (browser local may differ; server still gates)
        const res = await fetch(`${getApiUrl().replace(/\/$/, "")}/ops/idle-tick`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        });
        idleSent = true;
        // If off-market server returns ran:false — stay quiet
        if (!res.ok) return;
      } catch {
        /* ignore */
      }
    }, 30000);

    return () => {
      events.forEach((e) => window.removeEventListener(e, mark));
      window.clearInterval(timer);
    };
  }, []);


  const [backendUp, setBackendUp] = useState<"checking" | "up" | "down">("checking");
  const [isWaking, setIsWaking] = useState(false);
  const [scanTaskId, setScanTaskId] = useState<string | null>(null);
  const [pollInterval, setPollInterval] = useState<number | null>(null);
  const [wsLive, setWsLive] = useState(false);
  const [liveScanRows, setLiveScanRows] = useState<Decision[]>([]);
  const [scanTransport, setScanTransport] = useState<"stream" | "poll" | null>(null);
  const pollIntervalRefWs = useRef<number | null>(null);
  const scanCancelledRef = useRef(false);
  const scanAbortRef = useRef<AbortController | null>(null);

  /** Stop all scan polling / session tracking on the client. */
  function clearScanActivity() {
    if (scanAbortRef.current) {
      try { scanAbortRef.current.abort(); } catch {}
      scanAbortRef.current = null;
    }
    if (pollIntervalRefWs.current) {
      window.clearInterval(pollIntervalRefWs.current);
      pollIntervalRefWs.current = null;
    }
    setPollInterval((prev) => {
      if (prev) window.clearInterval(prev);
      return null;
    });
    try { sessionStorage.removeItem("stockky_scan_task_id"); } catch {}
  }

  function buildScanResultFromRows(rows: Decision[], total: number, partial: boolean): ScanResult {
    const actionable = new Set(["BUY NOW", "PREPARE TO BUY", "SELL"]);
    const recommendations = rows.filter((r) => actionable.has(String(r.decision || "")));
    const buy = rows.filter((r) => r.decision === "BUY NOW" || r.decision === "PREPARE TO BUY").length;
    const sell = rows.filter((r) => r.decision === "SELL").length;
    const hold = rows.filter((r) => r.decision === "HOLD").length;
    const cautious = rows.filter((r) => r.decision === "DO NOT BUY" || r.decision === "WAIT").length;
    return {
      scanned: rows.length,
      universe_size: total || rows.length,
      watchlist_size: 0,
      recommendations,
      watchlist_candidates: [],
      verdict: partial
        ? `Partial stream: ${rows.length}/${total || rows.length}`
        : `Stream complete: ${rows.length} symbols`,
      market_mood: buy > sell ? "bullish" : sell > buy ? "bearish" : "mixed",
      market_stats: {
        buy_signals: buy,
        sell_signals: sell,
        hold_signals: hold,
        cautious,
      },
      all_results: rows,
      errors: rows
        .filter((r) => (r as any).error)
        .map((r) => ({ symbol: r.symbol, error: (r as any).error || "ERROR" })),
      ...(partial ? { partial: true, stopped_early: true } as any : {}),
    };
  }


  const handleRealtime = useCallback((msg: RealtimeMessage) => {
    if (msg.type !== "scan_status" || !msg.task_id) return;
    if (scanCancelledRef.current) return;
    if ((msg.status === "done" || msg.status === "cancelled") && msg.result) {
      clearScanActivity();
      setView({ mode: "scan", data: msg.result as any });
      setScanTaskId(null);
      setStatusMessage(msg.status === "cancelled" ? "Scan stopped (live)" : "Scan complete (live)");
      setTimeout(() => setStatusMessage(null), 2500);
    } else if (msg.status === "done" && !msg.result) {
      clearScanActivity();
      setScanTaskId(null);
      setView({ mode: "idle" });
    } else if (msg.status === "running" || msg.processed != null) {
      setView({
        mode: "loading",
        label: `Running market scan... (${msg.processed || 0}/${msg.total || "?"})`,
        progress: {
          processed: Number(msg.processed || 0),
          total: Number(msg.total || 0),
          elapsed: Number(msg.elapsed || 0),
        },
      });
    }
  }, []);

  const { connected: wsConnected, subscribeScan } = useStockkyRealtime(handleRealtime);
  useEffect(() => { setWsLive(wsConnected); }, [wsConnected]);



  // Retry, Status and Last Request tracking
  const [isRetrying, setIsRetrying] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const lastRequestType = useRef<"stock" | "scan" | "watchlist_scan" | null>(null);
  const lastSymbol = useRef<string | null>(null);

  useEffect(() => {
    checkBackend();
    // Refresh should restore last MARKET SCAN, not last single-stock analysis.
    (async () => {
      if (sessionStorage.getItem("stockky_scan_task_id")) return;
      try {
        const raw = localStorage.getItem("stockky_last_scan");
        if (raw) {
          const parsed = JSON.parse(raw);
          if (parsed && (parsed.all_results || parsed.recommendations || parsed.scanned != null)) {
            setView({ mode: "scan", data: parsed });
            return;
          }
        }
      } catch {}
      try {
        const last = await api.getLastScan();
        if (last?.ok && last.result) {
          try {
            localStorage.setItem("stockky_last_scan", JSON.stringify(last.result));
          } catch {}
          setView({ mode: "scan", data: last.result });
        }
      } catch {}
    })();
  }, []);

  // Resumes a scan across a page refresh — without this, reloading mid-scan
  // (or right after it finished, before the tab had a chance to show the
  // result) just loses all state and drops back to idle, even though the
  // backend scan is either still running or already sitting there done.
  useEffect(() => {
    const savedTaskId = sessionStorage.getItem("stockky_scan_task_id");
    if (!savedTaskId) return;
    (async () => {
      try {
        const status = await api.scanStatus(savedTaskId);
        if (status.status === "done") {
          sessionStorage.removeItem("stockky_scan_task_id");
          try {
            if (status.result) localStorage.setItem("stockky_last_scan", JSON.stringify(status.result));
          } catch {}
          setView({ mode: "scan", data: status.result! });
        } else if (status.status === "error") {
          sessionStorage.removeItem("stockky_scan_task_id");
        } else if (status.status === "running") {
          setScanTaskId(savedTaskId);
          setView({
            mode: "loading",
            label: `Running market scan... (${status.processed}/${status.total})`,
            progress: {
              processed: status.processed,
              total: status.total,
              elapsed: status.elapsed,
              estimatedRemaining: status.estimated_remaining ?? undefined,
            },
          });
          const interval = window.setInterval(() => pollScanStatus(savedTaskId), 1000);
          pollIntervalRefWs.current = interval;
          setPollInterval(interval);
        }
      } catch {
        // Task not found (Redis TTL expired, ~1hr) or gateway unreachable
        // right now — either way, nothing to resume, so just drop it
        // rather than keep retrying against a task that's gone.
        sessionStorage.removeItem("stockky_scan_task_id");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [pollInterval]);

  function checkBackend() {
    setBackendUp("checking");
    api
      .ping()
      .then(() => {
        setBackendUp("up");
        api.getWatchlist().then((r) => setWatchlist(r.symbols)).catch(() => {});
      })
      .catch(() => setBackendUp("down"));
  }

  async function handleWakeBackend() {
    setIsWaking(true);
    try {
      await wakeService(getApiUrl());
      setTimeout(() => {
        checkBackend();
        setIsWaking(false);
      }, 3000);
    } catch {
      setIsWaking(false);
      checkBackend();
    }
  }

  async function handleSearch(symbol: string) {
    if (!symbol.trim()) return;
    const sym = symbol.trim().toUpperCase();
    try {
      window.alert(
        `Analysis started for ${sym}\n\nFetching quote, technicals, fundamentals, news & decision. This may take a few seconds.`
      );
    } catch {}
    setStatusMessage(`🟢 Analysis started for ${sym}`);
    setTimeout(() => setStatusMessage(null), 5000);

    setTab("dashboard");
    scanCancelledRef.current = true;
    clearScanActivity();
    lastRequestType.current = "stock";
    lastSymbol.current = symbol.trim();
    setView({ mode: "loading", label: `Analysing ${sym}...` });
    setScanTaskId(null);
    setCancelRequested(false);
    try {
      const data = await api.getStock(symbol.trim());
      try { localStorage.setItem("stockky_last_analysis", JSON.stringify(data)); } catch {}
      setView({ mode: "stock", data });
      setQuery("");
      setStatusMessage(`✅ Analysis ready for ${sym}`);
      setTimeout(() => setStatusMessage(null), 4000);
    } catch (e) {
      setView({ mode: "error", message: (e as Error).message });
      setStatusMessage(`❌ Analysis failed for ${sym}`);
      setTimeout(() => setStatusMessage(null), 5000);
    }
  }

  async function handleScan(liteOverride?: boolean) {
    const useLite = typeof liteOverride === "boolean" ? liteOverride : liteScan;
    if (typeof liteOverride === "boolean") setLiteScan(liteOverride);
    const modeLabel = useLite ? "Lite Market Run" : "Full Market Run";
    try {
      window.alert(
        `${modeLabel} started.\n\nStreaming results as they finish (NDJSON).\nYou can press Stop Scan anytime.`
      );
    } catch {}
    setStatusMessage(useLite ? "🟢 Lite Market Run (live stream)" : "🟢 Full Market Run (live stream)");
    setTimeout(() => setStatusMessage(null), 5000);

    scanCancelledRef.current = false;
    lastRequestType.current = "scan";
    setScanTaskId(null);
    setCancelRequested(false);
    setStoppingScan(false);
    setLiveScanRows([]);
    setScanTransport("stream");
    clearScanActivity();

    const ac = new AbortController();
    scanAbortRef.current = ac;

    setView({
      mode: "loading",
      label: `Streaming ${modeLabel.toLowerCase()}…`,
      progress: { processed: 0, total: 0, elapsed: 0 },
    });

    const rows: Decision[] = [];
    let total = 0;
    let processed = 0;
    const t0 = Date.now();

    try {
      for await (const row of api.scanStream({
        lite: useLite,
        // Full runs must rebuild universe; lite can use cache but still needs full list
        forceRefresh: !useLite,
        signal: ac.signal,
      })) {
        if (scanCancelledRef.current) break;

        if (row && row._meta) {
          if (row.event === "feed_bulk_loaded") {
            total = Number(row.total) || total;
            setView({
              mode: "loading",
              label: `Neon feed ${row.feed_hits ?? "?"}/${total} — streaming symbols…`,
              progress: {
                processed,
                total: total || processed,
                elapsed: (Date.now() - t0) / 1000,
              },
            });
          } else if (row.event === "done" || row.event === "cancelled") {
            processed = Number(row.processed) || processed;
            total = Number(row.total) || total;
          }
          continue;
        }

        // Symbol result line — Step 2: upsert by symbol (no duplicates on re-emit)
        const dec = row as Decision;
        if (dec && dec.symbol) {
          const idx = rows.findIndex((s) => s.symbol === dec.symbol);
          if (idx >= 0) rows[idx] = dec;
          else rows.push(dec);
          processed = Number(row._progress?.processed) || rows.length;
          total = Number(row._progress?.total) || total;
          const elapsed = Number(row._progress?.elapsed) || (Date.now() - t0) / 1000;
          // Progressive live list with upsert + soft cap for UI
          setLiveScanRows((prev) => {
            const i = prev.findIndex((s) => s.symbol === dec.symbol);
            let next: Decision[];
            if (i >= 0) {
              next = [...prev];
              next[i] = dec;
            } else {
              next = [...prev, dec];
            }
            return next.length > 80 ? next.slice(-80) : next;
          });
          setView({
            mode: "loading",
            label: `Live scan ${processed}/${total || "?"} — ${dec.symbol} → ${dec.decision || "?"}`,
            progress: {
              processed,
              total: total || processed,
              elapsed,
              estimatedRemaining:
                processed >= 8 && total > processed && elapsed > 0
                  ? Math.min(
                      6 * 3600, // cap display at 6h so early slow symbols don't show 80h
                      ((total - processed) * elapsed) / processed
                    )
                  : undefined,
            },
          });
        }
      }

      const partial = scanCancelledRef.current;
      // If stream ended early (proxy timeout / free-tier), continue via background scan
      const expected = total || 0;
      const got = rows.length;
      const earlyExit = expected > 40 && got > 0 && got < Math.floor(expected * 0.85);
      if (earlyExit && !scanCancelledRef.current) {
        console.warn(`Stream partial ${got}/${expected} — continuing via /scan/start`);
        setStatusMessage(`Stream reached ${got}/${expected} — continuing full scan in background…`);
        setScanTransport("poll");
        try {
          const { task_id } = await api.scanStart(true, useLite);
          if (task_id) {
            setScanTaskId(task_id);
            try { subscribeScan(task_id); } catch {}
            sessionStorage.setItem("stockky_scan_task_id", task_id);
            const interval = window.setInterval(() => {
              if (scanCancelledRef.current) {
                window.clearInterval(interval);
                return;
              }
              pollScanStatus(task_id);
            }, 1500);
            setPollInterval(interval);
            pollIntervalRefWs.current = interval;
            await pollScanStatus(task_id);
            return;
          }
        } catch (contErr) {
          console.warn("continue scanStart failed", contErr);
        }
      }
      const result = buildScanResultFromRows(rows, total || rows.length, partial || earlyExit);
      try {
        localStorage.setItem("stockky_last_scan", JSON.stringify(result));
      } catch {}
      setLiveScanRows([]);
      setScanTransport(null);
      // BUG FIX: only clear the ref if it's still THIS call's controller.
      // Same race as SurpriseStocks.tsx's fetchSurpriseStocks — if a newer
      // scan superseded this one (clearScanActivity() aborted this call and
      // installed a new controller before this call's own unwind reached
      // here), unconditionally nulling scanAbortRef.current would wipe out
      // the NEW scan's controller, leaving it unabortable.
      if (scanAbortRef.current === ac) scanAbortRef.current = null;
      setView({ mode: "scan", data: result });
      setStatusMessage(
        partial || earlyExit
          ? `⏹ Partial scan ${got || rows.length}/${expected || "?"} — results kept`
          : "✅ Live stream complete"
      );
      setTimeout(() => setStatusMessage(null), 4000);
    } catch (e: any) {
      if (e?.name === "AbortError" || scanCancelledRef.current) {
        const result = buildScanResultFromRows(rows, total || rows.length, true);
        try {
          localStorage.setItem("stockky_last_scan", JSON.stringify(result));
        } catch {}
        setLiveScanRows([]);
        setScanTransport(null);
        // Same guard as the success path above — don't clear a newer call's
        // controller out from under it.
        if (scanAbortRef.current === ac) scanAbortRef.current = null;
        if (rows.length) {
          setView({ mode: "scan", data: result });
          setStatusMessage("⏹ Stream aborted — partial results kept");
        } else {
          setView({ mode: "idle" });
        }
        setTimeout(() => setStatusMessage(null), 3000);
        return;
      }

      // Fallback: classic async task + poll if stream endpoint unavailable
      console.warn("scanStream failed, falling back to /scan/start", e);
      setScanTransport("poll");
      setStatusMessage("Stream unavailable — falling back to background scan…");
      try {
        const { task_id } = await api.scanStart(false, useLite);
        if (scanCancelledRef.current) return;
        setScanTaskId(task_id);
        try { subscribeScan(task_id); } catch {}
        sessionStorage.setItem("stockky_scan_task_id", task_id);
        const interval = window.setInterval(() => {
          if (scanCancelledRef.current) {
            window.clearInterval(interval);
            return;
          }
          pollScanStatus(task_id);
        }, 1000);
        setPollInterval(interval);
        pollIntervalRefWs.current = interval;
        await pollScanStatus(task_id);
      } catch (e2) {
        clearScanActivity();
        setScanTransport(null);
        setView({ mode: "error", message: (e2 as Error).message || (e as Error).message });
      }
    }
  }


  async function handleScanWatchlist() {
    setView({ mode: "loading", label: "Scanning watchlist..." });
    lastRequestType.current = "watchlist_scan";
    setScanTaskId(null);
    if (pollInterval) clearInterval(pollInterval);
    try {
      const data = await api.scanWatchlist();
      setView({ mode: "scan", data });
    } catch (e) {
      setView({ mode: "error", message: (e as Error).message });
    }
  }

  async function pollScanStatus(taskId: string) {
    if (scanCancelledRef.current) return;
    try {
      const status = await api.scanStatus(taskId);
      if (scanCancelledRef.current) return;
      if (status.status === "running") {
        setView({
          mode: "loading",
          label: `Running market scan... (${status.processed}/${status.total})`,
          progress: {
            processed: status.processed,
            total: status.total,
            elapsed: status.elapsed,
            estimatedRemaining: status.estimated_remaining ?? undefined,
          },
        });
      } else if (status.status === "done") {
        if (pollInterval) clearInterval(pollInterval);
        setPollInterval(null);
        setScanTaskId(null);
        sessionStorage.removeItem("stockky_scan_task_id");
        setView({ mode: "scan", data: status.result! });
      } else if (status.status === "error") {
        if (pollInterval) clearInterval(pollInterval);
        setPollInterval(null);
        setScanTaskId(null);
        sessionStorage.removeItem("stockky_scan_task_id");
        setView({ mode: "error", message: status.error || "Scan failed" });
      }
    } catch (e) {
      console.warn("Polling error", e);
    }
  }

  // Requests cancellation and keeps polling — the backend picks up the
  // cancel flag on its next check (every 3rd completed symbol, so this
  // isn't instant) and finalizes the task as "done" with whatever was
  // scored so far. pollScanStatus already knows how to move to the scan
  // results view once status flips to "done", so this doesn't need its
  // own separate handling for that — just marks the UI as "stopping" and
  // lets the normal poll loop pick up the finalized partial result.
  const [stoppingScan, setStoppingScan] = useState(false);
  const [cancelRequested, setCancelRequested] = useState(false);
  async function handleStopScan() {
    // Stream mode: abort fetch; generator path finalizes partial rows
    if (scanTransport === "stream" || scanAbortRef.current) {
      setCancelRequested(true);
      setStoppingScan(true);
      scanCancelledRef.current = true;
      setStatusMessage("⏹ Stopping live stream — keeping symbols scored so far…");
      setView({
        mode: "loading",
        label: "Stopping stream — finalizing partial results…",
        progress: view.mode === "loading" ? view.progress : { processed: 0, total: 0, elapsed: 0 },
      });
      try { scanAbortRef.current?.abort(); } catch {}
      try { await api.stopAllScans(); } catch {}
      return;
    }

    if (!scanTaskId && !cancelRequested) return;
    const taskId = scanTaskId;
    setCancelRequested(true);
    setStoppingScan(true);
    setStatusMessage("⏹ Stopping — keeping symbols scored so far…");
    setView({
      mode: "loading",
      label: "Stopping scan — finalizing partial results…",
      progress: view.mode === "loading" ? view.progress : { processed: 0, total: 0, elapsed: 0 },
    });

    // Request cancel; keep task id so we can poll for partial result
    if (taskId) {

      try {
        await api.scanCancel(taskId);
        // Belt-and-suspenders: signal every running scan on gateway
        try { await api.stopAllScans(); } catch {}
      } catch (e) {
        console.warn("Cancel request failed", e);
      }
      // Poll briefly for done + result (backend builds partial rankings)
      for (let i = 0; i < 45; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        try {
          const status = await api.scanStatus(taskId);
          if (status.status === "done" && status.result) {
            try {
              localStorage.setItem("stockky_last_scan", JSON.stringify(status.result));
            } catch {}
            clearScanActivity();
            setScanTaskId(null);
            setView({ mode: "scan", data: status.result });
            setStatusMessage(
              status.cancelled || status.partial
                ? "⏹ Scan stopped — partial results shown"
                : "Scan complete"
            );
            setStoppingScan(false);
            setCancelRequested(false);
            setTimeout(() => setStatusMessage(null), 4000);
            return;
          }
          if (status.status === "error") break;
          if (status.status === "running") {
            setView({
              mode: "loading",
              label: `Stopping… (${status.processed}/${status.total})`,
              progress: {
                processed: status.processed,
                total: status.total,
                elapsed: status.elapsed,
              },
            });
          }
        } catch {
          /* keep trying */
        }
      }
    }

    // Fallback: try last cached scan from backend
    try {
      const last = await (api as any).getLastScan();
      if (last?.ok && last.result) {
        localStorage.setItem("stockky_last_scan", JSON.stringify(last.result));
        setView({ mode: "scan", data: last.result });
        setStatusMessage("⏹ Stopped — showing last available scan results");
      } else {
        setView({ mode: "idle" });
        setStatusMessage("⏹ Scan stopped (no partial results yet)");
      }
    } catch {
      setView({ mode: "idle" });
      setStatusMessage("⏹ Scan stopped");
    }
    clearScanActivity();
    setScanTaskId(null);
    setStoppingScan(false);
    setCancelRequested(false);
    setTimeout(() => setStatusMessage(null), 4000);
  }

  const handleRetry = async () => {
    if (isRetrying) return;
    setIsRetrying(true);
    setStatusMessage("⏳ Restarting services and retrying...");

    try {
      if (backendUp === "down") {
        await handleWakeBackend();
        await new Promise(r => setTimeout(r, 3000));
      }
      
      if (lastRequestType.current === 'stock' && lastSymbol.current) {
        await handleSearch(lastSymbol.current);
      } else if (lastRequestType.current === 'scan') {
        await handleScan();
      } else if (lastRequestType.current === 'watchlist_scan') {
        await handleScanWatchlist();
      } else {
        setView({ mode: "idle" });
      }
      setStatusMessage("✅ Retry successful");
    } catch (e) {
      setStatusMessage("❌ Retry failed: " + (e as Error).message);
    } finally {
      setIsRetrying(false);
      setTimeout(() => setStatusMessage(null), 4000);
    }
  };

  async function handleWatchlistUpdate(symbols: string[]) {
    await api.setWatchlist(symbols);
    setWatchlist(symbols);
  }

  async function handleAddToWatchlist(symbol: string) {
    try {
      await api.addToWatchlist(symbol);
      const wl = await api.getWatchlist();
      setWatchlist(wl.symbols);
      setStatusMessage(`✅ Added ${symbol} to watchlist`);
      setTimeout(() => setStatusMessage(null), 3000);
    } catch (e) {
      console.error("Failed to add to watchlist", e);
      setStatusMessage(`❌ Failed to add ${symbol} to watchlist`);
      setTimeout(() => setStatusMessage(null), 3000);
    }
  }

  async function handleAddManyToWatchlist(symbols: string[], label: string) {
    if (symbols.length === 0) {
      setStatusMessage("Nothing to add");
      setTimeout(() => setStatusMessage(null), 3000);
      return;
    }
    try {
      const before = new Set(watchlist);
      const newCount = symbols.filter((s) => !before.has(s.toUpperCase())).length;
      await api.addManyToWatchlist(symbols);
      const wl = await api.getWatchlist();
      setWatchlist(wl.symbols);
      setStatusMessage(
        newCount > 0
          ? `✅ Added ${newCount} new symbol(s) from ${label} (${symbols.length - newCount} already on watchlist)`
          : `All ${symbols.length} symbol(s) from ${label} were already on the watchlist`
      );
      setTimeout(() => setStatusMessage(null), 4000);
    } catch (e) {
      console.error(`Failed to add ${label} to watchlist`, e);
      setStatusMessage(`❌ Failed to add ${label} to watchlist`);
      setTimeout(() => setStatusMessage(null), 3000);
    }
  }

  // Telegram handlers
  async function handleSendTopPicks() {
    if (view.mode !== "scan") return;
    const recs = view.data.recommendations || [];
    if (recs.length === 0) {
      setStatusMessage("❌ No recommendations to send");
      setTimeout(() => setStatusMessage(null), 3000);
      return;
    }
    try {
      await api.sendPicksToTelegram({ type: "top5", recommendations: recs });
      setStatusMessage("✅ Top 5 picks sent to Telegram!");
      setTimeout(() => setStatusMessage(null), 3000);
    } catch (e) {
      setStatusMessage("❌ Failed to send: " + (e as Error).message);
      setTimeout(() => setStatusMessage(null), 5000);
    }
  }

  async function handleSendAllActionable() {
    if (view.mode !== "scan") return;
    const all = view.data.all_results || [];
    const actionable = all.filter(r => r.decision === "BUY NOW" || r.decision === "PREPARE TO BUY");
    if (actionable.length === 0) {
      setStatusMessage("❌ No actionable stocks found");
      setTimeout(() => setStatusMessage(null), 3000);
      return;
    }
    try {
      await api.sendPicksToTelegram({ type: "all_actionable", recommendations: actionable });
      setStatusMessage(`✅ ${actionable.length} picks sent to Telegram!`);
      setTimeout(() => setStatusMessage(null), 3000);
    } catch (e) {
      setStatusMessage("❌ Failed to send: " + (e as Error).message);
      setTimeout(() => setStatusMessage(null), 5000);
    }
  }

  if (!systemReady) {
    return <SystemCheck onReady={() => setSystemReady(true)} />;
  }

  const cmdActions: CommandAction[] = [
    { id: "dash", label: "Dashboard", group: "Navigate", hint: "tab", run: () => setTab("dashboard") },
    { id: "datafeed", label: "Data Feed", group: "Navigate", hint: "tab", run: () => setTab("datafeed") },
    { id: "hot", label: "Stockky Hot Picks", group: "Navigate", hint: "tab", run: () => setTab("hot") },
    { id: "surprise", label: "Surprise Momentum", group: "Navigate", hint: "tab", run: () => setTab("surprise") },
    { id: "ipo", label: "IPO Tracker", group: "Navigate", hint: "tab", keywords: "ipo listing new issue gmp", run: () => setTab("ipo") },
    { id: "positionstocks", label: "Position Stocks", group: "Navigate", hint: "tab", keywords: "scalp scalping momentum 5m 15m 60m", run: () => setTab("positionstocks") },
    { id: "calibration", label: "Real Money Calibration", group: "Navigate", hint: "tab", keywords: "switches on off controls tests calibration retrain g1 g2 g3 g4 kronos timesfm daily trades pnl run", run: () => setTab("calibration") },
    { id: "train", label: "Training Lab", group: "Navigate", hint: "tab", run: () => setTab("training") },

    { id: "trades", label: "Trades", group: "Navigate", hint: "tab", run: () => setTab("trades") },
    { id: "alerts", label: "Alerts", group: "Navigate", hint: "tab", run: () => setTab("notifications") },
    { id: "wl", label: "Watchlist", group: "Navigate", hint: "tab", run: () => setTab("watchlist") },
    { id: "settings", label: "Settings", group: "Navigate", hint: "tab", run: () => setTab("settings") },
    { id: "ratelimits", label: "Rate limit monitor", group: "Navigate", hint: "Settings", run: () => setTab("settings") },
    { id: "scan-wl", label: "Scan Watchlist", group: "Action", keywords: "scan", run: () => { setTab("dashboard"); handleScanWatchlist(); } },
    ...(watchlist || []).slice(0, 30).map((s) => ({
      id: `sym-${s}`,
      label: s,
      group: "Watchlist",
      keywords: "stock analyse",
      run: () => { setTab("dashboard"); handleSearch(s); },
    })),
  ];

  // Grouped by what the person is doing, not by which service backs the tab.
  const navItems: NavItem[] = [
    { id: "dashboard", label: "Dashboard", short: "Home", icon: "home", group: "" },
    { id: "realtrade", label: "Auto Trade", short: "Auto Trade", icon: "bot", group: "Trading" },
    { id: "positionstocks", label: "Position Stocks", short: "Scalp", icon: "trend", group: "Trading" },
    { id: "calibration", label: "Real Money Calibration", short: "Calibrate", icon: "sliders", group: "Trading" },
    { id: "trades", label: "Paper Trades", short: "Paper", icon: "swap", group: "Trading" },
    { id: "hot", label: "Hot Picks", short: "Picks", icon: "flame", group: "Discover" },
    { id: "surprise", label: "Surprise Momentum", short: "Surprise", icon: "zap", group: "Discover" },
    { id: "ipo", label: "IPO Tracker", short: "IPO", icon: "tag", group: "Discover" },
    { id: "watchlist", label: "Watchlist", short: "Watchlist", icon: "star", group: "Discover" },
    { id: "notifications", label: "Alerts", short: "Alerts", icon: "bell", group: "System" },
    { id: "datafeed", label: "Data Feed", short: "Feed", icon: "database", group: "System" },
    { id: "training", label: "Training Lab", short: "Train", icon: "flask", group: "System" },
    { id: "settings", label: "Settings", short: "Settings", icon: "sliders", group: "System" },
  ];
  const goTo = (id: Tab) => {
    const apply = () => {
      setTab(id);
      if (id === "watchlist") setShowWatchlist(true);
      else setShowWatchlist(false);
      if (id !== "settings") setShowSettings(false);
      setMobileMoreOpen(false);
    };
    // group 460: soft cross-fade where the View Transitions API exists; off for reduced-motion and TV mode
    try {
      const vt = (document as any).startViewTransition;
      const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches || document.documentElement.classList.contains("tv");
      if (typeof vt === "function" && !calm) { vt.call(document, () => flushSync(apply)); return; }
    } catch { /* fall through to the plain switch */ }
    apply();
  };
  const themeLabel = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";


  return (
    <div className="min-h-screen bg-ink text-paper relative terminal-shell terminal-premium">
      <CommandPalette open={cmdOpen} onClose={() => setCmdOpen(false)} actions={cmdActions}
        symbolAction={(sym) => ({ id: `board-${sym}`, label: `${sym} — zones, levels, R:R`, group: "Market Board", hint: "popup", run: () => setBoardSym({ symbol: sym }) })} />
      <StockPopup row={boardSym} onClose={() => setBoardSym(null)} reduced={false} />
      <a className="skip-link" href="#main-content">Skip to content</a>
      {statusMessage && (
        <div role="status" aria-live="polite" className="app-toast fixed top-4 right-4 z-50 bg-graphite border border-signal-buy/40 rounded-lg px-4 py-2.5 shadow-2xl animate-fadeIn flex items-center gap-2">
          <p className="font-mono text-sm text-paper">{statusMessage}</p>
        </div>
      )}

      {/* Desktop sidebar */}
      <DesktopSidebar navItems={navItems} tab={tab} goTo={goTo} showServiceManager={showServiceManager}
        setShowServiceManager={setShowServiceManager} setCmdOpen={setCmdOpen} />

      {/* Mobile top bar */}
      <MobileTopBar navItems={navItems} tab={tab} mobileMoreOpen={mobileMoreOpen} setMobileMoreOpen={setMobileMoreOpen} backendUp={backendUp}
        wsLive={wsLive} handleWakeBackend={handleWakeBackend} setTab={setTab} setCmdOpen={setCmdOpen} />

      {/* Mobile slide-over menu (professional drawer) */}
      {mobileMoreOpen && (
        <MobileDrawer navItems={navItems} tab={tab} goTo={goTo} setMobileMoreOpen={setMobileMoreOpen} setShowServiceManager={setShowServiceManager}
          wakeAllDbs={wakeAllDbs} dbWakeState={dbWakeState} theme={theme} setTheme={setTheme} />
      )}

      {/* Desktop top bar: search + system status */}
      <DesktopTopBar setCmdOpen={setCmdOpen} wsLive={wsLive} backendUp={backendUp} handleWakeBackend={handleWakeBackend} setTab={setTab}
        dbWakeState={dbWakeState} wakeAllDbs={wakeAllDbs} themeLabel={themeLabel} theme={theme} setTheme={setTheme} />

      {showServiceManager && (
        <ServiceManager onClose={() => setShowServiceManager(false)} />
      )}

      {backendUp === "down" && !showSettings && (
        <BackendDownBanner isWaking={isWaking} handleWakeBackend={handleWakeBackend} checkBackend={checkBackend} setShowSettings={setShowSettings} />
      )}

      {showWatchlist && tab === "dashboard" && (
        <div className="border-b border-slate/60 bg-graphite">
          <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6">
            <WatchlistManager
              symbols={watchlist}
              onChange={handleWatchlistUpdate}
              onAnalyse={(s) => {
                setShowWatchlist(false);
                handleSearch(s);
              }}
              onScanWatchlist={() => {
                setShowWatchlist(false);
                handleScanWatchlist();
              }}
            />
          </div>
        </div>
      )}

      <main id="main-content" tabIndex={-1} className="terminal-main max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-8 pb-24 md:pb-8">
        <ErrorBoundary key={tab} label="This page">
        <Suspense fallback={<div className="py-16 flex justify-center" role="status" aria-label="Loading page"><span className="inline-block w-5 h-5 rounded-full border-2 border-mist border-t-transparent animate-spin" /></div>}>
        {tab === "notifications" ? (
          <div className="page-terminal">
            <PageHeader title="Alerts" desc="Notifications from the scanners and trading services, newest first." />
            <NotificationsPanel />
          </div>
        ) : tab === "training" ? (
          <div className="page-terminal">
            <PageHeader title="Training Lab" desc="Train, evaluate and monitor the prediction models behind the signals." />
            <Training />
          </div>
        ) : tab === "trades" ? (
          <div className="page-terminal">
            <PageHeader title="Paper Trades" desc="Practice trades with a simulated wallet. No real money is used here." />
            <Trades />
          </div>
        ) : tab === "datafeed" ? (
          <div className="page-terminal">
            <PageHeader title="Data Feed" desc="Market data coverage and freshness across the stocks Stockky tracks." />
            <DataFeed />
            <DataHealthAudit />
          </div>
        ) : tab === "hot" ? (
          <div className="page-terminal">
            <PageHeader title="Hot Picks" desc="Stocks currently ranked highest by the combined technical, fundamental and news score." />
            <HotStocks
              onAnalyze={(s) => {
                setTab("dashboard");
                handleSearch(s);
              }}
            />
          </div>
        ) : tab === "surprise" ? (
          <div className="page-terminal">
            <PageHeader title="Surprise Momentum" desc="Stocks making unusual moves on volume, news or price right now." />
            <SurpriseStocks
              onSelect={(s) => {
                setTab("dashboard");
                handleSearch(s);
              }}
            />
          </div>
        ) : tab === "ipo" ? (
          <div className="page-terminal">
            <PageHeader title="IPO Tracker" desc="Upcoming, open and recently listed IPOs." />
            <IpoTracker
              onSelect={(s) => {
                setTab("dashboard");
                handleSearch(s);
              }}
            />
          </div>
        ) : tab === "realtrade" ? (
          <AdminGate title="Auto Trade" onExit={() => setTab("dashboard")}>
            <div className="page-terminal">
              <PageHeader title="Auto Trade" desc="Automated trading with your broker account. Switch between simulated (DEMO) and live (REAL) mode below." />
              <RealAutoTrade />
            </div>
          </AdminGate>
        ) : tab === "positionstocks" ? (
          <AdminGate title="Position Stocks" onExit={() => setTab("dashboard")}>
            <div className="page-terminal">
              <PageHeader title="Position Stocks" desc="Fast momentum trades that open and close within the day, using their own capital pool." />
              <PositionStocksTab />
            </div>
          </AdminGate>
        ) : tab === "calibration" ? (
          <AdminGate title="Real Money Calibration" onExit={() => setTab("dashboard")}>
            <div className="page-terminal">
              <PageHeader title="Real Money Calibration" desc="Switch the real-money rules ON / OFF, see daily trades with entry and exit time, and run every test, calibration and training job from here." />
              <ErrorBoundary label="Real Money Calibration"><CalibrationControlTab /></ErrorBoundary>
            </div>
          </AdminGate>
        ) : tab === "settings" ? (
          <SettingsPage
            backendUp={backendUp}
            onSaved={checkBackend}
            theme={theme}
            setTheme={setTheme}
            onOpenServices={() => setShowServiceManager(true)}
          />
        ) : tab === "watchlist" ? (
          <div className="page-terminal">
            <PageHeader title="Watchlist" desc="The stocks you follow. Scan them together or open one for a full analysis." />
            <WatchlistManager
              symbols={watchlist}
              onChange={handleWatchlistUpdate}
              onAnalyse={(s) => {
                setTab("dashboard");
                handleSearch(s);
              }}
              onScanWatchlist={() => {
                setTab("dashboard");
                handleScanWatchlist();
              }}
            />
          </div>
        ) : (
          <DashboardView view={view} setView={setView} query={query} setQuery={setQuery} watchlist={watchlist}
            handleSearch={handleSearch} handleScan={handleScan} handleStopScan={handleStopScan} handleRetry={handleRetry}
            handleAddToWatchlist={handleAddToWatchlist} handleAddManyToWatchlist={handleAddManyToWatchlist}
            handleSendTopPicks={handleSendTopPicks} handleSendAllActionable={handleSendAllActionable}
            liveScanRows={liveScanRows} scanTransport={scanTransport} scanTaskId={scanTaskId} isRetrying={isRetrying}
            cancelRequested={cancelRequested} stoppingScan={stoppingScan} wsLive={wsLive} signalFilter={signalFilter}
            setSignalFilter={setSignalFilter} setShowSettings={setShowSettings} />
        )}
        </Suspense>
        </ErrorBoundary>
      </main>

      
      {/* Mobile bottom navigation */}
      <nav className="terminal-bottom-nav md:hidden" aria-label="Primary">
        {/* Pinned to the same five tabs this bar has always shown. Adding
            "ipo" to navItems would otherwise have silently pushed "training"
            out of a blind .slice(0, 5); IPO Tracker is reachable from the
            desktop rail, the ⋯ More drawer and the command palette. */}
        {MOBILE_BOTTOM_TABS.map((id) => navItems.find((i) => i.id === id)!).map((item) => (
          <button
            key={item.id}
            type="button"
            className={tab === item.id ? "active" : ""}
            aria-current={tab === item.id ? "page" : undefined}
            onClick={() => goTo(item.id)}
          >
            <NavIcon name={item.icon} />
            <span>{item.short}</span>
          </button>
        ))}
        <button
          type="button"
          className={mobileMoreOpen || !MOBILE_BOTTOM_TABS.includes(tab) ? "active" : ""}
          onClick={() => setMobileMoreOpen(true)}
        >
          <NavIcon name="menu" />
          <span>More</span>
        </button>
      </nav>

      <footer className="app-footer px-4 sm:px-6 py-5 border-t border-slate/40 mt-8 pb-20 md:pb-5">
        <p className="text-[11px] text-mist/70">
          Stockky is for information only and is not investment advice. Verify prices and signals before you trade.
        </p>
      </footer>
    </div>
  );
}
