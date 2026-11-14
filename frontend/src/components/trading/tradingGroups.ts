// E537 (redesign round 5): the tab groups and the header numbers for the two trading pages, as pure functions (no import, no call, no state).
// Every old tab id stays; a group only decides which ids sit under one top-level tab. Auto Trade and Position Stocks share this one pattern.

export interface TabGroup<T extends string> { id: string; label: string; tabs: T[] }

export type RtTab = "overview" | "board" | "live" | "positions" | "orders" | "history" | "watchlist" | "pipeline" | "charges" | "log";
export type PsTab = "overview" | "board" | "timemachine" | "pipeline" | "screener" | "positions" | "history" | "dhanorders" | "charges" | "settings";

/** Auto Trade: Now, Orders, History, Pipeline, Log (plan: "Tabs reduced to 5 groups"). */
export const RT_GROUPS: TabGroup<RtTab>[] = [
  { id: "now", label: "Now", tabs: ["overview", "live", "positions"] },
  { id: "orders", label: "Orders", tabs: ["orders"] },
  { id: "history", label: "History", tabs: ["history", "charges"] },
  { id: "pipeline", label: "Pipeline", tabs: ["pipeline", "board", "watchlist"] },
  { id: "log", label: "Log", tabs: ["log"] },
];

/** Position Stocks: Now, Board, Screener, History, Tools. */
export const PS_GROUPS: TabGroup<PsTab>[] = [
  { id: "now", label: "Now", tabs: ["overview", "positions"] },
  { id: "board", label: "Board", tabs: ["board"] },
  { id: "screener", label: "Screener", tabs: ["screener"] },
  { id: "history", label: "History", tabs: ["history", "charges", "dhanorders"] },
  { id: "tools", label: "Tools", tabs: ["timemachine", "pipeline", "settings"] },
];

export function groupOf<T extends string>(groups: TabGroup<T>[], tab: T): TabGroup<T> {
  return groups.find((g) => g.tabs.includes(tab)) ?? groups[0];
}

/** Clicking a group keeps the page you are on if it is inside that group, otherwise opens the group's first page. */
export function tabForGroup<T extends string>(groups: TabGroup<T>[], groupId: string, current: T): T {
  const g = groups.find((x) => x.id === groupId);
  if (!g) return current;
  return g.tabs.includes(current) ? current : g.tabs[0];
}

/** True when every id appears in exactly one group (nothing becomes unreachable). */
export function groupsCover<T extends string>(groups: TabGroup<T>[], ids: readonly T[]): boolean {
  const seen = groups.flatMap((g) => g.tabs);
  return seen.length === ids.length && ids.every((id) => seen.filter((s) => s === id).length === 1);
}

const fin = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);

/** "+₹1,250", "-₹340", "₹0": the sign is in the text, Indian digit grouping, never a minus on a zero. */
export function signedRupees(n: number): string {
  const r = Math.round(n);
  const body = Math.abs(r).toLocaleString("en-IN");
  return r > 0 ? `+₹${body}` : r < 0 ? `-₹${body}` : "₹0";
}
export const plainRupees = (n: number): string => `₹${Math.round(n).toLocaleString("en-IN")}`;

export interface HeaderInput {
  mode: "DEMO" | "REAL";
  armed: boolean | null;
  killed?: boolean;
  pnl: number | null | undefined;
  /** total capital the page works with, and what is free; "in use" is the difference. Either may be missing. */
  total: number | null | undefined;
  idle: number | null | undefined;
}
export interface HeaderView {
  modeText: string;
  real: boolean;
  armedState: "armed" | "paused" | "offline" | null;
  armedText: string | null;
  pnlText: string | null;
  pnlTone: "up" | "down" | "flat";
  inUseText: string | null;
  idleText: string | null;
  canPause: boolean;
}

export function headerView(i: HeaderInput): HeaderView {
  const real = i.mode === "REAL";
  const armedState: HeaderView["armedState"] = i.killed ? "offline" : i.armed === null ? null : i.armed ? "armed" : "paused";
  const armedText = i.killed ? "Halted by loss limit" : i.armed === null ? null : i.armed ? "Armed" : "Not armed";
  const pnl = fin(i.pnl) ? i.pnl : null;
  const inUse = fin(i.total) && fin(i.idle) ? Math.max(0, i.total - i.idle) : null;
  return {
    modeText: real ? "REAL money" : "DEMO (practice)",
    real,
    armedState, armedText,
    pnlText: pnl === null ? null : `${pnl > 0 ? "▲ " : pnl < 0 ? "▼ " : ""}${signedRupees(pnl)}`,
    pnlTone: pnl === null || pnl === 0 ? "flat" : pnl > 0 ? "up" : "down",
    inUseText: inUse === null ? null : plainRupees(inUse),
    idleText: fin(i.idle) ? plainRupees(i.idle) : null,
    canPause: i.armed === true && !i.killed,
  };
}

// ── The arming flow as three steps ──
export type StepState = "done" | "current" | "todo";
export interface ArmStep { n: number; label: string; hint: string; state: StepState }

/**
 * The four old gates (admin login, Dhan, risk config, armed) as three steps: 1 Login and broker, 2 Risk confirmed, 3 Armed.
 * Nothing is hidden: the hint of step 1 still says which of the two (login or Dhan) is missing. `dhanRequired` is false in DEMO.
 */
export function armSteps(g: { gate1: boolean; gate2: boolean; gate3: boolean; armed: boolean; dhanRequired?: boolean }): ArmStep[] {
  const dhan = g.dhanRequired !== false;
  const s1 = g.gate1 && g.gate2;
  const raw = [
    { label: dhan ? "Login and Dhan" : "Login", done: s1, hint: !g.gate1 ? "Admin login needed" : !g.gate2 ? "Dhan is not connected" : dhan ? "Logged in, Dhan connected" : "Logged in (Dhan not needed in DEMO)" },
    { label: "Risk confirmed", done: g.gate3, hint: g.gate3 ? "Risk settings confirmed" : "Confirm the risk settings" },
    { label: "Armed", done: g.armed, hint: g.armed ? "Auto-orders are on" : s1 && g.gate3 ? "Ready to arm" : "Finish the steps before" },
  ];
  const firstOpen = raw.findIndex((r) => !r.done);
  return raw.map((r, i) => ({ n: i + 1, label: r.label, hint: r.hint, state: r.done ? "done" : i === firstOpen ? "current" : "todo" }));
}
