// E536 (redesign round 4): pure helpers for the top of the stock decision card. No import, no call, no state: the numbers come from the
// decision the page already holds, so this file cannot change what a decision says, only how the plan is laid out.

export interface PlanInput {
  entryLow: number | null | undefined;
  entryHigh: number | null | undefined;
  stop: number | null | undefined;
  target: number | null | undefined;
  price: number | null | undefined;
}
export interface PlanView {
  entry: number;
  stop: number;
  target: number;
  riskPct: number;
  rewardPct: number;
  rr: number;
  rrText: string;
}

const fin = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);

/** The entry the plan is measured from: the middle of the entry range, else the live price. */
export function planEntry(p: PlanInput): number | null {
  if (fin(p.entryLow) && fin(p.entryHigh) && p.entryLow > 0 && p.entryHigh >= p.entryLow) return (p.entryLow + p.entryHigh) / 2;
  if (fin(p.price) && p.price > 0) return p.price;
  return null;
}

/**
 * Entry, stop, target and reward-to-risk for a LONG idea (BUY NOW / PREPARE TO BUY). Returns null unless stop < entry < target, because a
 * ratio from numbers in the wrong order would look like a plan and is not one. Risk and reward are percentages of the entry.
 */
export function tradePlan(p: PlanInput): PlanView | null {
  const entry = planEntry(p);
  if (entry === null || !fin(p.stop) || !fin(p.target)) return null;
  if (!(p.stop > 0 && p.stop < entry && entry < p.target)) return null;
  const risk = entry - p.stop;
  const reward = p.target - entry;
  const rr = reward / risk;
  return { entry, stop: p.stop, target: p.target, riskPct: (risk / entry) * 100, rewardPct: (reward / entry) * 100, rr, rrText: `1 : ${rr.toFixed(1)}` };
}

export type SignalTone = "buy" | "prepare" | "hold" | "avoid" | "sell";
export interface SignalWord { word: string; glyph: string; tone: SignalTone }

/** The four words the plan names (BUY, PREPARE, HOLD, AVOID) plus WAIT and SELL; the glyph is always shown, so the signal is never colour-only. */
export function signalWord(decision: string): SignalWord {
  switch (decision) {
    case "BUY NOW": return { word: "BUY", glyph: "▲", tone: "buy" };
    case "PREPARE TO BUY": return { word: "PREPARE", glyph: "◆", tone: "prepare" };
    case "HOLD": return { word: "HOLD", glyph: "■", tone: "hold" };
    case "WAIT": return { word: "WAIT", glyph: "●", tone: "hold" };
    case "SELL": return { word: "SELL", glyph: "▼", tone: "sell" };
    default: return { word: "AVOID", glyph: "○", tone: "avoid" };
  }
}

export const isLongIdea = (decision: string): boolean => decision === "BUY NOW" || decision === "PREPARE TO BUY";

/** "₹1,234.5" in Indian digit grouping; "—" for a missing or non-finite number. */
export function rupees(n: number | null | undefined, digits = 2): string {
  if (!fin(n)) return "—";
  return `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: digits })}`;
}

/** "+3.2%" / "-1.0%" / "0.0%": the sign is in the text. */
export function signedPct(n: number | null | undefined, digits = 1): string {
  if (!fin(n)) return "—";
  const s = n.toFixed(digits);
  return n > 0 ? `+${s}%` : `${s}%`;
}
