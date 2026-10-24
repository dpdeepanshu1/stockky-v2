import { lazy } from "react";
import WatchlistManager from "../WatchlistManager";
import { AdminGate } from "../AdminGate"; // group 460: one login for the three admin pages
import ErrorBoundary from "../ui/ErrorBoundary";
import { PageHeader } from "../ui/PageHeader";
import { SettingsPage } from "./SettingsPage";
import { DashboardView, type DashboardViewProps } from "./DashboardView";
import type { Tab } from "./appTypes";

// E70 - E31 step 14: the tab switch of App.tsx (which page the main area shows), moved here with the lazy page imports (JSX unchanged).
// The per-tab ErrorBoundary and the Suspense stay in App, around this component, so a lazy chunk that is still loading suspends up to them.
// E31: the heavy pages are separate chunks, fetched when their tab is first opened (the dashboard no longer carries the trading, training and
// calibration pages). They all sit inside the per-tab ErrorBoundary + Suspense below; a failed chunk download shows the boundary's retry, not a blank page.
const Training = lazy(() => import("../Training"));
const HotStocks = lazy(() => import("../HotStocks"));
const SurpriseStocks = lazy(() => import("../SurpriseStocks"));
const IpoTracker = lazy(() => import("../IpoTracker"));
const RealAutoTrade = lazy(() => import("../RealAutoTrade"));
const PositionStocksTab = lazy(() => import("../PositionStocksTab"));
const CalibrationControlTab = lazy(() => import("../CalibrationControlTab"));
const DataFeed = lazy(() => import("../DataFeed"));
const DataHealthAudit = lazy(() => import("../DataHealthAudit"));
export const loadTrades = () => import("../Trades");          // also called before the open-deposit event, so the page exists to hear it
const Trades = lazy(loadTrades);
const NotificationsPanel = lazy(() => import("../NotificationsPanel"));

type Theme = "dark" | "light";

export interface TabContentProps {
  tab: Tab; setTab: (t: Tab) => void;
  watchlist: string[]; handleWatchlistUpdate: (symbols: string[]) => Promise<void>;
  handleSearch: (symbol: string) => unknown; handleScanWatchlist: () => unknown;
  backendUp: "checking" | "up" | "down"; checkBackend: () => void;
  theme: Theme; setTheme: (t: Theme) => void;
  setShowServiceManager: (v: boolean) => void;
  dashboard: DashboardViewProps;
}

export function TabContent({
  tab, setTab, watchlist, handleWatchlistUpdate, handleSearch, handleScanWatchlist, backendUp, checkBackend, theme, setTheme, setShowServiceManager, dashboard,
}: TabContentProps) {
  return (
    <>
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
      <DashboardView {...dashboard} />
    )}
    </>
  );
}
