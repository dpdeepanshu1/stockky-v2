// Round 262, slice 1: pure helpers of the F&O "Control" card (the four locks, the underlyings choice and the nine strategy switches on F&O Overview).
// The card calls the SAME controlApi.set the Switches page uses, so the LOOSENS confirmation, the audit log and the live / restart behaviour come from the service.
import type { FlagRow } from "./calibrationApi";

export const LOCK_KEYS = ["FNO_ENGINE_ENABLED", "FNO_MODE", "FNO_REAL_ENABLED", "FNO_UNDERLYINGS"] as const;
export const STRAT_PREFIX = "FNO_STRAT_";
export const REAL_PHRASE = "REAL";
/** Strategies that only trade on an expiry day: DEMO only until 30+ DEMO expiry days of evidence exist. */
export const EXPIRY_STRATS = ["FNO_STRAT_EXPIRY_MOMENTUM", "FNO_STRAT_EXPIRY_ALPHA"];

export interface ControlView { locks: FlagRow[]; strategies: FlagRow[] }

/** Picks the rows the card shows, in a stable order: locks as listed above, strategies as the service lists them (bool switches named FNO_STRAT_*). */
export function pickControl(flags: FlagRow[] | null | undefined): ControlView {
  const rows = Array.isArray(flags) ? flags : [];
  const locks = LOCK_KEYS.map((k) => rows.find((r) => r.key === k)).filter((r): r is FlagRow => !!r);
  const strategies = rows.filter((r) => r.key.startsWith(STRAT_PREFIX) && r.kind === "bool");
  return { locks, strategies };
}

/** The value a row has right now: what the running service uses, else the desired / effective one, else the shipped default. */
export function currentValue(r: FlagRow): string {
  const v = r.running ?? r.effective ?? r.desired ?? r.shipped ?? "";
  return String(v).toLowerCase();
}

export const isOn = (r: FlagRow): boolean => currentValue(r) === "true";

/** The value a click on a bool switch sends. */
export const toggledValue = (r: FlagRow): string => (isOn(r) ? "false" : "true");

/** Changes that can send real orders need a typed word, not just an OK button. */
export function needsTypedConfirm(key: string, value: string): boolean {
  const v = String(value).toLowerCase();
  return (key === "FNO_REAL_ENABLED" && v === "true") || (key === "FNO_MODE" && v === "real");
}

export function typedConfirmOk(typed: string | null | undefined): boolean {
  return (typed ?? "").trim().toUpperCase() === REAL_PHRASE;
}

export const valueText = (r: FlagRow, v: string): string => (r.kind === "bool" ? (String(v).toLowerCase() === "true" ? "ON" : "OFF") : String(v).toUpperCase());

/** One line after a change, from the service's SetResult. */
export function resultText(r: FlagRow, res: { change?: string; value?: string; restart_required?: boolean }): string {
  const val = valueText(r, res.value ?? "");
  if (res.change === "SAME") return `${r.title}: already ${val}.`;
  if (res.restart_required) return `${r.title}: saved as ${val}, takes effect after a restart (outside market hours).`;
  return `${r.title}: now ${val}, effective immediately (${(res.change || "changed").toLowerCase()}).`;
}

/** How many of the switches differ from their recommended value (a quick "is anything looser than advised" count). */
export function offRecommended(v: ControlView): number {
  return [...v.locks, ...v.strategies].filter((r) => r.recommended != null && currentValue(r) !== String(r.recommended).toLowerCase()).length;
}
