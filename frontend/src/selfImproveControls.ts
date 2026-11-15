// frontend/src/selfImproveControls.ts  (round 543, E568, S3 of the Self Improve plan) - pure helpers for the Controls and Changes sections of the
// "Self Improve System" tab. Nothing here sends a request or sets anything: it validates a form, decides whether a click needs a second click, builds the
// request body the runner already accepts (POST /agent/settings, PUT /autocalib/settings) and turns history into words. Covered by selfImproveControls.test.mjs.
import type { AgentSettingsView, AuditLine, Tone } from "./selfImproveView";
import type { AutoChange, AutoRuleRow, AutoSettings } from "./calibrationApi";

/** "2026-11-16 16:10 IST" from epoch seconds; "-" for junk. Same text as agentView.whenIst, kept here so this module has no runtime import (the node test runner cannot resolve one). */
export function whenIst(ts: number | null | undefined): string {
  if (typeof ts !== "number" || !Number.isFinite(ts)) return "-";
  const d = new Date(ts * 1000 + 5.5 * 3600 * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} IST`;
}

/* ---------- agent settings form ---------- */
export interface AgentDraft { mode: string; run_after: string; max_ideas: string; max_queue: string }
export type AgentValues = AgentSettingsView["values"];
export type AgentBounds = AgentSettingsView["bounds"];

export const draftFrom = (v: AgentValues): AgentDraft => ({ mode: String(v.mode), run_after: String(v.run_after), max_ideas: String(v.max_ideas), max_queue: String(v.max_queue) });

const hm = (s: string): number | null => {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(s).trim());
  if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) return null;
  return Number(m[1]) * 60 + Number(m[2]);
};
const wholeIn = (s: string, lo: number, hi: number): number | null => (/^\d{1,3}$/.test(String(s).trim()) && Number(s) >= lo && Number(s) <= hi ? Number(s) : null);

/** One message per bad field, using the bounds the runner reported; {} when the form is fine. The runner checks again: this only saves a round trip. */
export function validateDraft(d: AgentDraft, b: AgentBounds): Record<string, string> {
  const e: Record<string, string> = {};
  if (!b.modes.includes(d.mode)) e.mode = `Mode must be ${b.modes.join(" / ")}.`;
  const t = hm(d.run_after), lo = hm(b.run_after[0]), hi = hm(b.run_after[1]);
  if (t == null) e.run_after = "Run-after must look like 16:10.";
  else if (lo != null && hi != null && (t < lo || t > hi)) e.run_after = `Run-after must be between ${b.run_after[0]} and ${b.run_after[1]} IST.`;
  if (wholeIn(d.max_ideas, b.max_ideas[0], b.max_ideas[1]) == null) e.max_ideas = `Max ideas must be a whole number from ${b.max_ideas[0]} to ${b.max_ideas[1]}.`;
  if (wholeIn(d.max_queue, b.max_queue[0], b.max_queue[1]) == null) e.max_queue = `Max queued tests must be a whole number from ${b.max_queue[0]} to ${b.max_queue[1]}.`;
  return e;
}

/** Only the fields that differ from the values in force, as the runner expects them (numbers as numbers). An invalid draft gives an empty body. */
export function draftBody(values: AgentValues, d: AgentDraft, b: AgentBounds): { body: Record<string, string | number>; changed: string[] } {
  if (Object.keys(validateDraft(d, b)).length) return { body: {}, changed: [] };
  const body: Record<string, string | number> = {};
  if (d.mode !== values.mode) body.mode = d.mode;
  if (d.run_after.trim() !== values.run_after) body.run_after = d.run_after.trim();
  if (Number(d.max_ideas) !== Number(values.max_ideas)) body.max_ideas = Number(d.max_ideas);
  if (Number(d.max_queue) !== Number(values.max_queue)) body.max_queue = Number(d.max_queue);
  return { body, changed: Object.keys(body) };
}
/** The second-click rule for the agent: only the move INTO propose widens what can happen (it queues tests). Off and report are one click. */
export const agentNeedsTwoClicks = (values: AgentValues, body: Record<string, unknown>): boolean => body.mode === "propose" && values.mode !== "propose";
/** Reset drops the saved values: back to the .env ones. That widens only when the .env mode is propose and the mode in force is not. */
export const resetNeedsTwoClicks = (s: Pick<AgentSettingsView, "values" | "env_defaults">): boolean => String(s.env_defaults?.mode) === "propose" && s.values.mode !== "propose";
/** "mode report → propose · max ideas 5 → 3" for the confirm line. */
export function draftChangeText(values: AgentValues, body: Record<string, string | number>): string {
  const label: Record<string, string> = { mode: "mode", run_after: "run after", max_ideas: "max ideas", max_queue: "max queued tests" };
  return Object.keys(body).map((k) => `${label[k] || k} ${String((values as unknown as Record<string, unknown>)[k])} → ${String(body[k])}`).join(" · ");
}

/* ---------- auto-calibration controls ---------- */
export type AcAction =
  | { kind: "enabled"; to: boolean }
  | { kind: "dry_run"; to: boolean }
  | { kind: "pin"; key: string; to: boolean };   // to = the NEW pinned state

/** Two clicks for every move that widens what can happen: dry-run OFF (changes become real), UNPIN (a switch becomes eligible), and switching ON while
 *  dry-run is already off (it would start applying). One click for the narrowing moves: switch OFF, dry-run ON, pin, and switching ON while dry-run is on. */
export function acNeedsTwoClicks(a: AcAction, s: Pick<AutoSettings, "enabled" | "dry_run">): boolean {
  if (a.kind === "dry_run") return a.to === false;
  if (a.kind === "pin") return a.to === false;
  return a.to === true && s.dry_run === false;
}
export const acArmId = (a: AcAction): string => (a.kind === "pin" ? `pin:${a.key}:${a.to ? "pin" : "unpin"}` : `${a.kind}:${a.to ? "on" : "off"}`);
/** The body of PUT /autocalib/settings. A pin change sends the WHOLE new list (the route replaces it), sorted and without duplicates. */
export function acBody(a: AcAction, pinned: string[], confirm = false): { enabled?: boolean; dry_run?: boolean; pinned?: string[]; confirm?: true } {
  const c = confirm ? { confirm: true as const } : {};          // round 547: the runner itself refuses a widening move without confirm (409)
  if (a.kind === "enabled") return { enabled: a.to, ...c };
  if (a.kind === "dry_run") return { dry_run: a.to, ...c };
  const set = new Set(pinned);
  if (a.to) set.add(a.key); else set.delete(a.key);
  return { pinned: [...set].sort(), ...c };
}
/** What the second click will do, in plain words (shown on the armed button's note). */
export function acConfirmText(a: AcAction): string {
  if (a.kind === "dry_run") return "Turning dry-run OFF means auto-calibration changes real switches after the next finished test (2 agreeing runs to loosen, max 25 % a step, 24 h cool-down).";
  if (a.kind === "pin") return `Unpinning ${a.key} lets auto-calibration change it by itself from its test result.`;
  return "Switching ON with dry-run already off means auto-calibration starts changing real switches.";
}
export function acButtonText(a: AcAction, armed: boolean): string {
  if (a.kind === "dry_run") return armed ? "Click again: turn dry-run OFF" : a.to ? "Dry-run ON (applies nothing)" : "Turn dry-run OFF";
  if (a.kind === "enabled") return armed ? "Click again: switch ON" : a.to ? "Switch ON" : "Switch OFF";
  return armed ? "Click again: unpin" : a.to ? "Pin" : "Unpin";
}

/* ---------- the pinned list ---------- */
export type PinGroup = "A" | "B" | "C" | "other";
// the suggested order from plan v6 section 0 (A now, B after a week of clean nights, C last); a switch not listed here is "other".
const GROUP_OF: Record<string, PinGroup> = {
  OVERNIGHT_HOLD_ENABLED: "A", STRUCTURE_EXIT_MODE: "A",
  ENTRY_VWAP_GATE_ENABLED: "B", OPENING_GATE_ENABLED: "B", KRONOS_GATE_MODE: "B", TIMESFM_GATE_MODE: "B", STRUCTURE_GATE_MODE: "B", PS_STRUCTURE_GATE_MODE: "B", STRUCTURE_STOP_MODE: "B",
  STRUCTURE_MIN_RR: "C", STRUCTURE_MIN_ROOM_ATR: "C", STRUCTURE_MAX_CHASE_ATR: "C", STRUCTURE_MAX_STRETCH_ATR: "C",
};
export const pinGroup = (key: string): PinGroup => GROUP_OF[key] || "other";
export const PIN_GROUP_TEXT: Record<PinGroup, string> = {
  A: "Group A - one-way: can only tighten or switch OFF (unpin first)",
  B: "Group B - gate on/off or mode: may loosen, needs 2 agreeing runs (after a week of clean nights)",
  C: "Group C - numbers: move at most 25 % a step (last)",
  other: "Other switches with an auto-calibration rule",
};
export interface PinRow { key: string; title: string; service: string; tests: string[]; pinned: boolean; group: PinGroup }
const GROUP_ORDER: PinGroup[] = ["A", "B", "C", "other"];
/** One row per switch that has an auto-calibration rule, ordered group A, B, C, other, then by name. */
export function pinRows(rules: Pick<AutoRuleRow, "key" | "title" | "service" | "tests">[], pinned: string[]): PinRow[] {
  const p = new Set(pinned);
  return rules.map((r) => ({ key: r.key, title: r.title, service: r.service, tests: r.tests, pinned: p.has(r.key), group: pinGroup(r.key) }))
    .sort((a, b) => GROUP_ORDER.indexOf(a.group) - GROUP_ORDER.indexOf(b.group) || a.key.localeCompare(b.key));
}
export function pinSummary(rows: PinRow[]): string {
  const n = rows.filter((r) => r.pinned).length;
  return `${n} pinned (never changed automatically) · ${rows.length - n} unpinned (auto-calibration may change them; the agent can name them)`;
}

/* ---------- protected list (read only) ---------- */
export function protectedRows(p: Record<string, string> | null | undefined): { key: string; why: string }[] {
  return Object.entries(p || {}).map(([key, why]) => ({ key, why })).sort((a, b) => a.key.localeCompare(b.key));
}

/* ---------- Changes ---------- */
const CHANGE_WORD: Record<string, string> = { LOOSENS: "LOOSENED", TIGHTENS: "TIGHTENED", NEUTRAL: "CHANGED" };
export const changeWord = (c: string | null | undefined): string => CHANGE_WORD[String(c || "")] || "CHANGED";
/** Loosening widens what can happen, so it is flagged; tightening and neutral moves are not. */
export const changeTone = (c: string | null | undefined): Tone => (c === "LOOSENS" ? "warn" : undefined);
export function changeHeadline(c: { at: number; title: string; key: string; from: unknown; to: unknown }): string {
  return `${whenIst(c.at)} · ${c.title || c.key}: ${c.from == null ? "-" : String(c.from)} → ${c.to == null ? "-" : String(c.to)}`;
}
/** "applied live" / "saved, needs a service restart" / "saved, not live yet": never says live unless the service said so. */
export function changeApplied(c: Pick<AutoChange, "applied_live" | "restart_required">): string {
  if (c.applied_live) return "applied live";
  return c.restart_required ? "saved, needs a service restart to take effect" : "saved, not live yet";
}
export interface PendingRow { key: string; title: string; to: string; votes: number }
/** Moves waiting for a second, different test run (loosening needs 2). */
export function pendingRows(pending: Record<string, { to: string; votes: string[] }> | null | undefined, rules: Pick<AutoRuleRow, "key" | "title">[]): PendingRow[] {
  const title = new Map(rules.map((r) => [r.key, r.title] as const));
  return Object.entries(pending || {}).map(([key, p]) => ({ key, title: title.get(key) || key, to: String(p?.to ?? "-"), votes: Array.isArray(p?.votes) ? p.votes.length : 0 })).sort((a, b) => a.key.localeCompare(b.key));
}
export function changesSummary(h: Pick<AutoChange, "change">[]): string {
  if (!h.length) return "No switch was changed by auto-calibration yet.";
  const n = (w: string) => h.filter((c) => c.change === w).length;
  return `${h.length} change${h.length === 1 ? "" : "s"} in the history: ${n("TIGHTENS")} tightened · ${n("LOOSENS")} loosened · ${h.length - n("TIGHTENS") - n("LOOSENS")} other`;
}
/** Audit lines of the auto-calibration settings (round 543 runner); an older runner sends none. Same text shape as the agent's audit lines. */
export const acAuditLines = (a: AuditLine[] | null | undefined): AuditLine[] => (Array.isArray(a) ? a : []);
