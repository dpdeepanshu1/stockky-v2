// frontend/src/sessionView.ts  (E277)
// Pure helpers for the "Trading day" rail on the Position Stocks and Real Trade pages. No imports, no network, never throws.
// The slots are the plan's morning timeline (Stockky_Opening_Entry_and_Risk_Rework_Plan) as a DISPLAY: the services' own switches and
// clocks decide what actually runs. The rail only shows where IST time stands against that day. Covered by sessionView.test.mjs.

export type SessionService = "ps" | "rt";
export type SlotState = "done" | "now" | "next" | "later";

export interface SessionSlot { id: string; at: string; mins: number; label: string; note: string }
export interface SessionSlotView extends SessionSlot { state: SlotState }
export interface SessionView {
  service: SessionService;
  title: string;
  slots: SessionSlotView[];
  /** IST minutes since midnight, or null on a weekend (no session is drawn as running). */
  nowMins: number | null;
  /** 0 to 100: how far along the rail the marker sits (slots are evenly spaced; the marker moves between two slots by time). */
  pct: number;
  active: string | null;
  weekday: boolean;
  words: string;
}

const mm = (hhmm: string): number => { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; };
const slot = (at: string, id: string, label: string, note: string): SessionSlot => ({ id, at, mins: mm(at), label, note });

/** Position Stocks: intraday only, long and short. */
export const PS_SLOTS: SessionSlot[] = [
  slot("08:30", "plan", "Plan", "Overseas cues, India VIX and typed news events are read into the day's plan."),
  slot("09:05", "rebuild", "Rebuild", "The plan is rebuilt and refreshed every minute until 09:20."),
  slot("09:12", "final", "Final price", "The final pre-open price is read after NSE matching ends. Gap is judged on that, not the 09:10 print."),
  slot("09:15", "observe", "Observe", "Opening range, first volume and spread are measured. No orders."),
  slot("09:21", "s1s4", "S1 / S4", "First entries on a closed 1-minute bar after the range: positive event (S1) or negative event short (S4), quarter size."),
  slot("09:30", "s2", "S2", "Strength that holds (S2) is added, half size until its own results prove it."),
  slot("09:45", "s3", "S3", "Panic-dump or pullback setup (S3) and the normal shortlist. Full size only after promotion."),
  slot("14:00", "cutoff", "Last entry", "No new entries after the cut-off."),
  slot("14:55", "flat", "Flat", "Everything is closed, shorts are covered. Nothing is held overnight."),
];

/** Real Trade: long only, 2 to 5 session holds, never on the opening print. */
export const RT_SLOTS: SessionSlot[] = [
  slot("08:30", "plan", "Plan", "After-hours events and overseas cues decide which swing picks go first."),
  slot("09:05", "rebuild", "Rebuild", "The pre-open event list is rebuilt; a negative event turns a pick into a veto."),
  slot("09:15", "observe", "Observe", "Opening print is noise. Nothing is bought on it."),
  slot("09:21", "s1", "S1 / swing", "Event-backed and swing picks may enter on a closed bar that agrees with the signal."),
  slot("09:30", "s2", "S2", "Relative-strength setup (S2) from here, half size."),
  slot("09:45", "s3", "S3", "Pullback to support in liquid names (S3, long), probation size."),
  slot("14:00", "cutoff", "Last entry", "No new entries after the cut-off."),
  slot("15:00", "carry", "Carry check", "Each position is carried only if no results are due, no negative event, structure holds and heat fits."),
];

const TITLE: Record<SessionService, string> = { ps: "Trading day · intraday", rt: "Trading day · 2 to 5 day holds" };

/** Weekday and minutes since midnight in India, from any instant. Falls back to the device clock's own fields if Intl cannot do it. */
export function istParts(now: Date): { weekday: boolean; mins: number } {
  try {
    const parts = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(now);
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
    const wd = get("weekday");
    const h = Number(get("hour")) % 24, m = Number(get("minute"));
    return { weekday: wd !== "Sat" && wd !== "Sun", mins: (Number.isFinite(h) ? h : 0) * 60 + (Number.isFinite(m) ? m : 0) };
  } catch {
    const d = new Date(now.getTime() + (5 * 60 + 30) * 60000);
    const dow = d.getUTCDay();
    return { weekday: dow !== 0 && dow !== 6, mins: d.getUTCHours() * 60 + d.getUTCMinutes() };
  }
}

export function sessionView(service: SessionService, now: Date): SessionView {
  const list = service === "rt" ? RT_SLOTS : PS_SLOTS;
  const { weekday, mins } = istParts(now);
  // the slot that is "now" is the last one at or before the clock; the first one after it is "next"
  let at = -1;
  if (weekday) for (let i = 0; i < list.length; i++) if (list[i].mins <= mins) at = i;
  const slots: SessionSlotView[] = list.map((s, i) => ({
    ...s,
    state: !weekday ? (i === 0 ? "next" : "later") : i < at ? "done" : i === at ? "now" : i === at + 1 ? "next" : "later",
  }));
  let pct = 0;
  if (weekday && at >= 0) {
    if (at >= list.length - 1) pct = 100;
    else {
      const a = list[at].mins, b = list[at + 1].mins;
      pct = ((at + (mins - a) / Math.max(1, b - a)) / (list.length - 1)) * 100;
    }
  }
  pct = Math.max(0, Math.min(100, Math.round(pct * 10) / 10));
  const cur = at >= 0 ? list[at] : null;
  const nxt = !weekday ? list[0] : at + 1 < list.length ? list[at + 1] : null;
  const closed = weekday && mins >= 15 * 60 + 30;
  const words = closed
    ? "The market is closed for the day. The next plan starts at 08:30 IST."
    : !weekday
    ? `Weekend. The plan starts again at ${list[0].at} IST on the next weekday.`
    : cur === null
      ? `Before the day's plan. Next: ${list[0].label} at ${list[0].at} IST.`
      : at >= list.length - 1
        ? `${cur.label} (${cur.at}): ${cur.note}`
        : `${cur.label} (${cur.at}): ${cur.note}${nxt ? ` Next: ${nxt.label} at ${nxt.at}.` : ""}`;
  if (closed) for (const s of slots) s.state = "done";
  return { service, title: TITLE[service], slots, nowMins: weekday ? mins : null, pct: closed ? 100 : pct, active: cur && !closed ? cur.id : null, weekday, words };
}


/* -- the pre-market plan card (GET /premarket/plan on Position Stocks) ----------------------------------------------------------------------- */

export interface PremarketPlanDoc {
  ok: boolean; enabled?: boolean | null; error?: string;
  day?: string | null; built_at?: string | null; builds?: number | null;
  risk_off?: boolean | null; vix?: string | null; events?: { positive?: number; negative?: number } | null; notes?: unknown;
}
export type PlanTone = "good" | "warn" | "bad" | undefined;
export interface PremarketPlanView {
  kind: "unavailable" | "off" | "waiting" | "stale" | "ready";
  tone: PlanTone; headline: string;
  tiles: { label: string; value: string; tone: PlanTone; hint: string }[];
  notes: string[];
}

const isNum = (v: unknown): v is number => typeof v === "number" && isFinite(v);

/** Today's date in India as YYYY-MM-DD (the plan's own `day` is an IST date string). */
export function istDay(now: Date): string {
  try { return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit" }).format(now); }
  catch { return new Date(now.getTime() + 19800000).toISOString().slice(0, 10); }
}

export function premarketPlanView(doc: PremarketPlanDoc | null | undefined, now: Date): PremarketPlanView {
  const base: PremarketPlanView = { kind: "unavailable", tone: undefined, headline: "The pre-market plan is not available.", tiles: [], notes: [] };
  if (!doc) return base;
  if (doc.ok === false) return { ...base, headline: `The pre-market plan could not be read${doc.error ? `: ${String(doc.error).slice(0, 100)}` : "."}` };
  if (doc.enabled === false) return { ...base, kind: "off", headline: "Off. PREMARKET_PLAN_ENABLED is false, so no cues, VIX or event list are prepared before the open." };
  const builds = isNum(doc.builds) ? doc.builds : 0;
  if (!doc.day || builds === 0) return { ...base, kind: "waiting", headline: "No plan built yet today. The first build runs from 08:30 IST on a trading day." };
  if (doc.day !== istDay(now)) return { ...base, kind: "stale", tone: "warn", headline: `The last plan is from ${doc.day}. Today's plan has not been built yet.` };
  const pos = isNum(doc.events?.positive) ? doc.events!.positive! : 0, neg = isNum(doc.events?.negative) ? doc.events!.negative! : 0;
  const riskOff = doc.risk_off === true;
  const built = typeof doc.built_at === "string" && doc.built_at.length >= 16 ? doc.built_at.slice(11, 16) : "-";
  const notes = Array.isArray(doc.notes) ? (doc.notes as unknown[]).filter((n): n is string => typeof n === "string" && n.length > 0).slice(0, 4) : [];
  return {
    kind: "ready", tone: riskOff ? "warn" : "good",
    headline: riskOff ? "Overseas markets lean risk-off. The plan sizes entries down; it never blocks one by itself." : "Plan built for today. Overseas cues do not flag risk-off.",
    tiles: [
      { label: "Overseas", value: doc.risk_off == null ? "unknown" : riskOff ? "risk-off" : "calm", tone: riskOff ? "warn" : doc.risk_off == null ? undefined : "good", hint: "Average overseas move and US futures against the carry rule's limits." },
      { label: "India VIX", value: doc.vix ? String(doc.vix) : "unknown", tone: undefined, hint: "Today's India VIX against its own recent range." },
      { label: "Good news", value: String(pos), tone: undefined, hint: "Watchlist names with a typed positive event (S1 candidates)." },
      { label: "Bad news", value: String(neg), tone: neg > 0 ? "warn" : undefined, hint: "Watchlist names with a typed negative event (S4 shorts and entry vetoes)." },
      { label: "Last build", value: built === "-" ? "-" : `${built} IST`, tone: undefined, hint: `${builds} build${builds === 1 ? "" : "s"} today; refreshed every minute until 09:20.` },
    ],
    notes,
  };
}
