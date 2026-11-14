// E539 (redesign round 6): the Real Money Calibration side list, the inline run progress and the "last changed" line of a switch. Pure: no imports, no network.
export type CalSection = "switches" | "alerts" | "auto" | "weather" | "research" | "short" | "tape" | "setups" | "swing" | "reconcile" | "probes" | "trades" | "plan" | "tests" | "runs";
export interface CalItem { id: CalSection; label: string }
export interface CalGroup { id: string; label: string; items: CalItem[] }

/** the left list: every one of the 15 old section ids appears exactly once, in groups. */
export const CAL_GROUPS: CalGroup[] = [
  { id: "control", label: "Control", items: [{ id: "switches", label: "Switches" }, { id: "alerts", label: "Alert switches" }, { id: "auto", label: "Auto-calibration" }] },
  { id: "market", label: "Market", items: [{ id: "weather", label: "Market Weather" }, { id: "research", label: "Market research" }, { id: "short", label: "Short selling" }, { id: "tape", label: "Order-flow tape" }, { id: "swing", label: "Swing lane" }] },
  { id: "checks", label: "Checks", items: [{ id: "setups", label: "Setup scorecard" }, { id: "reconcile", label: "Reconciliation" }, { id: "probes", label: "Status probes" }] },
  { id: "runs", label: "Runs", items: [{ id: "trades", label: "Daily trades" }, { id: "plan", label: "Run plan" }, { id: "tests", label: "Tests & training" }, { id: "runs", label: "Runs & downloads" }] },
];

export const calIds = (groups: CalGroup[] = CAL_GROUPS): string[] => groups.flatMap((g) => g.items.map((i) => i.id));
export function calCovers(groups: CalGroup[], all: string[]): boolean {
  const ids = calIds(groups);
  return ids.length === all.length && all.every((a) => ids.filter((x) => x === a).length === 1);
}
export const calGroupOf = (id: string, groups: CalGroup[] = CAL_GROUPS): CalGroup | null => groups.find((g) => g.items.some((i) => i.id === id)) || null;
/** sections that show the "Run all" bar (the same rule the page had as a long && chain). */
export const NO_RUN_ALL: CalSection[] = ["runs", "alerts", "weather", "research", "swing", "reconcile", "tape", "setups", "probes"];
export const showsRunAll = (s: CalSection): boolean => !NO_RUN_ALL.includes(s);

/** how much of its time limit a running job has used, as a whole percent (0 to 100). Honest: this is time used, not work done. */
export function runProgress(startedAt: string | null | undefined, timeoutS: number | null | undefined, now: number = Date.now()): { pct: number; usedS: number } | null {
  if (!startedAt || !timeoutS || timeoutS <= 0) return null;
  const t = new Date(startedAt).getTime();
  if (!Number.isFinite(t)) return null;
  const usedS = Math.max(0, Math.round((now - t) / 1000));
  return { pct: Math.max(0, Math.min(100, Math.round((usedS / timeoutS) * 100))), usedS };
}

/** the visible "last changed" line of a switch row. */
export function lastChangedText(o: { by?: string; at?: string } | null | undefined): string {
  if (!o || !o.at) return "Last changed: not changed from this page";
  const at = String(o.at).slice(0, 16).replace("T", " ");
  return `Last changed: ${at}${o.by ? ` by ${o.by}` : ""}`;
}

/** the one-line "what it does" under a switch name: the first sentence of its help text, cut at 140 characters. */
export function oneLine(help: string | null | undefined, max = 140): string {
  const t = String(help || "").replace(/\s+/g, " ").trim();
  if (!t) return "";
  const m = t.match(/^.*?[.!?](?=\s|$)/);
  const first = (m ? m[0] : t).trim();
  return first.length <= max ? first : `${first.slice(0, max - 1).trimEnd()}…`;
}
