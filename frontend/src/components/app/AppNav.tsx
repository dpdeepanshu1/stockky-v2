import { Fragment, useEffect, useState, useSyncExternalStore } from "react";
import { getApiUrl } from "../../api";
import { SessionPill, useAdminSession } from "../AdminGate";
import { NavIcon } from "../ui/NavIcon";
import { Sheet } from "../kit/Sheet";
import { Tabs } from "../kit/Tabs";
import { Button } from "../kit/Button";
import { StatusClock } from "./clocks";
import type { Tab } from "./appTypes";
import {
  BOTTOM_SLOTS, DRAWER_SECTIONS, MARKETS_TABS, SECTIONS, TRADING_TABS, activeSlot, isTradingTab, itemOf, itemsOf, sectionOf, sectionTarget,
  type NavSection, type SlotId,
} from "./navModel";
import { modeStripVersion, readModeStrip, stripView, subscribeModeStrip } from "./modeStrip";

// E68 / E535: the app's navigation and top bars. E535 (redesign round 3) regrouped the 14 tabs into five sections (navModel.ts), made the phone
// bottom bar Home / Markets / Trading / Alerts / More, and added the mode strip. State stays in App and arrives as props; no tab id or route changed.
type Setter<T> = (v: T) => void;
export type BackendUp = "checking" | "up" | "down";
export type DbWakeState = "idle" | "waking" | "ok" | "error";
type Theme = "dark" | "light";

const SYSTEM_FOLD_KEY = "stockky:nav_system_open";

/* ---------- desktop sidebar: five sections, System folded ---------- */
export function DesktopSidebar({ tab, goTo, showServiceManager, setShowServiceManager, setCmdOpen }: {
  tab: Tab; goTo: (id: Tab) => void;
  showServiceManager: boolean; setShowServiceManager: Setter<boolean>; setCmdOpen: Setter<boolean>;
}) {
  const [systemOpen, setSystemOpen] = useState(() => { try { return localStorage.getItem(SYSTEM_FOLD_KEY) === "1"; } catch { return false; } });
  const here = sectionOf(tab).id;
  const toggleSystem = () => { const v = !systemOpen; setSystemOpen(v); try { localStorage.setItem(SYSTEM_FOLD_KEY, v ? "1" : "0"); } catch { /* storage blocked */ } };
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
          {SECTIONS.map((sec: NavSection) => {
            const items = itemsOf(sec.id);
            const folded = sec.foldedByDefault && !systemOpen && here !== sec.id;
            return (
              <Fragment key={sec.id}>
                {sec.id !== "home" && (
                  sec.foldedByDefault ? (
                    <button type="button" className="sidebar-group-label sh-sec-toggle" aria-expanded={!folded} onClick={toggleSystem} title={`${sec.label} (${folded ? "show" : "hide"})`}>
                      <span>{sec.label}</span><span className="sh-chev" aria-hidden="true">{folded ? "▸" : "▾"}</span>
                    </button>
                  ) : (
                    <div className="sidebar-group-label sh-sec-label">
                      <span>{sec.label}</span>
                      {sec.locked && <span className="sh-lock" title="Needs the admin login"><NavIcon name="lock" /><span className="sr-only">Needs the admin login</span></span>}
                    </div>
                  )
                )}
                {items.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`sidebar-link ${tab === item.id ? "active" : ""} ${folded ? "sh-folded" : ""}`}
                    title={item.label}
                    aria-current={tab === item.id ? "page" : undefined}
                    onClick={() => goTo(item.id)}
                  >
                    <NavIcon name={item.icon} />
                    {item.label}
                  </button>
                ))}
              </Fragment>
            );
          })}
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

/* ---------- phone top bar: page name, status dot, search ---------- */
export function MobileTopBar({ tab, backendUp, wsLive, handleWakeBackend, setTab, setCmdOpen }: {
  tab: Tab; backendUp: BackendUp; wsLive: boolean; handleWakeBackend: () => unknown; setTab: Setter<Tab>; setCmdOpen: Setter<boolean>;
}) {
  return (
      <header className="terminal-topbar md:hidden">
        <div className="flex items-center gap-2 min-w-0">
          <span className="brand-mark brand-mark-sm" aria-hidden="true">S</span>
          <div className="mtb-titles">
            <span className="mtb-app">{itemOf(tab).label}</span>
            <span className="mtb-page">{sectionOf(tab).id === "home" ? "Stockky" : sectionOf(tab).label}</span>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <SessionPill compact />
          <button
            type="button"
            className={`sh-dot-btn ${backendUp === "up" ? "ok" : backendUp === "down" ? "bad" : "warn"}`}
            onClick={() => (backendUp === "down" ? handleWakeBackend() : setTab("settings"))}
            aria-label={backendUp === "up" ? (wsLive ? "Backend connected, live prices" : "Backend connected, polling prices") : backendUp === "down" ? "Backend unreachable, tap to wake" : "Connecting to the backend"}
            title={backendUp === "up" ? (wsLive ? "Connected · live prices" : "Connected · polling prices") : backendUp === "down" ? "Backend unreachable — tap to wake" : "Connecting…"}
          >
            <span className="dot" />
            <span className="sh-dot-word">{backendUp === "up" ? (wsLive ? "Live" : "Polling") : backendUp === "down" ? "Offline" : "…"}</span>
          </button>
          <button type="button" className="icon-btn" aria-label="Search and commands" onClick={() => setCmdOpen(true)}>
            <NavIcon name="search" />
          </button>
        </div>
      </header>
  );
}

/* ---------- phone bottom bar: Home, Markets, Trading, Alerts, More ---------- */
export function MobileBottomNav({ tab, goTo, tradingOpen, setTradingOpen, mobileMoreOpen, setMobileMoreOpen }: {
  tab: Tab; goTo: (id: Tab) => void; tradingOpen: boolean; setTradingOpen: Setter<boolean>; mobileMoreOpen: boolean; setMobileMoreOpen: Setter<boolean>;
}) {
  const lit: SlotId = activeSlot(tab, tradingOpen ? "trading" : mobileMoreOpen ? "more" : null);
  const press = (id: SlotId) => {
    if (id === "home") goTo("dashboard");
    else if (id === "alerts") goTo("notifications");
    else if (id === "markets") goTo(sectionTarget(tab, "markets"));
    else if (id === "trading") { setMobileMoreOpen(false); setTradingOpen(true); }
    else { setTradingOpen(false); setMobileMoreOpen(true); }
  };
  return (
    <nav className="terminal-bottom-nav md:hidden" aria-label="Primary">
      {BOTTOM_SLOTS.map((slot) => (
        <button key={slot.id} type="button" className={lit === slot.id ? "active" : ""} aria-current={lit === slot.id && !tradingOpen && !mobileMoreOpen ? "page" : undefined}
          aria-haspopup={slot.id === "trading" || slot.id === "more" ? "dialog" : undefined} onClick={() => press(slot.id)}>
          <NavIcon name={slot.icon} />
          <span>{slot.label}</span>
        </button>
      ))}
    </nav>
  );
}

/* ---------- phone "Trading" sheet: the four pages that need the admin login ---------- */
export function TradingSheet({ open, onClose, tab, goTo }: { open: boolean; onClose: () => void; tab: Tab; goTo: (id: Tab) => void }) {
  return (
    <Sheet open={open} onClose={onClose} title="Trading">
      <p className="sh-sheet-note"><NavIcon name="lock" /> These four pages use one admin login.</p>
      <div className="sh-sheet-list">
        {TRADING_TABS.map((id) => {
          const item = itemOf(id);
          return (
            <button key={id} type="button" className={`sh-sheet-link ${tab === id ? "active" : ""}`} aria-current={tab === id ? "page" : undefined} onClick={() => goTo(id)}>
              <NavIcon name={item.icon} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </div>
    </Sheet>
  );
}

/* ---------- phone More drawer: Paper and System only (the bar covers the rest) ---------- */
export function MobileDrawer({ tab, goTo, setMobileMoreOpen, setShowServiceManager, wakeAllDbs, dbWakeState, theme, setTheme }: {
  tab: Tab; goTo: (id: Tab) => void; setMobileMoreOpen: Setter<boolean>; setShowServiceManager: Setter<boolean>;
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
              {DRAWER_SECTIONS.map((sid) => (
                <Fragment key={sid}>
                  <div className="mobile-drawer-group">{SECTIONS.find((s) => s.id === sid)!.label}</div>
                  {itemsOf(sid).map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      className={`mobile-drawer-link ${tab === item.id ? "active" : ""}`}
                      aria-current={tab === item.id ? "page" : undefined}
                      onClick={() => goTo(item.id)}
                    >
                      <NavIcon name={item.icon} />
                      {item.label}
                    </button>
                  ))}
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

/* ---------- Markets: one section, four sliding tabs ---------- */
export function MarketsTabs({ tab, goTo }: { tab: Tab; goTo: (id: Tab) => void }) {
  return (
    <div className="sh-markets-tabs">
      <Tabs<Tab> label="Markets" value={tab} onChange={goTo} items={MARKETS_TABS.map((id) => ({ id: id as Tab, label: itemOf(id).label }))} />
    </div>
  );
}

/* ---------- mode strip: DEMO or REAL, armed or not, today's P&L (read-only) ---------- */
export function ModeStrip({ tab }: { tab: Tab }) {
  useSyncExternalStore(subscribeModeStrip, modeStripVersion, modeStripVersion);
  const session = useAdminSession();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const id = window.setInterval(() => setNow(Date.now()), 15000); return () => window.clearInterval(id); }, []);
  if (!isTradingTab(tab)) return null;
  const v = stripView(tab, readModeStrip(tab), now, session.has);
  return (
    <div className={`sh-modestrip ${v.tone}`} role="status" aria-label="Trading mode">
      <span className="sh-ms-mode">
        {v.tone === "real" && <NavIcon name="lock" />}
        <b>{v.modeText}</b>
      </span>
      {v.armedText && <span className={`sh-ms-armed ${v.armedOn ? "on" : ""}`}><i aria-hidden="true" />{v.armedText}</span>}
      {v.pnlText && <span className={`sh-ms-pnl ${v.pnlTone}`}>Today {v.pnlText}{v.stale ? <em> · may be out of date</em> : null}</span>}
      <span className="sh-ms-hint">{v.hint}</span>
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
        <SessionPill />
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

export function BackendDownBanner({ isWaking, handleWakeBackend, checkBackend, openSettings }: {
  isWaking: boolean; handleWakeBackend: () => void; checkBackend: () => void; openSettings: () => void;
}) {
  return (
    <div className="sh-banner" role="alert">
      <p className="sh-banner-text">Can't reach the backend at <span className="sh-banner-url">{getApiUrl() || "(not set)"}</span>.</p>
      <div className="sh-banner-actions">
        <Button size="sm" onClick={handleWakeBackend} loading={isWaking}>{isWaking ? "Waking..." : "Wake Backend"}</Button>
        <Button size="sm" variant="ghost" onClick={checkBackend}>Retry</Button>
        <Button size="sm" variant="ghost" onClick={openSettings}>Fix in Settings</Button>
      </div>
    </div>
  );
}
