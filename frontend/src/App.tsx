// frontend/src/App.tsx

import { useState, useEffect, useRef, useCallback } from "react";
import { api, getApiUrl, setApiUrl, Decision, ScanResult, wakeService } from "./api";
import Pipeline from "./components/Pipeline";
import DecisionCard, { HorizonStrip } from "./components/DecisionCard";
import ScanPanel, { MultiHorizonScanLists } from "./components/ScanPanel";
import WatchlistManager from "./components/WatchlistManager";
import NotificationsPanel from "./components/NotificationsPanel";
import SystemCheck from "./components/SystemCheck";
import MarketMovers from "./components/MarketMovers";
import ServiceManager from "./components/ServiceManager";
import Training from "./components/Training";
import HotStocks from "./components/HotStocks";
import Trades from "./components/Trades";
// ── NEW: import Market Sentiment Header ──
import MarketSentimentHeader from "./components/MarketSentimentHeader";
import CommandPalette, { CommandAction } from "./components/CommandPalette";
import { useStockkyRealtime, RealtimeMessage } from "./useRealtime";

type ViewState =
  | { mode: "idle" }
  | { mode: "loading"; label: string; progress?: { processed: number; total: number; elapsed: number; estimatedRemaining?: number } }
  | { mode: "stock"; data: Decision }
  | { mode: "scan"; data: ScanResult }
  | { mode: "error"; message: string };

type Tab = "dashboard" | "notifications" | "training" | "trades" | "hot" | "settings" | "watchlist";

export default function App() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    try {
      const t = localStorage.getItem("stockky_theme");
      if (t === "light" || t === "dark") return t;
    } catch {}
    return "dark";
  });
  useEffect(() => {
    document.documentElement.classList.toggle("light", theme === "light");
    try { localStorage.setItem("stockky_theme", theme); } catch {}
  }, [theme]);

  const [systemReady, setSystemReady] = useState(false);
  const [liteScan, setLiteScan] = useState(true);
  const [tab, setTab] = useState<Tab>("dashboard");
  const [query, setQuery] = useState("");
  const [view, setView] = useState<ViewState>({ mode: "idle" });
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [showWatchlist, setShowWatchlist] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showServiceManager, setShowServiceManager] = useState(false);
  const [cmdOpen, setCmdOpen] = useState(false);

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
    const goTrades = () => setTab("trades");
    window.addEventListener("stockky:goto-trades", goTrades as EventListener);
    return () => window.removeEventListener("stockky:goto-trades", goTrades as EventListener);
  }, []);

  const [backendUp, setBackendUp] = useState<"checking" | "up" | "down">("checking");
  const [isWaking, setIsWaking] = useState(false);
  const [scanTaskId, setScanTaskId] = useState<string | null>(null);
  const [pollInterval, setPollInterval] = useState<number | null>(null);
  const [wsLive, setWsLive] = useState(false);
  const pollIntervalRefWs = useRef<number | null>(null);

  const handleRealtime = useCallback((msg: RealtimeMessage) => {
    if (msg.type !== "scan_status" || !msg.task_id) return;
    if (msg.status === "done" && msg.result) {
      setView({ mode: "scan", data: msg.result as any });
      setScanTaskId(null);
      if (pollIntervalRefWs.current) {
        window.clearInterval(pollIntervalRefWs.current);
        pollIntervalRefWs.current = null;
      }
      try { localStorage.removeItem("stockky_scan_task"); } catch {}
      setStatusMessage("Scan complete (live)");
      setTimeout(() => setStatusMessage(null), 2500);
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
    try {
      const raw = localStorage.getItem("stockky_last_analysis");
      if (raw && view.mode === "idle") {
        // restore last analysis on refresh without wiping scan resume logic
        const parsed = JSON.parse(raw);
        if (parsed && parsed.symbol) {
          // only restore if no scan task in flight
          if (!sessionStorage.getItem("stockky_scan_task_id")) {
            setView({ mode: "stock", data: parsed });
          }
        }
      }
    } catch {}
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
    setTab("dashboard");
    lastRequestType.current = "stock";
    lastSymbol.current = symbol.trim();
    setView({ mode: "loading", label: `Analysing ${symbol.toUpperCase()}...` });
    if (pollInterval) clearInterval(pollInterval);
    setScanTaskId(null);
    try {
      const data = await api.getStock(symbol.trim());
      try { localStorage.setItem("stockky_last_analysis", JSON.stringify(data)); } catch {}
      setView({ mode: "stock", data });
      setQuery("");
    } catch (e) {
      setView({ mode: "error", message: (e as Error).message });
    }
  }

  async function handleScan() {
    setView({ mode: "loading", label: "Starting market scan..." });
    lastRequestType.current = "scan";
    setScanTaskId(null);
    setCancelRequested(false);
    if (pollInterval) clearInterval(pollInterval);
    try {
      const { task_id } = await api.scanStart(false, liteScan);
      setScanTaskId(task_id);
      try { subscribeScan(task_id); } catch {}
      sessionStorage.setItem("stockky_scan_task_id", task_id);
      const interval = window.setInterval(() => pollScanStatus(task_id), 1000);
      setPollInterval(interval);
      pollIntervalRefWs.current = interval;
      await pollScanStatus(task_id);
    } catch (e) {
      if (scanTaskId) {
        const interval = window.setInterval(() => pollScanStatus(scanTaskId), 1000);
        setPollInterval(interval);
        pollScanStatus(scanTaskId);
      } else {
        setView({ mode: "error", message: (e as Error).message });
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
    try {
      const status = await api.scanStatus(taskId);
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
    if (!scanTaskId) return;
    setStoppingScan(true);
    setCancelRequested(true);
    try {
      const result = await api.scanCancel(scanTaskId);
      setStatusMessage(
        `Stopping — finishing up (${result.processed_so_far ?? "?"}/${result.total ?? "?"} scanned so far)...`
      );
    } catch (e) {
      console.warn("Cancel request failed", e);
      setStatusMessage(`Could not stop the scan: ${(e as Error).message || "unknown error"} — it may finish on its own.`);
      setCancelRequested(false);
    } finally {
      setStoppingScan(false);
    }
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

  function formatTime(seconds: number): string {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
  }

  const cmdActions: CommandAction[] = [
    { id: "dash", label: "Dashboard", group: "Navigate", hint: "tab", run: () => setTab("dashboard") },
    { id: "hot", label: "Stockky Hot Picks", group: "Navigate", hint: "tab", run: () => setTab("hot") },
    { id: "train", label: "Training Lab", group: "Navigate", hint: "tab", run: () => setTab("training") },
    { id: "trades", label: "Trades", group: "Navigate", hint: "tab", run: () => setTab("trades") },
    { id: "alerts", label: "Alerts", group: "Navigate", hint: "tab", run: () => setTab("notifications") },
    { id: "wl", label: "Watchlist", group: "Navigate", hint: "tab", run: () => setTab("watchlist") },
    { id: "settings", label: "Settings", group: "Navigate", hint: "tab", run: () => setTab("settings") },
    { id: "scan-wl", label: "Scan Watchlist", group: "Action", keywords: "scan", run: () => { setTab("dashboard"); handleScanWatchlist(); } },
    ...(watchlist || []).slice(0, 30).map((s) => ({
      id: `sym-${s}`,
      label: s,
      group: "Watchlist",
      keywords: "stock analyse",
      run: () => { setTab("dashboard"); handleSearch(s); },
    })),
  ];

  const navItems: { id: Tab; label: string; short: string }[] = [
    { id: "dashboard", label: "Dashboard", short: "Home" },
    { id: "hot", label: "Hot Picks", short: "Picks" },
    { id: "training", label: "Training", short: "Train" },
    { id: "trades", label: "Trades", short: "Trade" },
    { id: "notifications", label: "Alerts", short: "Alerts" },
    { id: "watchlist", label: "Watchlist", short: "List" },
    { id: "settings", label: "Settings", short: "Set" },
  ];

  return (
    <div className="min-h-screen bg-ink text-paper relative terminal-shell">
      <CommandPalette open={cmdOpen} onClose={() => setCmdOpen(false)} actions={cmdActions} />
      {statusMessage && (
        <div className="fixed top-4 right-4 z-50 bg-graphite border border-signal-buy/40 rounded-lg px-4 py-2.5 shadow-2xl animate-fadeIn flex items-center gap-2">
          <p className="font-mono text-sm text-paper">{statusMessage}</p>
        </div>
      )}

      {/* Desktop sidebar */}
      <aside className="terminal-sidebar hidden md:flex">
        <div className="sidebar-brand">
          <span className="font-display text-lg tracking-tight">Stockky</span>
          <span className="mono text-[9px] text-mist tracking-widest uppercase">NSE · AI</span>
          <span className={`mono text-[9px] ${wsLive ? "text-signal-buy" : "text-mist/50"}`}>
            {wsLive ? "● LIVE" : "○ polling"}
          </span>
        </div>
        <nav className="sidebar-nav">
          {navItems.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`sidebar-link ${tab === item.id ? "active" : ""}`}
              onClick={() => {
                setTab(item.id);
                if (item.id === "watchlist") setShowWatchlist(true);
                if (item.id !== "settings") setShowSettings(false);
              }}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <BackendStatusDot status={backendUp} onClick={() => setTab("settings")} />
          <button
            type="button"
            className="sidebar-link"
            onClick={() => setCmdOpen(true)}
            title="Command palette"
          >
            <span className="kbd">⌘K</span> Command
          </button>
          <button
            type="button"
            className="sidebar-link"
            onClick={() => setShowServiceManager(!showServiceManager)}
          >
            Services
          </button>
          <button
            type="button"
            className="sidebar-link theme-toggle"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            title="Toggle dark / light"
            aria-label="Toggle color theme"
          >
            {theme === "dark" ? "☀ Light mode" : "☾ Dark mode"}
          </button>
        </div>
      </aside>

      {/* Mobile top bar */}
      <header className="terminal-topbar md:hidden">
        <span className="font-display text-base">Stockky</span>
        <div className="flex items-center gap-2">
          <BackendStatusDot status={backendUp} onClick={() => setTab("settings")} />
          <button type="button" className="btn-terminal text-[10px]" onClick={() => setCmdOpen(true)}>
            ⌘K
          </button>
        </div>
      </header>

      {showServiceManager && (
        <ServiceManager onClose={() => setShowServiceManager(false)} />
      )}

      {backendUp === "down" && !showSettings && (
        <div className="border-b border-signal-sell/30 bg-signal-sell/5">
          <div className="max-w-6xl mx-auto px-4 sm:px-6 py-3 flex items-center justify-between gap-3 flex-wrap">
            <p className="font-mono text-xs text-signal-sell">
              Can't reach the backend at <span className="text-signal-sell/80">{getApiUrl() || "(not set)"}</span>.
            </p>
            <div className="flex gap-3">
              <button
                onClick={handleWakeBackend}
                disabled={isWaking}
                className="font-mono text-xs text-paper bg-signal-prepare/20 border border-signal-prepare/40 rounded-lg px-4 py-1.5 hover:bg-signal-prepare/30 transition disabled:opacity-50"
              >
                {isWaking ? "Waking..." : "Wake Backend"}
              </button>
              <button onClick={checkBackend} className="font-mono text-xs text-mist hover:text-paper underline">
                Retry
              </button>
              <button
                onClick={() => setShowSettings(true)}
                className="font-mono text-xs text-signal-sell hover:text-paper underline"
              >
                Fix in Settings
              </button>
            </div>
          </div>
        </div>
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

      <main className="terminal-main max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-8 pb-24 md:pb-8">
        {tab === "notifications" ? (
          <div className="page-terminal">
            <p className="dash-section-title">Alerts</p>
            <NotificationsPanel />
          </div>
        ) : tab === "training" ? (
          <div className="page-terminal">
            <p className="dash-section-title">Training Lab</p>
            <Training />
          </div>
        ) : tab === "trades" ? (
          <div className="page-terminal">
            <p className="dash-section-title">Paper Trades</p>
            <Trades />
          </div>
        ) : tab === "hot" ? (
          <HotStocks />
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
            <p className="dash-section-title">Watchlist</p>
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
          <>
            {/* Dashboard content — terminal-style */}
            <section className="mb-8 sm:mb-10 dash-terminal">
              <div className="dash-hero">
              <p className="dash-section-title">Market terminal</p>
              <MarketClock />
              <h1>
                Know your next move. <span>In one call.</span>
              </h1>
              <p>
                Technical, fundamental, news and AI signals — combined into a single decision.
                Press <span className="kbd">Ctrl</span>+<span className="kbd">K</span> to jump anywhere.
              </p>

              <div className="dash-search-row">
                <div className="dash-search">
                  <span className="font-mono text-mist text-xs select-none">NSE:</span>
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value.toUpperCase())}
                    onKeyDown={(e) => e.key === "Enter" && handleSearch(query)}
                    placeholder="TCS, INFY, RELIANCE..."
                    autoComplete="off"
                    spellCheck={false}
                  />
                  {query && (
                    <button
                      onClick={() => handleSearch(query)}
                      className="btn-terminal shrink-0"
                    >
                      Analyse
                    </button>
                  )}
                </div>
                <label className="mono text-[10px] text-mist/70 flex items-center gap-1.5 cursor-pointer whitespace-nowrap">
                  <input
                    type="checkbox"
                    checked={liteScan}
                    onChange={(e) => setLiteScan(e.target.checked)}
                    className="rounded border-slate"
                  />
                  Lite
                </label>
                <button
                  onClick={handleScan}
                  className="btn-terminal whitespace-nowrap"
                  title={liteScan ? "Lite scan: faster, less enrichment" : "Full scan: all pillars + summaries"}
                >
                  {liteScan ? "Run lite scan" : "Run full scan"}
                </button>
              </div>
              </div>

              {/* ── NEW: Market Sentiment Header placed above Market Movers ── */}
              <div className="mt-8">
                <MarketSentimentHeader />
              </div>

              <div className="mt-8">
                <MarketMovers onSelect={handleSearch} />
              </div>

              {watchlist.length > 0 && view.mode === "idle" && (
                <div className="flex flex-wrap gap-2 mt-4">
                  {watchlist.slice(0, 10).map((s) => (
                    <button
                      key={s}
                      onClick={() => handleSearch(s)}
                      className="font-mono text-[11px] text-mist hover:text-paper border border-slate/60 hover:border-mist/60 rounded-md px-2.5 py-1 transition"
                    >
                      {s}
                    </button>
                  ))}
                  {watchlist.length > 10 && (
                    <span className="font-mono text-[11px] text-mist/40 py-1">
                      +{watchlist.length - 10} more
                    </span>
                  )}
                </div>
              )}
            </section>

            <section>
              {view.mode === "idle" && (
                <div className="border border-dashed border-slate rounded-xl p-10 sm:p-16 text-center">
                  <p className="text-mist/40 font-mono text-xs">
                    Search a symbol or run the scanner to begin.
                  </p>
                </div>
              )}

              {view.mode === "loading" && (
                <div className="rounded-xl border border-slate bg-graphite p-8 max-w-sm">
                  <p className="font-mono text-xs text-mist mb-2">{view.label}</p>
                  {view.progress && (
                    <div className="mt-4 space-y-2">
                      <div className="flex justify-between font-mono text-[11px] text-mist/60">
                        <span>Processed: {view.progress.processed}/{view.progress.total}</span>
                        <span>⏱️ {formatTime(view.progress.elapsed)}</span>
                      </div>
                      <div className="w-full h-1 bg-slate rounded-full overflow-hidden">
                        <div
                          className="h-full bg-signal-prepare transition-all duration-500"
                          style={{ width: `${Math.min(100, (view.progress.processed / Math.max(1, view.progress.total)) * 100)}%` }}
                        />
                      </div>
                      {(() => {
                        const p = view.progress;
                        let rem = p.estimatedRemaining;
                        if ((rem === undefined || rem === null || rem <= 0) && p.processed > 0 && p.total > p.processed && p.elapsed > 0) {
                          const avg = Math.max(p.elapsed / p.processed, 0.8);
                          rem = (p.total - p.processed) * avg;
                        }
                        if (rem !== undefined && rem !== null && rem > 0) {
                          return (
                            <p className="font-mono text-[10px] text-mist/50 text-right">
                              Est. remaining: {formatTime(rem)}
                            </p>
                          );
                        }
                        if (p.processed === 0) {
                          return (
                            <p className="font-mono text-[10px] text-mist/40 text-right">
                              Est. remaining: calculating…
                            </p>
                          );
                        }
                        return null;
                      })()}
                    </div>
                  )}
                  {scanTaskId && (
                    <button
                      onClick={handleStopScan}
                      disabled={stoppingScan || cancelRequested}
                      className="mt-4 font-mono text-xs text-signal-avoid border border-signal-avoid/40 rounded-lg px-3 py-2 hover:bg-signal-avoid/10 transition disabled:opacity-50 w-full"
                    >
                      {cancelRequested ? "Stopping — finishing up..." : "⏹ Stop Scan"}
                    </button>
                  )}
                  <div className="mt-6">
                    <Pipeline running={true} />
                  </div>
                </div>
              )}

              {view.mode === "error" && (
                <div className="rounded-xl border border-signal-sell/40 bg-signal-sell/5 p-6">
                  <p className="font-mono text-xs text-signal-sell/70 uppercase tracking-widest mb-1">
                    Error
                  </p>
                  <p className="text-sm text-signal-sell break-words">{view.message}</p>
                  {view.message.includes("timeout") || view.message.includes("reach") && (
                    <p className="text-xs text-mist/60 mt-2">
                      ⏳ Free‑tier services may take up to 60 seconds to wake up on the first request.
                      Wait a moment and try again.
                    </p>
                  )}
                  <div className="flex gap-4 mt-4">
                    <button
                      onClick={handleRetry}
                      disabled={isRetrying}
                      className="font-mono text-xs text-paper bg-signal-prepare/20 border border-signal-prepare/40 rounded-lg px-4 py-1.5 hover:bg-signal-prepare/30 transition disabled:opacity-50"
                    >
                      {isRetrying ? "⏳ Restarting..." : (scanTaskId ? "Resume Scan" : "Try again")}
                    </button>
                    <button
                      onClick={() => setShowSettings(true)}
                      className="font-mono text-xs text-mist hover:text-paper underline"
                    >
                      Check backend settings
                    </button>
                  </div>
                </div>
              )}

              {view.mode === "stock" && (
                <>
                  <div className="mb-3 flex items-center gap-3">
                    <button type="button" onClick={() => setView({ mode: "idle" })} className="px-3 py-1.5 rounded-lg bg-slate-800 text-slate-200 text-sm border border-slate-600 hover:bg-slate-700">← Back</button>
                    <span className="text-slate-400 text-sm">Analysis</span>
                  </div>
                  <HorizonStrip data={(view as any).data} />
                  <DecisionCard
                    data={view.data}
                    onBack={() => setView({ mode: "idle" })}
                    onSearchRelated={handleSearch}
                    onAddToWatchlist={handleAddToWatchlist}
                  />
                </>
              )}

              {view.mode === "scan" && (
                <>
                  <MultiHorizonScanLists data={(view as any).data} />
                  <ScanPanel
                    result={view.data}
                    onSelect={handleSearch}
                    onBack={() => setView({ mode: "idle" })}
                    onAddToWatchlist={handleAddToWatchlist}
                    onAddManyToWatchlist={handleAddManyToWatchlist}
                    onSendTopPicks={handleSendTopPicks}
                    onSendAllActionable={handleSendAllActionable}
                  />
                </>
              )}
            </section>
          </>
        )}
      </main>

      
      {/* Mobile bottom navigation */}
      <nav className="terminal-bottom-nav md:hidden" aria-label="Primary">
        {navItems.filter((i) => i.id !== "settings").slice(0, 5).map((item) => (
          <button
            key={item.id}
            type="button"
            className={`bottom-nav-item ${tab === item.id ? "active" : ""}`}
            onClick={() => setTab(item.id)}
          >
            <span>{item.short}</span>
          </button>
        ))}
        <button
          type="button"
          className={`bottom-nav-item ${tab === "settings" ? "active" : ""}`}
          onClick={() => setTab("settings")}
        >
          <span>More</span>
        </button>
      </nav>

<footer className="max-w-6xl mx-auto px-4 sm:px-6 py-6 border-t border-slate/40 mt-12">
        <p className="text-[11px] text-mist/40 font-mono">
          For informational use only -- not investment advice. Always verify before trading.
        </p>
      </footer>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`font-mono text-xs uppercase tracking-widest px-3 py-2 rounded-lg transition ${
        active ? "text-paper bg-slate/60" : "text-mist hover:text-paper"
      }`}
    >
      {children}
    </button>
  );
}

function BackendStatusDot({
  status,
  onClick,
}: {
  status: "checking" | "up" | "down";
  onClick: () => void;
}) {
  const color =
    status === "up" ? "bg-signal-buy" : status === "down" ? "bg-signal-sell" : "bg-signal-hold animate-pulse";
  const label = status === "up" ? "Backend connected" : status === "down" ? "Backend unreachable" : "Checking...";
  return (
    <button
      onClick={onClick}
      title={label}
      className="hidden sm:flex items-center gap-1.5 font-mono text-[10px] text-mist/60 hover:text-mist transition px-1"
    >
      <span className={`w-1.5 h-1.5 rounded-full ${color}`} />
      {label}
    </button>
  );
}



function MarketClock() {
  const [now, setNow] = useState(() => new Date());
  const [phase, setPhase] = useState<string | null>(null);
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);
  useEffect(() => {
    let c = false;
    (async () => {
      try {
        const { getApiUrl } = await import("./api");
        const base = getApiUrl();
        if (!base) return;
        const res = await fetch(`${base.replace(/\/$/, "")}/market/session`);
        if (!c && res.ok) {
          const j = await res.json();
          setPhase(j.phase || null);
        }
      } catch { /* ignore */ }
    })();
    return () => { c = true; };
  }, []);
  const ist = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
    weekday: "short",
  }).format(now);
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    weekday: "short",
  }).formatToParts(now);
  const map: Record<string, string> = {};
  for (const p of parts) if (p.type !== "literal") map[p.type] = p.value;
  const hm = parseInt((map.hour || "0") + (map.minute || "0"), 10);
  const day = map.weekday || "";
  const weekday = !["Sat", "Sun"].includes(day);
  const open = phase ? phase === "open" : weekday && hm >= 915 && hm <= 1530;
  const pre = phase ? phase === "preopen" : weekday && hm >= 830 && hm < 915;
  const post = phase ? phase === "post" : weekday && hm > 1530 && hm <= 1600;
  const holiday = phase === "holiday";
  const label = holiday ? "NSE HOLIDAY" : open ? "NSE OPEN" : pre ? "PRE-OPEN" : post ? "POST" : "NSE CLOSED";
  const color = holiday ? "text-signal-sell" : open ? "text-signal-buy" : pre || post ? "text-amber-300" : "text-mist/60";
  return (
    <div className="market-clock mono text-[11px] flex flex-wrap gap-3 mb-2">
      <span className={color}>{label}</span>
      <span className="text-mist/70">{ist} IST</span>
      <span className="text-mist/50">Session 09:15–15:30 · Full warm 08:30–16:00</span>
    </div>
  );
}

function SettingsPage({
  backendUp,
  onSaved,
  theme,
  setTheme,
  onOpenServices,
}: {
  backendUp: "checking" | "up" | "down";
  onSaved: () => void;
  theme: "dark" | "light";
  setTheme: (t: "dark" | "light") => void;
  onOpenServices: () => void;
}) {
  const [url, setUrl] = useState(getApiUrl());
  const [savedMsg, setSavedMsg] = useState<string | null>(null);

  function save() {
    setApiUrl(url);
    onSaved();
    setSavedMsg("API URL saved on this device.");
    setTimeout(() => setSavedMsg(null), 3000);
  }

  return (
    <div className="page-terminal settings-page space-y-4">
      <p className="dash-section-title">Settings</p>
      <p className="text-xs text-mist/60 mb-2">API connection · theme · services · deploy checklist</p>
      <section className="terminal-panel">
        <h3 className="mono text-xs text-mist uppercase tracking-widest mb-2">Backend connection</h3>
        <p className="text-mist/70 text-xs mb-3 max-w-lg">
          API Gateway public URL. Required when frontend and backend are deployed separately.
          Status:{" "}
          <strong className={backendUp === "up" ? "text-signal-buy" : backendUp === "down" ? "text-signal-sell" : "text-mist"}>
            {backendUp}
          </strong>
        </p>
        <div className="flex flex-col sm:flex-row gap-2">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && save()}
            placeholder="https://your-api-gateway.onrender.com"
            className="flex-1 bg-ink/60 border border-slate rounded-lg px-3 py-2 font-mono text-xs text-paper placeholder:text-mist/30 outline-none focus:border-cyan-500/50"
            spellCheck={false}
            autoComplete="off"
          />
          <button type="button" onClick={save} className="btn-terminal">
            Save URL
          </button>
        </div>
        {savedMsg && <p className="mono text-xs text-signal-buy mt-2 mb-0">{savedMsg}</p>}
      </section>

      <section className="terminal-panel">
        <h3 className="mono text-xs text-mist uppercase tracking-widest mb-2">Appearance</h3>
        <button type="button" className="btn-terminal" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
          Switch to {theme === "dark" ? "light" : "dark"} mode
        </button>
      </section>

      <section className="terminal-panel">
        <h3 className="mono text-xs text-mist uppercase tracking-widest mb-2">Services</h3>
        <p className="text-mist/70 text-xs mb-3">Wake or inspect microservice health.</p>
        <button type="button" className="btn-terminal" onClick={onOpenServices}>
          Open Service Manager
        </button>
      </section>

      <section className="terminal-panel">
        <h3 className="mono text-xs text-mist uppercase tracking-widest mb-2">Deploy checklist</h3>
        <ul className="text-xs text-mist/80 space-y-1.5 mono pl-4 list-disc">
          <li>Set VITE_API_URL at frontend build time to your API Gateway</li>
          <li>Set DATABASE_URL on decision-prediction-service (Neon/Supabase Postgres)</li>
          <li>Set UPSTASH_REDIS_REST_URL + TOKEN on all services</li>
          <li>Replace placeholder *.onrender.com URLs with your real service URLs</li>
          <li>Optional: CALLMEBOT_USER / CALLMEBOT_USERS for voice alerts</li>
        </ul>
      </section>
    </div>
  );
}

function SettingsBanner({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [url, setUrl] = useState(getApiUrl());

  function save() {
    setApiUrl(url);
    onSaved();
    onClose();
  }

  return (
    <div className="border-b border-slate/60 bg-graphite">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-5">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="flex-1 min-w-[260px]">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
              Backend connection
            </h3>
            <p className="text-mist/70 text-xs mb-3 max-w-md">
              This is the URL of your deployed API Gateway service. If the app shows "Failed to
              fetch", it usually means this wasn't set when the frontend was built -- set it here
              once and it's remembered on this device.
            </p>
            <div className="flex gap-2">
              <input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && save()}
                placeholder="https://api-gateway-puwd.onrender.com"
                className="flex-1 bg-ink/60 border border-slate rounded-lg px-3 py-2 font-mono text-xs text-paper placeholder:text-mist/30 outline-none focus:border-signal-prepare/60 transition"
                spellCheck={false}
                autoComplete="off"
              />
              <button
                onClick={save}
                className="border border-slate rounded-lg px-4 py-2 font-mono text-xs text-mist hover:text-paper hover:border-signal-prepare/60 transition"
              >
                Save
              </button>
            </div>
          </div>
          <button
            onClick={onClose}
            className="font-mono text-xs text-mist hover:text-paper underline shrink-0"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}