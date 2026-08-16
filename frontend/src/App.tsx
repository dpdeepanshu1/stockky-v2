// frontend/src/App.tsx

import { useState, useEffect, useRef } from "react";
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
import Trades from "./components/Trades";
// ── NEW: import Market Sentiment Header ──
import MarketSentimentHeader from "./components/MarketSentimentHeader";

type ViewState =
  | { mode: "idle" }
  | { mode: "loading"; label: string; progress?: { processed: number; total: number; elapsed: number; estimatedRemaining?: number } }
  | { mode: "stock"; data: Decision }
  | { mode: "scan"; data: ScanResult }
  | { mode: "error"; message: string };

type Tab = "dashboard" | "notifications" | "training" | "trades";

export default function App() {
  const [systemReady, setSystemReady] = useState(false);
  const [tab, setTab] = useState<Tab>("dashboard");
  const [query, setQuery] = useState("");
  const [view, setView] = useState<ViewState>({ mode: "idle" });
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [showWatchlist, setShowWatchlist] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showServiceManager, setShowServiceManager] = useState(false);
  const [backendUp, setBackendUp] = useState<"checking" | "up" | "down">("checking");
  const [isWaking, setIsWaking] = useState(false);

  // Scan polling state
  const [scanTaskId, setScanTaskId] = useState<string | null>(null);
  const [pollInterval, setPollInterval] = useState<number | null>(null);

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
      const { task_id } = await api.scanStart();
      setScanTaskId(task_id);
      sessionStorage.setItem("stockky_scan_task_id", task_id);
      const interval = window.setInterval(() => pollScanStatus(task_id), 1000);
      setPollInterval(interval);
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

  return (
    <div className="min-h-screen bg-ink text-paper relative">
      {statusMessage && (
        <div className="fixed top-20 right-4 z-50 bg-graphite border border-signal-buy/40 rounded-xl px-5 py-3 shadow-2xl animate-fadeIn flex items-center gap-2">
          <p className="font-mono text-sm text-paper">{statusMessage}</p>
        </div>
      )}

      <header className="sticky top-0 z-40 border-b border-slate/60 backdrop-blur-sm bg-ink/90">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-4 flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-4">
            <div>
              <span className="font-display text-xl tracking-tight">Stockky</span>
              <span className="font-mono text-[10px] text-mist tracking-widest uppercase ml-3 hidden sm:inline">
                NSE - India
              </span>
            </div>
            <nav className="flex items-center gap-1 ml-2">
              <TabButton active={tab === "dashboard"} onClick={() => setTab("dashboard")}>
                Dashboard
              </TabButton>
              <TabButton active={tab === "notifications"} onClick={() => setTab("notifications")}>
                Notifications
              </TabButton>
              <TabButton active={tab === "training"} onClick={() => setTab("training")}>
                Training
              </TabButton>
              <TabButton active={tab === "trades"} onClick={() => setTab("trades")}>
                Trades
              </TabButton>
            </nav>
          </div>
          <div className="flex items-center gap-2">
            <BackendStatusDot status={backendUp} onClick={() => setShowSettings(true)} />
            {tab === "dashboard" && (
              <button
                onClick={() => setShowWatchlist(!showWatchlist)}
                className="flex items-center gap-2 text-xs font-mono text-mist hover:text-paper border border-slate rounded-lg px-3 py-2 hover:border-mist/60 transition"
              >
                <span className="w-1.5 h-1.5 rounded-full bg-signal-prepare inline-block" />
                Watchlist ({watchlist.length})
              </button>
            )}
            <button
              onClick={() => setShowServiceManager(!showServiceManager)}
              className="text-xs font-mono text-mist hover:text-paper border border-slate rounded-lg px-3 py-2 hover:border-mist/60 transition"
              title="Service Manager"
            >
              ⚙️ Services
            </button>
            <button
              onClick={() => setShowSettings(!showSettings)}
              className="text-xs font-mono text-mist hover:text-paper border border-slate rounded-lg px-3 py-2 hover:border-mist/60 transition"
              title="Backend settings"
            >
              Settings
            </button>
          </div>
        </div>
      </header>

      {showSettings && (
        <SettingsBanner onClose={() => setShowSettings(false)} onSaved={checkBackend} />
      )}

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

      <main className="max-w-6xl mx-auto px-4 sm:px-6 py-8 sm:py-10">
        {tab === "notifications" ? (
          <NotificationsPanel />
        ) : tab === "training" ? (
          <Training />
        ) : tab === "trades" ? (
          <Trades />
        ) : (
          <>
            {/* Dashboard content */}
            <section className="mb-8 sm:mb-10">
              <h1 className="font-display text-3xl sm:text-4xl md:text-[46px] leading-tight max-w-xl mb-2">
                Know your next move. <span className="italic text-mist">In one call.</span>
              </h1>
              <p className="text-mist text-sm max-w-lg mb-8">
                Technical, fundamental, news and AI signals -- combined into a single decision.
              </p>

              <div className="flex flex-col sm:flex-row gap-3">
                <div className="flex-1 flex items-center gap-2 border border-slate rounded-xl px-4 py-3.5 bg-graphite focus-within:border-signal-prepare/60 transition">
                  <span className="font-mono text-mist text-xs select-none">NSE:</span>
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value.toUpperCase())}
                    onKeyDown={(e) => e.key === "Enter" && handleSearch(query)}
                    placeholder="TCS, INFY, RELIANCE..."
                    className="bg-transparent outline-none flex-1 font-mono text-sm placeholder:text-mist/30 min-w-0"
                    autoComplete="off"
                    spellCheck={false}
                  />
                  {query && (
                    <button
                      onClick={() => handleSearch(query)}
                      className="text-[10px] font-mono uppercase tracking-widest bg-signal-prepare/10 text-signal-prepare border border-signal-prepare/30 rounded px-3 py-1.5 hover:bg-signal-prepare/20 transition shrink-0"
                    >
                      Analyse
                    </button>
                  )}
                </div>
                <button
                  onClick={handleScan}
                  className="border border-slate rounded-xl px-6 py-3.5 font-mono text-xs uppercase tracking-widest text-mist hover:text-paper hover:border-mist transition whitespace-nowrap bg-graphite"
                >
                  Run market scan
                </button>
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
                          style={{ width: `${(view.progress.processed / view.progress.total) * 100}%` }}
                        />
                      </div>
                      {view.progress.estimatedRemaining !== undefined && view.progress.estimatedRemaining > 0 && (
                        <p className="font-mono text-[10px] text-mist/40 text-right">
                          Est. remaining: {formatTime(view.progress.estimatedRemaining)}
                        </p>
                      )}
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
                placeholder="https://STOCKKY-API-GATEWAY.onrender.com"
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