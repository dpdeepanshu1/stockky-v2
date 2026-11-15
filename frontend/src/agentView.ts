// frontend/src/agentView.ts  (round 536, agent Round 6) - pure helpers for the AI agent card on Real Money Calibration > Auto-calibration.
// No imports and no network: the card (components/calibration/AgentCard.tsx) passes in what GET /agent/status returned. The agent only suggests; this file
// never decides anything and never mentions a key (the status carries only "key is set" flags). Covered by agentView.test.mjs.

export type AgentIdea = { switch: string; direction: string; why?: string };
export interface AgentScoreboard {
  ideas: number; survived: number; not_confirmed: number; contradicted: number; unclear: number; no_result: number; tested: number; pending: number;
  survival_pct: number | null; dropped: number; since_resume: boolean; rule: string;
}
export interface AgentUsage { date: string; gemini: number; groq: number; runs: number; used: number; cap: number; left: number; gemini_blocked: boolean }
export interface AgentReport {
  date?: string; at?: number; mode?: string; trigger?: string; source?: string; note?: string | null; weaknesses: string[]; ideas: AgentIdea[];
  queued: { test: string; switch: string }[]; skipped: { test: string; reason: string }[]; dropped: number; health: string[];
}
export interface AgentStatus {
  mode: string; mode_effective: string; stopped: { at?: number; date?: string; reason?: string } | null;
  run_after_ist: string; max_ideas: number; max_queue: number; market_open: boolean; run_now_allowed: boolean;
  keys: { gemini: boolean; groq: boolean }; models: { gemini?: string; gemini_backup?: string; groq?: string };
  last: { date?: string; at?: number; mode?: string; trigger?: string; source?: string; note?: string | null; ideas?: number; dropped?: number; queued?: number } | null;
  scoreboard: AgentScoreboard; usage_today: AgentUsage; last_report: AgentReport | null;
}
export interface AgentRunResult {
  ran: boolean; reason?: string; market_open?: boolean; date?: string; mode?: string; source?: string; note?: string | null; stopped?: string | null;
  ideas?: AgentIdea[]; queued?: { test: string; switch: string }[]; skipped?: { test: string; reason: string }[]; dropped?: unknown[]; health?: string[];
}

export type Headline = { kind: "stopped" | "off" | "report" | "propose"; tone: "good" | "warn" | "bad" | undefined; text: string };

/** The one line that says what the agent is doing now. A self-off is spelled out with its date; propose is "warn" because it also queues tests. */
export function headline(st: Pick<AgentStatus, "mode" | "mode_effective" | "stopped">): Headline {
  if (st.stopped) return { kind: "stopped", tone: "bad", text: `OFF: it switched itself off on ${st.stopped.date || "an earlier day"}` };
  const m = st.mode_effective || st.mode;
  if (m === "off") return { kind: "off", tone: "bad", text: "OFF: AGENT_MODE is off" };
  if (m === "propose") return { kind: "propose", tone: "warn", text: "PROPOSE: writes ideas and also queues the existing tests behind them" };
  return { kind: "report", tone: "good", text: "REPORT: writes ideas and sends a Telegram message, queues nothing" };
}

/** Why the "Run now" buttons are disabled; "" when they are free. */
export function runBlock(st: Pick<AgentStatus, "stopped" | "mode_effective" | "market_open" | "keys" | "usage_today">): string {
  if (st.stopped) return "The agent switched itself off. Press Resume first.";
  if (st.mode_effective === "off") return "AGENT_MODE is off.";
  if (st.market_open) return "The market is open (Mon-Fri 09:00-15:45 IST). Run it after 15:45.";
  if (!st.keys.gemini && !st.keys.groq) return "No LLM key is set (GEMINI_API_KEY or GROQ_API_KEY).";
  if (st.usage_today.left <= 0) return `Today's call cap is used up (${st.usage_today.cap}).`;
  return "";
}

export const keyLine = (k: { gemini: boolean; groq: boolean }): string => `Gemini key ${k.gemini ? "set" : "NOT set"} · Groq key ${k.groq ? "set" : "NOT set"}`;

export function usageLine(u: AgentUsage): string {
  const extra = u.gemini_blocked ? " · Gemini daily quota used up, Groq answers" : "";
  return `LLM calls today: ${u.used} of ${u.cap} (Gemini ${u.gemini}, Groq ${u.groq})${extra}`;
}

export function scoreLine(sb: AgentScoreboard): string {
  if (sb.ideas === 0) return "No ideas judged yet" + (sb.since_resume ? " since Resume" : "");
  const pct = sb.survival_pct == null ? "-" : `${sb.survival_pct}%`;
  return `${sb.ideas} ideas · ${sb.tested} tested · ${sb.survived} survived (${pct}) · ${sb.contradicted} contradicted · ${sb.not_confirmed} not confirmed · ${sb.pending} waiting` + (sb.since_resume ? " · counted since Resume" : "");
}

export const scoreTone = (sb: AgentScoreboard): "good" | "warn" | "bad" | undefined =>
  sb.survival_pct == null ? undefined : sb.survival_pct >= 20 ? "good" : sb.survival_pct >= 5 ? "warn" : "bad";

/** "2026-11-16T16:10 IST" from epoch seconds; "-" for junk. */
export function whenIst(ts: number | null | undefined): string {
  if (typeof ts !== "number" || !Number.isFinite(ts)) return "-";
  const d = new Date(ts * 1000 + 5.5 * 3600 * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} IST`;
}

/** What to show under the buttons after "Run now": the refusal reason, or ideas, queued tests and warnings. */
export function runResultLines(r: AgentRunResult): string[] {
  if (!r.ran) return [r.reason || "The agent did not run."];
  const out: string[] = [];
  if (r.note) out.push(r.note);
  for (const i of r.ideas || []) out.push(`${i.switch} ${i.direction}${i.why ? `: ${i.why}` : ""}`);
  if ((r.ideas || []).length === 0 && !r.note) out.push("No ideas this time.");
  if (r.queued && r.queued.length) out.push("Queued tests: " + Array.from(new Set(r.queued.map((q) => q.test))).join(", "));
  if (r.skipped && r.skipped.length) out.push(`Not queued: ${r.skipped.length} (${r.skipped[0].reason})`);
  for (const h of r.health || []) out.push("Warning: " + h);
  if (r.stopped) out.push("The agent switched itself off: " + r.stopped);
  return out;
}
