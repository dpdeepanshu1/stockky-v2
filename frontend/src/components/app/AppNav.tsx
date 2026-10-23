import { Fragment } from "react";
import { getApiUrl } from "../../api";
import { SessionChip } from "../AdminGate";
import { NavIcon } from "../ui/NavIcon";
import { StatusClock } from "./clocks";
import type { NavItem, Tab } from "./appTypes";

// E68 - E31 step 12: the app's navigation and top bars, moved out of App.tsx (JSX unchanged). State stays in App and arrives as props.
type Setter<T> = (v: T) => void;
export type BackendUp = "checking" | "up" | "down";
export type DbWakeState = "idle" | "waking" | "ok" | "error";
type Theme = "dark" | "light";

export function DesktopSidebar({ navItems, tab, goTo, showServiceManager, setShowServiceManager, setCmdOpen }: {
  navItems: NavItem[]; tab: Tab; goTo: (id: Tab) => void;
  showServiceManager: boolean; setShowServiceManager: Setter<boolean>; setCmdOpen: Setter<boolean>;
}) {
  return (
      <aside className="terminal-sidebar hidden md:flex" aria-label="Main navigation">
        <div className="sidebar-brand">
          <span className="brand-mark" aria-hidden="true">S</span>
          <div>
            <div className="brand-name">Stockky</div>
            <div className="brand-sub">NSE research &amp; trading</div>
          </div>
        </div>
        <nav className="sidebar-nav">
          {navItems.map((item, i) => (
            <Fragment key={item.id}>
              {item.group && item.group !== navItems[i - 1]?.group && (
                <div className="sidebar-group-label">{item.group}</div>
              )}
              <button
                type="button"
                className={`sidebar-link ${tab === item.id ? "active" : ""}`}
                title={item.label}
                aria-current={tab === item.id ? "page" : undefined}
                onClick={() => goTo(item.id)}
              >
                <NavIcon name={item.icon} />
                {item.label}
              </button>
            </Fragment>
          ))}
        </nav>
        <div className="sidebar-footer">
          <button type="button" className="sidebar-link" onClick={() => setShowServiceManager(!showServiceManager)}>
            <NavIcon name="layers" />
            Services
          </button>
          <button type="button" className="sidebar-link" onClick={() => setCmdOpen(true)}>
            <NavIcon name="search" />
            Search &amp; commands
            <span className="kbd" style={{ marginLeft: "auto" }}>Ctrl K</span>
          </button>
        </div>
      </aside>
  );
}

export function MobileTopBar({ navItems, tab, mobileMoreOpen, setMobileMoreOpen, backendUp, wsLive, handleWakeBackend, setTab, setCmdOpen }: {
  navItems: NavItem[]; tab: Tab; mobileMoreOpen: boolean; setMobileMoreOpen: Setter<boolean>;
  backendUp: BackendUp; wsLive: boolean; handleWakeBackend: () => unknown; setTab: Setter<Tab>; setCmdOpen: Setter<boolean>;
}) {
  return (
      <header className="terminal-topbar md:hidden">
        <div className="flex items-center gap-2 min-w-0">
          <button
            type="button"
            className="mobile-menu-btn"
            aria-label="Open menu"
            aria-expanded={mobileMoreOpen}
            onClick={() => setMobileMoreOpen(true)}
          >
            <NavIcon name="menu" />
          </button>
          <span className="brand-mark brand-mark-sm" aria-hidden="true">S</span>
          <div className="mtb-titles">
            <span className="mtb-app">Stockky</span>
            <span className="mtb-page">{navItems.find((i) => i.id === tab)?.label ?? ""}</span>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <SessionChip />
          <button
            type="button"
            className={`status-chip ${backendUp === "up" ? "ok" : backendUp === "down" ? "bad" : "warn"}`}
            onClick={() => (backendUp === "down" ? handleWakeBackend() : setTab("settings"))}
            title={backendUp === "up" ? "Backend connected" : backendUp === "down" ? "Backend unreachable — tap to wake" : "Connecting…"}
          >
            <span className="dot" />
            {backendUp === "up" ? (wsLive ? "Live" : "Polling") : backendUp === "down" ? "Offline" : "…"}
          </button>
          <button type="button" className="icon-btn" aria-label="Search and commands" onClick={() => setCmdOpen(true)}>
            <NavIcon name="search" />
          </button>
        </div>
      </header>
  );
}

export function MobileDrawer({ navItems, tab, goTo, setMobileMoreOpen, setShowServiceManager, wakeAllDbs, dbWakeState, theme, setTheme }: {
  navItems: NavItem[]; tab: Tab; goTo: (id: Tab) => void; setMobileMoreOpen: Setter<boolean>; setShowServiceManager: Setter<boolean>;
  wakeAllDbs: () => unknown; dbWakeState: DbWakeState; theme: Theme; setTheme: Setter<Theme>;
}) {
  return (
        <div className="mobile-drawer-root md:hidden" role="dialog" aria-modal="true" aria-label="Menu">
          <button
            type="button"
            className="mobile-drawer-backdrop"
            aria-label="Close menu"
            onClick={() => setMobileMoreOpen(false)}
          />
          <aside className="mobile-drawer-panel">
            <div className="mobile-drawer-head">
              <div className="drawer-brand">
                <span className="brand-mark" aria-hidden="true">S</span>
                <div>
                  <div className="brand-name">Stockky</div>
                  <div className="brand-sub">NSE research &amp; trading</div>
                </div>
              </div>
              <button type="button" className="icon-btn" aria-label="Close menu" onClick={() => setMobileMoreOpen(false)}>
                <NavIcon name="x" />
              </button>
            </div>
            <nav className="mobile-drawer-nav">
              {navItems.map((item, i) => (
                <Fragment key={item.id}>
                  {item.group && item.group !== navItems[i - 1]?.group && (
                    <div className="mobile-drawer-group">{item.group}</div>
                  )}
                  <button
                    type="button"
                    className={`mobile-drawer-link ${tab === item.id ? "active" : ""}`}
                    aria-current={tab === item.id ? "page" : undefined}
                    onClick={() => goTo(item.id)}
                  >
                    <NavIcon name={item.icon} />
                    {item.label}
                  </button>
                </Fragment>
              ))}
            </nav>
            <div className="mobile-drawer-foot">
              <button
                type="button"
                className="mobile-drawer-link"
                onClick={() => { setShowServiceManager(true); setMobileMoreOpen(false); }}
              >
                <NavIcon name="layers" />
                Services
              </button>
              <button
                type="button"
                className="mobile-drawer-link"
                onClick={() => void wakeAllDbs()}
                disabled={dbWakeState === "waking"}
              >
                <NavIcon name="database" />
                {dbWakeState === "waking" ? "Waking databases…" : dbWakeState === "ok" ? "Databases awake" : dbWakeState === "error" ? "Wake failed — try again" : "Wake databases"}
              </button>
              <button
                type="button"
                className="mobile-drawer-link"
                onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
              >
                <NavIcon name={theme === "dark" ? "sun" : "moon"} />
                {theme === "dark" ? "Light theme" : "Dark theme"}
              </button>
            </div>
          </aside>
        </div>
  );
}

export function DesktopTopBar({ setCmdOpen, wsLive, backendUp, handleWakeBackend, setTab, dbWakeState, wakeAllDbs, themeLabel, theme, setTheme }: {
  setCmdOpen: Setter<boolean>; wsLive: boolean; backendUp: BackendUp; handleWakeBackend: () => unknown; setTab: Setter<Tab>;
  dbWakeState: DbWakeState; wakeAllDbs: () => unknown; themeLabel: string; theme: Theme; setTheme: Setter<Theme>;
}) {
  return (
      <div className="app-topbar hidden md:flex">
        <button type="button" className="topbar-search" onClick={() => setCmdOpen(true)} title="Search stocks or jump to a page">
          <NavIcon name="search" />
          <span className="grow">Search stocks or jump to a page</span>
          <span className="kbd">Ctrl K</span>
        </button>
        <div className="topbar-spacer" />
        <SessionChip />
        <button
          type="button"
          className={`status-chip ${wsLive ? "ok" : ""}`}
          onClick={() => setCmdOpen(true)}
          title={wsLive ? "Prices are streaming live" : "Live stream is off — prices refresh by polling"}
        >
          <span className="dot" />
          {wsLive ? "Live prices" : "Polling prices"}
        </button>
        <button
          type="button"
          className={`status-chip ${backendUp === "up" ? "ok" : backendUp === "down" ? "bad" : "warn"}`}
          onClick={() => {
            if (backendUp === "down") handleWakeBackend();
            else setTab("settings");
          }}
          title={backendUp === "down" ? "Click to wake the backend" : "Open settings"}
        >
          <span className="dot" />
          {backendUp === "up" ? "Backend connected" : backendUp === "down" ? "Backend offline — wake" : "Connecting…"}
        </button>
        <button
          type="button"
          className={`status-chip chip-db ${dbWakeState === "ok" ? "ok" : dbWakeState === "error" ? "bad" : dbWakeState === "waking" ? "warn" : ""}`}
          onClick={() => void wakeAllDbs()}
          disabled={dbWakeState === "waking"}
          title="Wake the Neon, training and cache databases (free-tier databases sleep when idle)"
        >
          <span className="dot" />
          {dbWakeState === "waking" ? "Waking databases…" : dbWakeState === "ok" ? "Databases awake" : dbWakeState === "error" ? "Wake failed — retry" : "Wake databases"}
        </button>
        <StatusClock />
        <button type="button" className="icon-btn" aria-label={themeLabel} title={themeLabel} onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
          <NavIcon name={theme === "dark" ? "sun" : "moon"} />
        </button>
      </div>
  );
}

export function BackendDownBanner({ isWaking, handleWakeBackend, checkBackend, setShowSettings }: {
  isWaking: boolean; handleWakeBackend: () => void; checkBackend: () => void; setShowSettings: Setter<boolean>;
}) {
  return (
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
  );
}
