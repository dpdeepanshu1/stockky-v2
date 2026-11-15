// frontend/src/selfImproveView.ts  (round 542, E567, S2 of the Self Improve plan) - pure helpers for the "Self Improve System" tab.
// The tab reads three GET routes of calibration-runner-service (round 541): /agent/loop, /agent/history and /agent/settings. Nothing here decides or
// sets anything: every function turns data the runner already returned into words, counts and tones. A part whose state is unknown is shown as unknown,
// never guessed. No imports except erased types, no network: covered by selfImprove.test.mjs.
import type { AgentScoreboard, AgentUsage } from "./agentView";

export type StepState = "done" | "waiting" | "skipped" | "failed" | "not_due" | "unknown";
export type Tone = "good" | "warn" | "bad" | undefined;

export interface LoopStep { id: string; title: string; state: StepState; detail: string }
export interface LoopBanner { state: "off" | "dry_run" | "live"; text: string; unpinned: string[] }
export interface NextRun { state: "off" | "due_now" | "scheduled"; at_ist: string | null; reason: string }
export interface SlotView { id: string; pack: string; time: string; days: string[]; enabled: boolean; today: string; next_ist: string | null }
export interface AuditLine { at: number; at_ist: string; by: string; reset: boolean; changes: Record<string, { from: unknown; to: unknown }> }
export interface AgentSettingsView {
  values: { mode: string; run_after: string; max_ideas: number; max_queue: number };
  env_defaults: Record<string, unknown>; saved: Record<string, unknown>; source: Record<string, "saved" | "env">;
  bounds: { modes: string[]; run_after: string[]; max_ideas: number[]; max_queue: number[] }; audit: AuditLine[];
}
export interface LoopChange { at: number; title: string; from: unknown; to: unknown; change: string; reason: string; restart_required?: boolean }
/** Round 544 (S4): did the agent run today. `missed` is only set after the warn time (17:30 IST by default), the same moment the runner sends its one Telegram warning. */
export interface Proof { state: "ok" | "waiting" | "missed" | "off" | "weekend"; detail: string; day: string; warn_after_ist: string; runs_today: number; scheduled_today: number; ran_today: boolean; warned_today: boolean }
export interface RunLogLine { at: number; at_ist: string; day: string; ok: boolean; trigger?: string; mode?: string; by?: string; source?: string; ideas?: number; dropped?: number; queued?: number; errors?: number; note?: string; file?: string; error?: string }
export interface LoopView {
  now_ist: string; weekday: string; banner: LoopBanner;
  agent: {
    mode: string; mode_effective: string; stopped: { at?: number; date?: string; reason?: string } | null; run_after_ist: string; next_run: NextRun;
    last: { date?: string; at?: number; mode?: string; trigger?: string; source?: string; note?: string | null; ideas?: number; dropped?: number; queued?: number } | null;
    last_auto_date?: string | null; run_now_allowed: boolean; market_open: boolean; keys: { gemini: boolean; groq: boolean };
    usage_today: AgentUsage; scoreboard: AgentScoreboard; settings: AgentSettingsView; proof?: Proof;
  };
  schedule: { enabled: boolean; slots: SlotView[]; last_skipped?: unknown };
  autocalib: { settings: { enabled: boolean; dry_run: boolean; pinned: string[]; step_pct: number; cooldown_h: number }; last: { at?: number; changed?: number; waiting?: number; trigger?: string; dry_run?: boolean } | null; pending: Record<string, unknown>; recent_changes: LoopChange[] };
  chain: LoopStep[];
  recent_runs: { id: string; kind?: string; target?: string; status?: string; trigger?: string; queued_at?: string; finished_at?: string | null }[];
}

export type Outcome = "pending" | "survived" | "not_confirmed" | "contradicted" | "unclear" | "no_result";
export interface HistoryIdea { switch: string; direction: string; why?: string; support?: string | null; queued_test?: string | null; outcome: Outcome | string }
export interface Breakdown {
  allowed: number; pinned: number; seen: number; no_evidence: number; hold: number; would_change: number; unreachable: number; unreadable: number;
  asked: boolean; ideas: number; dropped: number; summary: string;
}
export interface HistoryDay {
  date: string; unreadable?: boolean; at?: number; mode?: string; trigger?: string; by?: string; source?: string; note?: string | null; auto_calibration?: string | null;
  breakdown?: Breakdown | null; ideas?: HistoryIdea[]; dropped?: { switch: string; reason: string }[]; weaknesses?: string[]; queued_n?: number; skipped_n?: number; errors_n?: number; health?: string[]; file?: string; runs_n?: number;
}
export interface HistoryView { days: HistoryDay[]; count: number; note?: string; runs?: RunLogLine[] }
/** Round 545 (S5): GET /agent/weekly. */
export type WeekState = "ran" | "missed" | "pending" | "weekend" | "off";
export interface WeekDay { date: string; weekday: string; state: WeekState; ideas: number; dropped: number; queued_n: number; file?: string | null }
export interface WeeklyView { from: string; to: string; days: WeekDay[]; weekdays: number; weekdays_ran: number; missed: string[]; ideas: number; outcomes: Record<string, number>; top_switches: { switch: string; n: number }[]; scoreboard: AgentScoreboard; summary: string }

/* ---------- the side list ---------- */
export type SiSection = "today" | "ideas" | "changes" | "controls" | "health";
export interface SiItem { id: SiSection; label: string }
export interface SiGroup { id: string; label: string; items: SiItem[] }
/** The left list, in the order of plan v6: Today / Loop, Ideas, Changes, Controls, Scoreboard & health (Changes and Controls: round 543, S3). */
export const SI_GROUPS: SiGroup[] = [
  { id: "loop", label: "Loop", items: [{ id: "today", label: "Today / Loop" }] },
  { id: "ideas", label: "Ideas", items: [{ id: "ideas", label: "Ideas" }] },
  { id: "changes", label: "Changes", items: [{ id: "changes", label: "What changed" }] },
  { id: "controls", label: "Controls", items: [{ id: "controls", label: "Controls" }] },
  { id: "health", label: "Health", items: [{ id: "health", label: "Scoreboard & health" }] },
];
export const siIds = (groups: SiGroup[] = SI_GROUPS): SiSection[] => groups.flatMap((g) => g.items.map((i) => i.id));

/* ---------- chain steps ---------- */
const STEP_LABEL: Record<StepState, string> = { done: "DONE", waiting: "WAITING", skipped: "SKIPPED", failed: "FAILED", not_due: "NOT DUE", unknown: "UNKNOWN" };
// the mark carries the state without colour
const STEP_MARK: Record<StepState, string> = { done: "✓", waiting: "…", skipped: "⊘", failed: "✕", not_due: "–", unknown: "?" };
export const stepLabel = (s: string): string => STEP_LABEL[s as StepState] || "UNKNOWN";
export const stepMark = (s: string): string => STEP_MARK[s as StepState] || "?";
export function stepTone(s: string): Tone {
  return s === "done" ? "good" : s === "failed" ? "bad" : s === "waiting" || s === "skipped" || s === "unknown" ? "warn" : undefined;
}
/** "2 done · 1 waiting · 1 not due" in chain order of states; "" for an empty chain. */
export function chainSummary(chain: Pick<LoopStep, "state">[]): string {
  const order: StepState[] = ["done", "waiting", "skipped", "failed", "unknown", "not_due"];
  return order.map((s) => [s, chain.filter((c) => c.state === s).length] as const).filter(([, n]) => n > 0).map(([s, n]) => `${n} ${stepLabel(s).toLowerCase()}`).join(" · ");
}

/* ---------- banner, next run, timetable ---------- */
export const bannerTone = (b: Pick<LoopBanner, "state">): Tone => (b.state === "dry_run" ? "warn" : b.state === "live" ? "good" : undefined);
export const bannerLabel = (b: Pick<LoopBanner, "state">): string => (b.state === "dry_run" ? "DRY-RUN" : b.state === "live" ? "LIVE" : "OFF");

export function nextRunText(n: NextRun | null | undefined): string {
  if (!n) return "Next automatic run: unknown";
  if (n.state === "off") return n.reason || "The agent is off: no automatic run.";
  if (n.state === "due_now") return "Due now: the agent starts within a minute.";
  return n.at_ist ? `Next automatic run: ${n.at_ist} IST` : "Next automatic run: unknown";
}

const TODAY_TEXT: Record<string, string> = { fired: "fired today", waiting: "waiting for today's time", not_fired: "time passed, did NOT fire", not_today: "no slot today", off: "slot is off" };
export const slotTodayText = (t: string): string => TODAY_TEXT[t] || t;
export const slotTodayTone = (t: string): Tone => (t === "fired" ? "good" : t === "not_fired" ? "bad" : t === "waiting" ? "warn" : undefined);
const WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
/** ["Mon","Tue","Wed","Thu","Fri"] -> "Mon-Fri"; any other set is listed. */
export function daysText(days: string[]): string {
  if (!days.length) return "no days";
  const idx = days.map((d) => WEEK.indexOf(d));
  if (idx.every((i) => i >= 0) && days.length > 2 && idx.every((v, i) => i === 0 || v === idx[i - 1] + 1)) return `${days[0]}-${days[days.length - 1]}`;
  return days.join(", ");
}
export function slotLine(s: SlotView): string {
  return `${s.pack || s.id} · ${s.time} IST · ${daysText(s.days)}`;
}

/* ---------- the "why no ideas" line and the switch counts ---------- */
export function whyLine(bd: Breakdown | null | undefined): string {
  if (!bd) return "No breakdown was saved for this run (it ran before round 541).";
  return bd.summary || "No summary was saved for this run.";
}
export interface CountChip { label: string; tone: Tone }
/** The counts behind the sentence, in the order a reader asks: how many may it name, how many are pinned, then why the rest gave nothing. */
export function breakdownChips(bd: Breakdown | null | undefined): CountChip[] {
  if (!bd) return [];
  const out: CountChip[] = [{ label: `${bd.allowed} switches it may name`, tone: undefined }, { label: `${bd.pinned} pinned (hidden from it)`, tone: bd.pinned >= bd.allowed && bd.allowed > 0 ? "warn" : undefined }];
  if (bd.no_evidence) out.push({ label: `${bd.no_evidence} no test result`, tone: "warn" });
  if (bd.hold) out.push({ label: `${bd.hold} on hold`, tone: undefined });
  if (bd.would_change) out.push({ label: `${bd.would_change} would change`, tone: "good" });
  if (bd.unreachable) out.push({ label: `${bd.unreachable} unreachable`, tone: "bad" });
  if (bd.unreadable) out.push({ label: `${bd.unreadable} unreadable`, tone: "bad" });
  return out;
}

/* ---------- ideas ---------- */
const OUTCOME_TEXT: Record<string, string> = { pending: "waiting for its test", survived: "survived", not_confirmed: "not confirmed", contradicted: "contradicted", unclear: "unclear", no_result: "no result" };
export const outcomeText = (o: string | null | undefined): string => OUTCOME_TEXT[String(o || "pending")] || String(o);
export function outcomeTone(o: string | null | undefined): Tone {
  return o === "survived" ? "good" : o === "contradicted" ? "bad" : o === "not_confirmed" || o === "unclear" ? "warn" : undefined;
}
/** The label of an idea's support: only the two values the runner writes; an old file without one shows nothing. */
export function supportText(s: string | null | undefined): string {
  return s === "rule_holds" ? "its rule says: no change (unproven)" : s === "rule_agrees" ? "its rule agrees" : "";
}
export const supportTone = (s: string | null | undefined): Tone => (s === "rule_holds" ? "warn" : s === "rule_agrees" ? "good" : undefined);
export const directionText = (d: string): string => (d === "up" ? "▲ UP" : d === "down" ? "▼ DOWN" : String(d).toUpperCase());

/** One line per saved day: "2026-11-15 · report · 2 ideas · 0 dropped · 0 queued". */
export function dayLine(d: HistoryDay): string {
  if (d.unreadable) return `${d.date} · file could not be read`;
  const n = (d.ideas || []).length;
  return `${d.date} · ${d.mode || "-"} · ${n} idea${n === 1 ? "" : "s"} · ${(d.dropped || []).length} dropped · ${d.queued_n ?? 0} queued`;
}
/** Counts of every idea in the history by outcome; the sum is the number of ideas shown. */
export function historyTotals(days: HistoryDay[]): { ideas: number; by: Record<string, number> } {
  const by: Record<string, number> = {};
  let ideas = 0;
  for (const d of days) for (const i of d.ideas || []) { ideas++; by[String(i.outcome || "pending")] = (by[String(i.outcome || "pending")] || 0) + 1; }
  return { ideas, by };
}
export function totalsLine(t: { ideas: number; by: Record<string, number> }): string {
  if (t.ideas === 0) return "No ideas in the saved days yet.";
  const parts = Object.keys(OUTCOME_TEXT).filter((k) => t.by[k]).map((k) => `${t.by[k]} ${OUTCOME_TEXT[k]}`);
  return `${t.ideas} idea${t.ideas === 1 ? "" : "s"} in the saved days: ${parts.join(" · ")}`;
}

/* ---------- health ---------- */
const hm = (s: string | null | undefined): number | null => {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(s || "").trim());
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
};
/** Minutes after the run-after time at which a weekday without a run counts as a MISSED run (the loop checks every 45 s, so 60 is generous). */
export const MISSED_AFTER_MIN = 60;
/** True when it is a weekday, the agent is on, its run-after time passed more than an hour ago and it has not run today. Honest: uses only the runner's own clock. */
export function agentMissedToday(l: Pick<LoopView, "now_ist" | "weekday" | "agent">): boolean {
  if (["Sat", "Sun"].includes(l.weekday) || l.agent.mode_effective === "off") return false;
  const day = String(l.now_ist || "").slice(0, 10);
  if (!day || l.agent.last_auto_date === day || (l.agent.last && l.agent.last.date === day)) return false;
  const now = hm(String(l.now_ist).slice(11, 16));
  const after = hm(l.agent.run_after_ist);
  return now != null && after != null && now >= after + MISSED_AFTER_MIN;
}
/** Round 544: the runner's own proof when it sends one (it matches the Telegram warning); an older runner has none, so the 60-minute rule above stays as the fallback. */
export function missedWarning(l: LoopView): string | null {
  const p = l.agent.proof;
  if (p) return p.state === "missed" ? `${p.detail} (the runner warns by Telegram after ${p.warn_after_ist} IST${p.warned_today ? ", already sent today" : ""}).` : null;
  return agentMissedToday(l) ? `The agent has not run today although its time (${l.agent.run_after_ist} IST) passed over an hour ago.` : null;
}
/** Plain warnings for the health card; empty when nothing needs a person. */
export function healthWarnings(l: LoopView): string[] {
  const w: string[] = [];
  if (l.agent.stopped) w.push(`The agent switched itself off on ${l.agent.stopped.date || "an earlier day"}. Press Resume to start counting again.`);
  const missed = missedWarning(l);
  if (missed) w.push(missed);
  if (!l.agent.keys.gemini && !l.agent.keys.groq) w.push("No LLM key is set (GEMINI_API_KEY or GROQ_API_KEY): the agent cannot ask a model.");
  if (l.agent.usage_today.left <= 0) w.push(`Today's call cap is used up (${l.agent.usage_today.cap}).`);
  else if (l.agent.usage_today.gemini_blocked) w.push("Gemini's daily quota is used up: Groq answers.");
  if (!l.schedule.enabled) w.push("The test timetable is switched off: the evening tests will not start by themselves.");
  for (const s of l.schedule.slots) if (s.today === "not_fired") w.push(`The ${s.pack || s.id} slot (${s.time} IST) did not fire today. A missed slot is not caught up.`);
  for (const c of l.chain) if (c.state === "failed") w.push(`${c.title}: ${c.detail}`);
  return w;
}

/** The two-click rule as one pure step: the first click on an action arms it, a second click on the SAME action fires it, any other click re-arms. */
export function nextArm(armed: string | null, clicked: string): { armed: string | null; fire: boolean } {
  return armed === clicked ? { armed: null, fire: true } : { armed: clicked, fire: false };
}

/** Settings in force as short lines: "mode report (from .env)", "run after 16:10 IST (saved)". */
export function settingLines(s: AgentSettingsView): string[] {
  const src = (k: string) => (s.source?.[k] === "saved" ? "saved on this page" : "from .env");
  return [
    `Mode: ${s.values.mode} (${src("mode")})`, `Run after: ${s.values.run_after} IST (${src("run_after")})`,
    `Max ideas a night: ${s.values.max_ideas} (${src("max_ideas")})`, `Max queued tests a night: ${s.values.max_queue} (${src("max_queue")})`,
  ];
}
/** One audit line as text: "2026-11-15 12:01 by admin: mode report -> propose". */
export function auditText(a: AuditLine): string {
  const ch = Object.entries(a.changes || {}).map(([k, v]) => `${k} ${String(v.from)} -> ${String(v.to)}`).join(", ");
  return `${a.at_ist} by ${a.by}: ${a.reset ? "reset to .env" + (ch ? "; " : "") : ""}${ch || (a.reset ? "" : "no change")}`;
}

/* ---------- round 544 / 545: proof, run log, weekly review ---------- */
const PROOF_TEXT: Record<string, string> = { ok: "RAN TODAY", waiting: "NOT YET", missed: "DID NOT RUN", off: "OFF", weekend: "WEEKEND" };
export const proofLabel = (p: Pick<Proof, "state"> | null | undefined): string => (p ? PROOF_TEXT[p.state] || "UNKNOWN" : "UNKNOWN");
export const proofTone = (p: Pick<Proof, "state"> | null | undefined): Tone => (!p ? undefined : p.state === "ok" ? "good" : p.state === "missed" ? "bad" : p.state === "waiting" ? "warn" : undefined);
/** One run-log line as text: "2026-11-16 16:12 · scheduled · gemini · 2 ideas, 1 dropped, 0 queued" or "... · CRASHED: boom". A manual run says who. */
export function runLogText(r: RunLogLine): string {
  const when = String(r.at_ist || "").slice(0, 16);
  const kind = r.trigger === "schedule" ? "scheduled" : `manual${r.by ? " by " + r.by : ""}`;
  if (r.ok === false) return `${when} · ${kind} · CRASHED: ${r.error || "unknown error"}`;
  const n = r.ideas ?? 0;
  return `${when} · ${kind} · ${r.source || "no LLM answer"} · ${n} idea${n === 1 ? "" : "s"}, ${r.dropped ?? 0} dropped, ${r.queued ?? 0} queued${r.errors ? `, ${r.errors} error(s)` : ""}`;
}
const WEEK_MARK: Record<string, string> = { ran: "✓", missed: "✕", pending: "…", weekend: "–", off: "⊘" };
const WEEK_TEXT: Record<string, string> = { ran: "ran", missed: "MISSED", pending: "not yet", weekend: "weekend", off: "agent off" };
export const weekMark = (s: string): string => WEEK_MARK[s] || "?";
export const weekText = (s: string): string => WEEK_TEXT[s] || s;
export const weekTone = (s: string): Tone => (s === "ran" ? "good" : s === "missed" ? "bad" : s === "pending" ? "warn" : undefined);
/** "Mon 11-16: ✓ ran · 2 ideas" for one day of the weekly card. */
export function weekDayLine(d: WeekDay): string {
  return `${d.weekday} ${String(d.date).slice(5)}: ${weekMark(d.state)} ${weekText(d.state)}${d.ideas ? ` · ${d.ideas} idea${d.ideas === 1 ? "" : "s"}` : ""}`;
}
