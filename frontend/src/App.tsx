// frontend/src/App.tsx

import { Suspense, useState, useEffect, useCallback } from "react";
import { flushSync } from "react-dom";
import { api, getApiUrl, startSessionKeepAlive, stopSessionKeepAlive } from "./api";
import WatchlistManager from "./components/WatchlistManager";
import SystemCheck from "./components/SystemCheck";
import ServiceManager from "./components/ServiceManager";
import ErrorBoundary from "./components/ui/ErrorBoundary";
import { NavIcon } from "./components/ui/NavIcon";
import CommandPalette, { CommandAction } from "./components/CommandPalette";
import StockPopup from "./components/board/StockPopup";
import type { BoardRowLite } from "./components/board/boardTypes";
import { DesktopSidebar, MobileTopBar, MobileDrawer, DesktopTopBar, BackendDownBanner } from "./components/app/AppNav";
import { TabContent, loadTrades } from "./components/app/TabContent";
import type { DashboardViewProps } from "./components/app/DashboardView";
import type { NavItem, Tab } from "./components/app/appTypes";
import { useWatchlist } from "./hooks/useWatchlist";
import { useBackendStatus } from "./hooks/useBackendStatus";
import { useScanRunner } from "./hooks/useScanRunner";

// E70 - E31 step 14: App is now the shell. The page switch is components/app/TabContent.tsx (with the lazy page chunks); the watchlist, the backend
// check and the whole scan / analysis runner are hooks in src/hooks/. What stays here: theme, keep-alive, DB wake, idle tick, shortcuts, navigation.

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
  const [tab, setTab] = useState<Tab>("dashboard");
  const [query, setQuery] = useState("");
  const [signalFilter, setSignalFilter] = useState<"ALL" | "BUY" | "PREPARE" | "HOLD">("ALL");
  const [showWatchlist, setShowWatchlist] = useState(false);
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



  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const { watchlist, setWatchlist, handleWatchlistUpdate, handleAddToWatchlist, handleAddManyToWatchlist } = useWatchlist(setStatusMessage);
  const { backendUp, isWaking, checkBackend, handleWakeBackend } = useBackendStatus(setWatchlist);
  const {
    view, setView, liveScanRows, scanTransport, scanTaskId, isRetrying, cancelRequested, stoppingScan, wsLive,
    handleSearch, handleScan, handleScanWatchlist, handleStopScan, handleRetry, handleSendTopPicks, handleSendAllActionable,
  } = useScanRunner({ setStatusMessage, setTab, setQuery, backendUp, handleWakeBackend });

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
  // "Fix in Settings" / "Check backend settings": open the Settings page, where the API URL is edited (they used to set a flag nothing rendered).
  const openSettings = () => goTo("settings");
  const themeLabel = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
  // Everything the dashboard page needs from the scan runner and the watchlist, as one object for the TabContent "dashboard" prop.
  const dashboard: DashboardViewProps = {
    view, setView, query, setQuery, watchlist,
    handleSearch, handleScan, handleStopScan, handleRetry, handleAddToWatchlist, handleAddManyToWatchlist, handleSendTopPicks, handleSendAllActionable,
    liveScanRows, scanTransport, scanTaskId, isRetrying, cancelRequested, stoppingScan, wsLive, signalFilter, setSignalFilter, openSettings,
  };


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

      {backendUp === "down" && tab !== "settings" && (
        <BackendDownBanner isWaking={isWaking} handleWakeBackend={handleWakeBackend} checkBackend={checkBackend} openSettings={openSettings} />
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
        <TabContent tab={tab} setTab={setTab} watchlist={watchlist} handleWatchlistUpdate={handleWatchlistUpdate}
          handleSearch={handleSearch} handleScanWatchlist={handleScanWatchlist} backendUp={backendUp} checkBackend={checkBackend}
          theme={theme} setTheme={setTheme} setShowServiceManager={setShowServiceManager} dashboard={dashboard} />
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