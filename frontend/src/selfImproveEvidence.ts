// frontend/src/selfImproveEvidence.ts  (round 556, E581) - pure helpers for Self Improve System > Evidence.
// Data: GET /agent/evidence on calibration-runner-service: the same two aggregate blocks the AI agent reads as context (real-trade evidence verdict; strategy registry + drift),
// each with one plain sentence. No rupee amount is ever in it. Nothing here decides or sets anything: it only turns the data into words, counts and tones.
// A part whose state is unknown is shown as unknown, never guessed. Type-only imports (erased), no network: covered by selfImproveEvidence.test.mjs.
import type { Tone } from "./selfImproveView";

export type PartStatus = "ok" | "none" | "stale" | "unreadable";
export interface BookWeak { group: string; key: string; n: number; mean_net_ret_pct: number | null; lower90_pct: number | null; verdict: string }
export interface BookEvidence {
  status: PartStatus | string; text?: string; age_days?: number; trades?: number; verdict?: string;
  mean_net_ret_pct?: number | null; lower90_pct?: number | null; dsr_prob?: number | null; charges_pct_of_gross?: number | null;
  weakest?: BookWeak[]; verdict_counts?: Record<string, number>;
}
export interface AttentionRow { strategy: string; status: string; drift: string | null; verdict: string | null; n: number }
export interface StrategyStatus { status: PartStatus | string; text?: string; age_days?: number; counts?: Record<string, number>; attention?: AttentionRow[] }
export interface EvidenceView { now?: number; book_evidence: BookEvidence; strategy_status: StrategyStatus }

export const REG_ORDER = ["testing", "proven", "watch", "demoted", "retired"] as const;

/** The verdict word -> tone. POSITIVE is good, NEGATIVE bad, NOT_PROVEN warn; NOT_ENOUGH_DATA (and anything unknown) is neutral: it is no information, not a warning. */
export function verdictTone(v: string | null | undefined): Tone {
  return v === "POSITIVE" ? "good" : v === "NEGATIVE" ? "bad" : v === "NOT_PROVEN" ? "warn" : undefined;
}
const VERDICT_WORD: Record<string, string> = { POSITIVE: "POSITIVE", NEGATIVE: "NEGATIVE", NOT_PROVEN: "NOT PROVEN", NOT_ENOUGH_DATA: "NOT ENOUGH DATA" };
export const verdictLabel = (v: string | null | undefined): string => VERDICT_WORD[String(v)] || "UNKNOWN";
/** The plain meaning under the verdict chip. It never says a person should switch anything. */
export function verdictMeaning(v: string | null | undefined): string {
  switch (v) {
    case "POSITIVE": return "The lower bound of the average net return is above zero and the Deflated Sharpe probability is high. Read it with the limits below; it is not a reason to raise size.";
    case "NEGATIVE": return "The upper bound of the average net return is below zero after costs. You decide whether to switch anything off; this page changes nothing.";
    case "NOT_PROVEN": return "Not clearly positive and not clearly negative yet.";
    case "NOT_ENOUGH_DATA": return "Fewer than 30 closed trades: this is no information, not a warning.";
    default: return "The verdict could not be read.";
  }
}

export function lifecycleTone(s: string | null | undefined): Tone {
  return s === "proven" ? "good" : s === "demoted" ? "bad" : s === "watch" ? "warn" : undefined;
}
/** Drift state -> tone. DECAY bad, WARNING warn, IMPROVING good; STABLE / NOT_ENOUGH_DATA / none neutral. */
export function driftTone(d: string | null | undefined): Tone {
  return d === "DECAY" ? "bad" : d === "WARNING" ? "warn" : d === "IMPROVING" ? "good" : undefined;
}
const DRIFT_WORD: Record<string, string> = { DECAY: "DECAY", WARNING: "WARNING", IMPROVING: "IMPROVING", STABLE: "STABLE", NOT_ENOUGH_DATA: "NOT ENOUGH DATA" };
export const driftLabel = (d: string | null | undefined): string => (d ? DRIFT_WORD[d] || "UNKNOWN" : "no drift reading");

/** A percentage the runner already rounded: "0.21%", "-0.05%", "-" when it is missing. */
export function pct(v: number | null | undefined, digits = 2): string {
  return typeof v === "number" && Number.isFinite(v) ? `${Number(v.toFixed(digits))}%` : "-";
}
/** Deflated Sharpe probability 0..1 -> "62 %"; "-" when missing. */
export const probText = (v: number | null | undefined): string => (typeof v === "number" && Number.isFinite(v) ? `${Math.round(v * 100)} %` : "-");

/** Why a part has nothing to show; "" when it is ok. Never guesses. */
export function partNotice(status: string | undefined, what: "book" | "registry"): string {
  if (status === "ok") return "";
  if (status === "stale") return what === "book" ? "The newest evidence report is too old to use (over 14 days). Run export_records, then evidence_report." : "The registry has not been updated for over 7 days. The daily strategy_registry job should refresh it.";
  if (status === "unreadable") return what === "book" ? "The evidence report could not be read." : "The strategy registry could not be read.";
  return what === "book" ? "No evidence report yet. Run export_records, then evidence_report, once from the runner page." : "No strategy registry yet. The daily strategy_registry job creates it after export_records and evidence_report have run.";
}

/** Counts per lifecycle status in a fixed order, zero ones left out; [] when there are no strategies. */
export function registryChips(counts: Record<string, number> | undefined): { label: string; tone: Tone }[] {
  const c = counts || {};
  return REG_ORDER.filter((k) => (c[k] || 0) > 0).map((k) => ({ label: `${c[k]} ${k}`, tone: lifecycleTone(k) }));
}
export const registryTotal = (counts: Record<string, number> | undefined): number => Object.values(counts || {}).reduce((a, b) => a + (Number.isFinite(b) ? b : 0), 0);

/** One attention row in words: "demoted · drift DECAY · last verdict NEGATIVE · 61 closed trades". */
export function attentionText(r: AttentionRow): string {
  return [r.status, r.drift ? `drift ${driftLabel(r.drift)}` : "no drift reading", r.verdict ? `last verdict ${verdictLabel(r.verdict)}` : "no verdict yet", `${r.n} closed trade${r.n === 1 ? "" : "s"}`].join(" · ");
}
/** The tone of an attention row: the worse of its lifecycle status and its drift. */
export function attentionTone(r: AttentionRow): Tone {
  const a = lifecycleTone(r.status), b = driftTone(r.drift);
  return a === "bad" || b === "bad" ? "bad" : a === "warn" || b === "warn" ? "warn" : undefined;
}

/** One weak group in words: "tier_name: tier_c · 33 trades · mean -0.4% · lower bound -0.9% · NOT PROVEN". */
export function weakText(w: BookWeak): string {
  return `${w.group}: ${w.key} · ${w.n} trade${w.n === 1 ? "" : "s"} · mean ${pct(w.mean_net_ret_pct)} · lower bound ${pct(w.lower90_pct)} · ${verdictLabel(w.verdict)}`;
}
/** "5 not proven · 7 not enough data" in a fixed order; "" when there are no counts. */
export function verdictCountsText(vc: Record<string, number> | undefined): string {
  return (["POSITIVE", "NEGATIVE", "NOT_PROVEN", "NOT_ENOUGH_DATA"] as const).filter((k) => vc && vc[k]).map((k) => `${vc![k]} ${verdictLabel(k).toLowerCase()}`).join(" · ");
}
/** The Needs-a-person line for the Health card: only a NEGATIVE book or a demoted / DECAY strategy; "" otherwise. Context, never an instruction. */
export function evidenceWarning(v: EvidenceView | null | undefined): string {
  if (!v) return "";
  const out: string[] = [];
  if (v.book_evidence?.status === "ok" && v.book_evidence.verdict === "NEGATIVE") out.push("the real trades read NEGATIVE after costs");
  const att = v.strategy_status?.status === "ok" ? v.strategy_status.attention || [] : [];
  const bad = att.filter((r) => r.status === "demoted" || r.drift === "DECAY").length;
  if (bad) out.push(`${bad} strateg${bad === 1 ? "y is" : "ies are"} demoted or decaying`);
  return out.length ? `Evidence: ${out.join("; ")}. See Evidence. This page changes no switch.` : "";
}
