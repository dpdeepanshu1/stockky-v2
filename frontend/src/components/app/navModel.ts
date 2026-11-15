// E535 (redesign round 3): the ONE description of the app's navigation. The sidebar, the phone bottom bar, the More drawer, the Markets tab strip,
// the mode strip and the command palette all read it, so a tab added here can never silently displace a bottom-bar slot.
// Pure data and pure functions (no React, no import), so shell.test.mjs runs them. Tab ids and routes are unchanged: only labels and grouping moved.
export type NavTab = "dashboard" | "notifications" | "training" | "trades" | "hot" | "surprise" | "ipo" | "datafeed" | "settings" | "watchlist" | "realtrade" | "positionstocks" | "fno" | "calibration" | "selfimprove";
export type NavIconId = "home" | "bot" | "trend" | "flame" | "zap" | "tag" | "swap" | "star" | "bell" | "database" | "flask" | "sliders" | "menu" | "lock" | "layers";
export type SectionId = "home" | "markets" | "trading" | "paper" | "system";

export interface NavEntry { id: NavTab; label: string; short: string; icon: NavIconId; group: string }

// Order = sidebar order. `group` equals the section label, because the page kickers (PageHeader kicker="...") are checked against it.
export const NAV_ITEMS: NavEntry[] = [
  { id: "dashboard", label: "Dashboard", short: "Home", icon: "home", group: "" },
  { id: "hot", label: "Hot Picks", short: "Picks", icon: "flame", group: "Markets" },
  { id: "surprise", label: "Surprise Momentum", short: "Surprise", icon: "zap", group: "Markets" },
  { id: "ipo", label: "IPO Tracker", short: "IPO", icon: "tag", group: "Markets" },
  { id: "watchlist", label: "Watchlist", short: "Watchlist", icon: "star", group: "Markets" },
  { id: "realtrade", label: "Auto Trade", short: "Auto Trade", icon: "bot", group: "Trading" },
  { id: "positionstocks", label: "Position Stocks", short: "Scalp", icon: "trend", group: "Trading" },
  { id: "fno", label: "F&O Options", short: "F&O", icon: "trend", group: "Trading" },
  { id: "calibration", label: "Real Money Calibration", short: "Calibrate", icon: "sliders", group: "Trading" },
  { id: "selfimprove", label: "Self Improve System", short: "Improve", icon: "flask", group: "Trading" },   // E567 (S2)
  { id: "trades", label: "Paper Trades", short: "Paper", icon: "swap", group: "Paper" },
  { id: "notifications", label: "Alerts", short: "Alerts", icon: "bell", group: "System" },
  { id: "datafeed", label: "Data Feed", short: "Feed", icon: "database", group: "System" },
  { id: "training", label: "Training Lab", short: "Train", icon: "flask", group: "System" },
  { id: "settings", label: "Settings", short: "Settings", icon: "sliders", group: "System" },
];

export interface NavSection { id: SectionId; label: string; icon: NavIconId; entry: NavTab; tabs: NavTab[]; locked: boolean; foldedByDefault: boolean }

export const SECTIONS: NavSection[] = [
  { id: "home", label: "Home", icon: "home", entry: "dashboard", tabs: ["dashboard"], locked: false, foldedByDefault: false },
  { id: "markets", label: "Markets", icon: "flame", entry: "hot", tabs: ["hot", "surprise", "ipo", "watchlist"], locked: false, foldedByDefault: false },
  { id: "trading", label: "Trading", icon: "lock", entry: "realtrade", tabs: ["realtrade", "positionstocks", "fno", "calibration", "selfimprove"], locked: true, foldedByDefault: false },
  { id: "paper", label: "Paper", icon: "swap", entry: "trades", tabs: ["trades"], locked: false, foldedByDefault: false },
  { id: "system", label: "System", icon: "layers", entry: "notifications", tabs: ["notifications", "datafeed", "training", "settings"], locked: false, foldedByDefault: true },
];

export const MARKETS_TABS: NavTab[] = SECTIONS.find((s) => s.id === "markets")!.tabs;
export const TRADING_TABS: NavTab[] = SECTIONS.find((s) => s.id === "trading")!.tabs;

/** True for a string that is one of the app's tab ids (used by the "go to this tab" event, so a bad id can never blank the page). */
export const isNavTab = (id: unknown): id is NavTab => typeof id === "string" && NAV_ITEMS.some((i) => i.id === id);

export const sectionOf = (tab: NavTab): NavSection => SECTIONS.find((s) => s.tabs.includes(tab))!;
export const itemOf = (tab: NavTab): NavEntry => NAV_ITEMS.find((i) => i.id === tab)!;
export const isTradingTab = (tab: NavTab): boolean => TRADING_TABS.includes(tab);
export const isMarketsTab = (tab: NavTab): boolean => MARKETS_TABS.includes(tab);
export const itemsOf = (s: SectionId): NavEntry[] => NAV_ITEMS.filter((i) => sectionOf(i.id).id === s);

/** Where a click on a section header goes: a page of that section you are already on stays; otherwise the section's entry page. */
export function sectionTarget(current: NavTab, section: SectionId): NavTab {
  const s = SECTIONS.find((x) => x.id === section)!;
  return s.tabs.includes(current) ? current : s.entry;
}

// ---- phone bottom bar: Home, Markets, Trading, Alerts, More ----
export type SlotId = "home" | "markets" | "trading" | "alerts" | "more";
export const BOTTOM_SLOTS: { id: SlotId; label: string; icon: NavIconId }[] = [
  { id: "home", label: "Home", icon: "home" },
  { id: "markets", label: "Markets", icon: "flame" },
  { id: "trading", label: "Trading", icon: "lock" },
  { id: "alerts", label: "Alerts", icon: "bell" },
  { id: "more", label: "More", icon: "menu" },
];

/** Which slot is lit. An open sheet or drawer wins; otherwise the slot of the page's section (Paper, System and the rest light More). */
export function activeSlot(tab: NavTab, open: "trading" | "more" | null): SlotId {
  if (open) return open;
  if (tab === "dashboard") return "home";
  if (tab === "notifications") return "alerts";
  const s = sectionOf(tab).id;
  return s === "markets" ? "markets" : s === "trading" ? "trading" : "more";
}

/** The More drawer lists every section that has no bottom-bar slot of its own (Paper, System), so every page stays reachable on a phone. */
export const DRAWER_SECTIONS: SectionId[] = ["paper", "system"];

export const NAV_SECTION_WORDS = SECTIONS.map((s) => s.label);
