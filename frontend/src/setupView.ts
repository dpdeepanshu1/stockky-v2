// frontend/src/setupView.ts  (E272)
// Pure helpers for the "Entry reason" column (positions and history) and the "Setup scorecard" card of the Real Money Calibration page.
// No runtime imports, no network, never throws. Shapes: `setup` on GET /positions and GET /trades/history rows, and GET /setup/stats (position-stocks).
// Covered by setupView.test.mjs.

export interface SetupStamp {
  id: string; trigger?: string; confirmations?: string[]; penalties?: Record<string, number>; size_mult?: number | null; slot?: string; side?: string;
}
export interface SetupCell {
  n: number; wins: number; win_rate: number | null; r_n: number; avg_r: number | null; lower_bound_r: number | null;
  verdict: "NOT_ENOUGH_DATA" | "NOT_PROVEN" | "POSITIVE" | string;
}
export interface SetupStatsDoc {
  ok: boolean; rows_read?: number; min_trades?: number; cells?: Record<string, SetupCell>; error?: string;
  /** E273 round 35: the slot ladder. `promoted` cells trade at full size, `demoted` cells at a reduced size; both are "<setup id>|<slot>" keys. */
  ladder_enabled?: boolean; promoted?: string[]; demoted?: string[];
}

const isNum = (v: unknown): v is number => typeof v === "number" && isFinite(v);

export const SLOT_ORDER = ["ALL", "0920", "0930", "1000", "later"] as const;
const SLOT_LABEL: Record<string, string> = { ALL: "all day", "0920": "09:20", "0930": "09:30", "1000": "10:00", later: "later" };
export const LEGACY_ID = "LEGACY_SHORTLIST";

export function slotLabel(slot: unknown): string {
  return typeof slot === "string" && SLOT_LABEL[slot] ? SLOT_LABEL[slot] : "later";
}

/** Plain name of a setup id: LEGACY_SHORTLIST reads "Shortlist (old rule)", S2_PULLBACK reads "S2 PULLBACK". */
export function setupName(id: unknown): string {
  if (typeof id !== "string" || !id.trim()) return "-";
  if (id === LEGACY_ID) return "Shortlist (old rule)";
  return id.replace(/_/g, " ");
}

export interface EntryReason { known: boolean; label: string; slot: string; title: string }

/** What the Entry reason column shows for one trade. A row without a stamp (opened before E272) reads "-" and says why in its tooltip. */
export function entryReason(stamp: SetupStamp | null | undefined): EntryReason {
  if (!stamp || typeof stamp !== "object" || typeof stamp.id !== "string" || !stamp.id) {
    return { known: false, label: "-", slot: "", title: "No setup stamp: this trade was opened before entries were stamped." };
  }
  const slot0 = slotLabel(stamp.slot);
  const slot = slot0 === "later" ? "after 10:30" : slot0;      // round 218: "later" alone read like part of the label
  const conf = Array.isArray(stamp.confirmations) ? stamp.confirmations.filter((c) => typeof c === "string" && c) : [];
  const pen = stamp.penalties && typeof stamp.penalties === "object" ? Object.entries(stamp.penalties).filter(([, v]) => isNum(v)) : [];
  const parts: string[] = [];
  const legacy = stamp.id === LEGACY_ID;
  parts.push(legacy ? "No new setup (S1 / S2 / S3) matched, so the old shortlist rule took this trade (the baseline the new setups are compared with)." : `Setup ${setupName(stamp.id)}.`);
  if (stamp.trigger) parts.push(`Trigger: ${stamp.trigger}.`);
  if (conf.length) parts.push(`Confirmations: ${conf.join(", ")}.`);
  if (pen.length) parts.push(`Penalties: ${pen.map(([k, v]) => `${k} ${v}`).join(", ")}.`);
  if (isNum(stamp.size_mult)) parts.push(`Size x${stamp.size_mult}.`);
  parts.push(`Entry slot ${slot}${stamp.side ? `, ${String(stamp.side).toLowerCase()}` : ""}.`);
  // round 218: an old-rule trade reads "<its shortlist label> (no setup)", not "Shortlist (old rule)"; the scorecard keeps the baseline name via setupName
  const trig = typeof stamp.trigger === "string" ? stamp.trigger.trim() : "";
  const label = legacy ? (trig ? `${trig} (no setup)` : "No setup matched") : setupName(stamp.id);
  return { known: true, label, slot, title: parts.join(" ") };
}

export type Tone = "good" | "warn" | "bad" | undefined;
export interface ScorecardRow {
  key: string; setup: string; slot: string; slotLabel: string; isAll: boolean;
  n: number; wins: number; winPct: number | null; avgR: number | null; lowerR: number | null;
  verdict: string; verdictText: string; tone: Tone; progressPct: number;
}
export interface ScorecardView {
  kind: "unavailable" | "empty" | "ok"; tone: Tone; headline: string; minTrades: number; rowsRead: number; rows: ScorecardRow[]; warnings: string[];
}

const VERDICT_TEXT: Record<string, string> = {
  NOT_ENOUGH_DATA: "not enough trades yet", NOT_PROVEN: "not proven", POSITIVE: "positive (lower bound above 0)",
};
const r2 = (v: number) => Math.round(v * 100) / 100;

export function scorecardView(doc: SetupStatsDoc | null | undefined): ScorecardView {
  const base: ScorecardView = { kind: "unavailable", tone: "warn", headline: "Setup numbers are not available.", minTrades: 30, rowsRead: 0, rows: [], warnings: [] };
  if (!doc || typeof doc !== "object") return base;
  if (doc.ok === false) return { ...base, headline: `Setup numbers are not available${doc.error ? `: ${doc.error}` : "."}` };
  const minTrades = isNum(doc.min_trades) && doc.min_trades > 0 ? doc.min_trades : 30;
  const rowsRead = isNum(doc.rows_read) ? doc.rows_read : 0;
  const cells = doc.cells && typeof doc.cells === "object" ? doc.cells : {};
  const rows: ScorecardRow[] = [];
  for (const [key, c] of Object.entries(cells)) {
    if (!c || typeof c !== "object") continue;
    const cut = key.lastIndexOf("|");
    const setup = cut >= 0 ? key.slice(0, cut) : key;
    const slot = cut >= 0 ? key.slice(cut + 1) : "ALL";
    const n = isNum(c.n) ? c.n : 0;
    const verdict = typeof c.verdict === "string" ? c.verdict : "NOT_ENOUGH_DATA";
    rows.push({
      key, setup, slot, slotLabel: slotLabel(slot), isAll: slot === "ALL", n, wins: isNum(c.wins) ? c.wins : 0,
      winPct: isNum(c.win_rate) ? r2(c.win_rate * 100) : null, avgR: isNum(c.avg_r) ? r2(c.avg_r) : null,
      lowerR: isNum(c.lower_bound_r) ? r2(c.lower_bound_r) : null, verdict, verdictText: VERDICT_TEXT[verdict] ?? verdict.toLowerCase().replace(/_/g, " "),
      tone: verdict === "POSITIVE" ? "good" : verdict === "NOT_PROVEN" ? "warn" : undefined, progressPct: Math.min(100, Math.round((n / minTrades) * 100)),
    });
  }
  rows.sort((a, b) => {
    if (a.setup !== b.setup) return a.setup === LEGACY_ID ? -1 : b.setup === LEGACY_ID ? 1 : a.setup.localeCompare(b.setup);
    return SLOT_ORDER.indexOf(a.slot as any) - SLOT_ORDER.indexOf(b.slot as any);
  });
  if (rows.length === 0) {
    return { kind: "empty", tone: "warn", headline: rowsRead > 0 ? `${rowsRead} settled trades read, none carries a setup stamp yet.` : "No settled trades are held yet, so there is nothing to score.",
      minTrades, rowsRead, rows, warnings: [] };
  }
  const all = rows.filter((r) => r.isAll);
  const positive = all.filter((r) => r.verdict === "POSITIVE").length;
  const enough = all.filter((r) => r.verdict !== "NOT_ENOUGH_DATA").length;
  const warnings: string[] = [];
  warnings.push("Position rows are deleted after a few days, so this window is short; a cell reaches the minimum only when the numbers are kept elsewhere too.");
  if (enough === 0) warnings.push(`No setup has ${minTrades} trades yet, so no setup can be judged and none can be promoted.`);
  const headline = enough === 0
    ? `${all.length} setup${all.length === 1 ? "" : "s"} seen, none with ${minTrades} trades yet.`
    : `${positive} of ${enough} judged setup${enough === 1 ? "" : "s"} positive (90% lower bound above 0).`;
  return { kind: "ok", tone: positive > 0 ? "good" : "warn", headline, minTrades, rowsRead, rows, warnings };
}


/* -- E277: the four entry setups as a ladder (display of the plan; sizes are the STARTING sizes, each cell learns its own) ---------------------- */

export type LadderStage = "starting" | "promoted" | "demoted" | "building";
export interface SetupInfo { id: string; short: string; name: string; side: string; from: string; start: string; why: string }

export const SETUP_CATALOG: SetupInfo[] = [
  { id: "S1_CATALYST_UP", short: "S1", name: "Good news", side: "long", from: "09:21", start: "quarter size", why: "A typed positive event (results, order, deal) and a closed bar above the opening range, with 2 of 3 confirmations." },
  { id: "S4_CATALYST_DOWN", short: "S4", name: "Bad news", side: "short (Position Stocks only)", from: "09:21", start: "quarter size", why: "A typed negative event and a closed bar below the opening range. In Real Trade this is an exit trigger on a held stock, never a short." },
  { id: "S2_RS_BREAKOUT", short: "S2", name: "Strength that holds", side: "long and short", from: "09:30", start: "half size", why: "Top of the day's relative strength, daily structure fine and a closed bar through the range or a pullback that holds." },
  { id: "S3_EXHAUSTION", short: "S3", name: "Panic or pullback", side: "long and short", from: "09:45", start: "half size (probation in Real Trade)", why: "A move beyond twice the stock's own volatility that fails, in liquid names, with tape exhaustion or a support wall." },
];

export interface SetupLadderCard extends SetupInfo { stage: LadderStage; stageText: string; closed: number; cellsPromoted: number; cellsDemoted: number }
export interface SetupLadderView { ladderOn: boolean | null; cards: SetupLadderCard[]; legacyClosed: number; words: string }

/** One card per setup: how many settled trades carry it, and whether any of its slot cells is promoted (full size) or demoted (reduced size) right now. */
export function setupLadderView(doc: SetupStatsDoc | null | undefined): SetupLadderView {
  const cells = doc && doc.cells && typeof doc.cells === "object" ? doc.cells : {};
  const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
  const promoted = strs(doc?.promoted), demoted = strs(doc?.demoted);
  const min = doc && isNum(doc.min_trades) && doc.min_trades > 0 ? doc.min_trades : 30;
  const closedOf = (id: string): number => {
    const c = (cells as Record<string, SetupCell | undefined>)[`${id}|ALL`];
    return c && isNum(c.n) ? c.n : 0;
  };
  const cards = SETUP_CATALOG.map((info): SetupLadderCard => {
    const closed = closedOf(info.id);
    const p = promoted.filter((k) => k.startsWith(`${info.id}|`)).length;
    const d = demoted.filter((k) => k.startsWith(`${info.id}|`)).length;
    const stage: LadderStage = p > 0 ? "promoted" : d > 0 ? "demoted" : closed >= min ? "starting" : "building";
    const stageText = stage === "promoted" ? `${p} slot${p === 1 ? "" : "s"} earned full size`
      : stage === "demoted" ? `${d} slot${d === 1 ? "" : "s"} cut to half of the starting size`
      : stage === "starting" ? "Enough trades, no slot has proven itself yet"
      : closed > 0 ? `${closed} of ${min} trades so far` : `No trades yet (${min} needed to judge a slot)`;
    return { ...info, stage, stageText, closed, cellsPromoted: p, cellsDemoted: d };
  });
  const legacyClosed = closedOf(LEGACY_ID);
  const ladderOn = doc && typeof doc.ladder_enabled === "boolean" ? doc.ladder_enabled : null;
  const words = !doc ? "The setup ladder has no numbers yet."
    : ladderOn === false ? "Slot ladder is OFF: every setup trades at its starting size whatever its results."
    : "A setup starts small. A slot earns full size when its 90% lower bound of average R is above zero after 30 settled trades, and is cut when its average R is below zero.";
  return { ladderOn, cards, legacyClosed, words };
}
