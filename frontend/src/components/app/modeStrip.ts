// E535: the always-visible mode strip on the four Trading pages. Each page PUBLISHES what it already knows (mode, armed, today's P&L) and the
// strip only READS it: this file imports nothing, makes no call and has no handler, so it cannot change what a page does.
export type StripMode = "DEMO" | "REAL";
export interface StripSnapshot { mode: StripMode; armed: boolean | null; pnl: number | null }
interface Stored extends StripSnapshot { at: number }

const store = new Map<string, Stored>();
const listeners = new Set<() => void>();
let version = 0;

export function publishModeStrip(tab: string, snap: StripSnapshot | null, nowMs: number = Date.now()): void {
  if (snap) store.set(tab, { ...snap, at: nowMs }); else store.delete(tab);
  version++;
  listeners.forEach((l) => l());
}
export function readModeStrip(tab: string): Stored | null { return store.get(tab) ?? null; }
export function subscribeModeStrip(fn: () => void): () => void { listeners.add(fn); return () => { listeners.delete(fn); }; }
export function modeStripVersion(): number { return version; }

export const STALE_AFTER_MS = 90_000;

/** "+₹1,250", "-₹340", "₹0": the sign is in the text, Indian digit grouping, never a minus on a zero. */
export function signedRupees(n: number): string {
  const r = Math.round(n);
  const body = Math.abs(r).toLocaleString("en-IN");
  return r > 0 ? `+₹${body}` : r < 0 ? `-₹${body}` : "₹0";
}

export type StripTone = "demo" | "real" | "locked" | "control";
export interface StripView {
  tone: StripTone;
  modeText: string;
  armedText: string | null;
  armedOn: boolean;
  pnlText: string | null;
  pnlTone: "up" | "down" | "flat" | null;
  stale: boolean;
  hint: string;
}

export function stripView(tab: string, snap: Stored | null, nowMs: number = Date.now(), hasSession: boolean = false): StripView {
  if (tab === "calibration") {
    return { tone: "control", modeText: "Control room", armedText: null, armedOn: false, pnlText: null, pnlTone: null, stale: false, hint: "Changes here alter the rules real-money trading follows." };
  }
  if (!snap && hasSession) {
    return { tone: "locked", modeText: "Mode not loaded yet", armedText: null, armedOn: false, pnlText: null, pnlTone: null, stale: false, hint: "Waiting for this page to report its mode, armed state and today's P&L." };
  }
  if (!snap) {
    return { tone: "locked", modeText: "Locked", armedText: null, armedOn: false, pnlText: null, pnlTone: null, stale: false, hint: "Log in to see the mode, armed state and today's P&L." };
  }
  const real = snap.mode === "REAL";
  const stale = nowMs - snap.at > STALE_AFTER_MS;
  const armedText = snap.armed === null ? null : snap.armed ? "Armed" : "Not armed";
  const pnl = snap.pnl;
  const pnlTone = pnl === null || !Number.isFinite(pnl) ? null : pnl > 0 ? "up" : pnl < 0 ? "down" : "flat";
  const pnlText = pnl === null || !Number.isFinite(pnl) ? null : `${pnl > 0 ? "▲ " : pnl < 0 ? "▼ " : ""}${signedRupees(pnl)}`;
  return {
    tone: real ? "real" : "demo",
    modeText: real ? "REAL money" : "DEMO (practice)",
    armedText, armedOn: snap.armed === true, pnlText, pnlTone, stale,
    hint: real ? "Orders on this page use real money." : "Practice mode: no real orders are sent.",
  };
}
