// frontend/src/weatherView.ts  (Market Weather, Part 1 step 5)
// Pure helpers for the Market Weather card (no imports, no network): shapes of GET /weather/status, and the three states the card can be in.
// Covered by weather.test.mjs. The runner is in SHADOW: nothing here sends a dial, so the card only shows what the profile WOULD tighten.

export type WeatherState = "calm" | "normal" | "choppy" | "stressed" | "panic";
export type WeatherStage = "morning" | "refine" | "lock" | "tick";
export const WEATHER_STAGES: WeatherStage[] = ["morning", "refine", "lock", "tick"];

export interface WeatherCurrent {
  state: WeatherState; score: number | null; data_ok: boolean | null; reasons: string[]; floor_reasons: string[];
  gap_pct: number | null; stage: WeatherStage; at: string;
}
export interface WeatherLogRow {
  at: string; day: string; stage: WeatherStage; ok: boolean; state?: WeatherState; previous_state?: WeatherState | null; changed?: boolean;
  score?: number | null; note?: string; error?: string; gap_pct?: number | null; live_would?: Record<string, number | string | null>;
}
export interface WeatherStatus {
  mode: string; enabled: boolean; stamp: boolean; notify: boolean; apply?: boolean; last_apply?: WeatherApplyResult | null; gate?: WeatherGate | null; day: string | null; current: WeatherCurrent | null; version: string;
  last_fired: Record<string, string>; profile: { state: WeatherState; dials: Record<string, number | string | null> }[]; recent: WeatherLogRow[];
}
export interface WeatherGate { mode: string | null; open: boolean; why: string }
export interface WeatherApplyResult { at: string; stage: string; applied: boolean; why: string; state?: string; services?: Record<string, { ok: boolean; why?: string; entries?: Record<string, string> }> }
export interface WeatherSettings { enabled?: boolean; stamp?: boolean; notify?: boolean; apply?: boolean }

export const STATE_TEXT: Record<WeatherState, string> = { calm: "Calm uptrend", normal: "Normal", choppy: "Choppy", stressed: "Stressed", panic: "Panic" };
export const STATE_TONE: Record<WeatherState, "good" | "warn" | "bad" | undefined> = { calm: "good", normal: undefined, choppy: "warn", stressed: "bad", panic: "bad" };
export const STATE_PLAIN: Record<WeatherState, string> = {
  calm: "Sunny and steady. Nothing would change.", normal: "An ordinary day. Nothing would change.", choppy: "Gusty and going nowhere. Smaller and later.",
  stressed: "A storm is here. Much smaller, fewer and earlier cut-off.", panic: "Storm after a crash. Smallest, fewest, latest start.",
};

export const DIAL_LABEL: Record<string, string> = {
  MARKET_STATE_RISK_MULT: "Risk per trade (× saved)",
  MARKET_STATE_CONVICTION_ADD: "Minimum conviction (+ points)",
  OPENING_ENTRY_NOT_BEFORE_IST: "Earliest entry (real-trade)",
  RISK_MAX_ENTRIES_PER_DAY: "New entries per day (real-trade)",
  ENTRY_LAST_TIME_IST: "Last new entry (real-trade)",
  MAX_CONCURRENT_SCALP_POSITIONS: "Open scalp positions (position-stocks)",
  SCALP_ENTRY_CUTOFF_IST: "Last scalp entry (position-stocks)",
  MARKET_STATE_SHORT_MULT: "Short size (× saved; 0 = no shorts)",
};
/** Display order of the dials (the profile table's key order is not guaranteed). */
export const DIAL_ORDER = Object.keys(DIAL_LABEL);

export type WeatherKind = "off" | "waiting" | "active";
export interface WeatherView {
  kind: WeatherKind;
  headline: string;            // one line for the card
  state: WeatherState | null;  // null unless active
  stale: boolean;              // a state exists but belongs to an earlier day
  failed: boolean;             // the latest stage of today did not get its data
  dials: { key: string; label: string; value: string }[];   // what the profile WOULD set for the current state (empty = nothing would change)
  today: WeatherLogRow[];      // today's log rows, oldest first
}

const fmtVal = (v: number | string | null | undefined): string => (v == null ? "-" : typeof v === "number" ? String(v) : v);

/** today = "YYYY-MM-DD" in IST (passed in so tests need no clock). */
export function weatherView(st: WeatherStatus | null | undefined, today: string): WeatherView {
  const empty: WeatherView = { kind: "off", headline: "", state: null, stale: false, failed: false, dials: [], today: [] };
  if (!st) return { ...empty, headline: "Market Weather is not available." };
  const rows = (st.recent || []).filter((r) => r.day === today).sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
  const failed = rows.length > 0 && !rows[rows.length - 1].ok;
  if (!st.enabled) return { ...empty, kind: "off", headline: "Off. Nothing is computed, logged or sent until you switch it on.", today: rows, failed };
  const cur = st.current;
  if (!cur || !cur.state) {
    return { ...empty, kind: "waiting", headline: failed ? "Switched on, but the last stage could not get its data." : "Switched on. Waiting for today's 08:00 morning stage.", today: rows, failed };
  }
  const stale = !!st.day && st.day !== today;
  const would = (st.profile || []).find((p) => p.state === cur.state)?.dials || {};
  const dials = DIAL_ORDER.filter((k) => would[k] != null).map((k) => ({ key: k, label: DIAL_LABEL[k], value: fmtVal(would[k]) }));
  return {
    kind: "active", state: cur.state, stale, failed, dials, today: rows,
    headline: stale ? `Last state was ${STATE_TEXT[cur.state]} on ${st.day}. Today has not started yet.` : `${STATE_TEXT[cur.state]} (set at the ${cur.stage} stage)`,
  };
}

/** "09:14" from an ISO timestamp that already carries the IST offset (the runner writes +05:30). */
export const hm = (iso: string | undefined): string => (iso && /T(\d\d:\d\d)/.exec(iso)?.[1]) || "-";

/** One line per service for the last apply attempt: what was sent, or why nothing was. */
export function applyLines(a: WeatherApplyResult | null | undefined): string[] {
  if (!a) return [];
  const out = [`${hm(a.at)} ${a.stage}: ${a.applied ? "dials applied" : a.why || "not applied"}`];
  for (const [svc, r] of Object.entries(a.services || {})) {
    out.push(r.ok ? `${svc}: ${Object.entries(r.entries || {}).map(([k, v]) => `${DIAL_LABEL[k] || k} ${v}`).join(", ") || "no dial tighter than saved"}` : `${svc}: ${r.why || "not sent"}`);
  }
  return out;
}

/** The evidence gate in one line: its mode and the verdict that opened or closed it. */
export function gateLine(g: WeatherGate | null | undefined): string {
  if (!g) return "";
  return `Gate (${g.mode || "unknown"}): ${g.open ? "open" : "closed"} - ${g.why}`;
}


/** The last wiring_check run in one line: when, did it pass, and the first line that explains a failure. `runs` are the runner's run records, newest first. */
export function lastWiringLine(runs: { target?: string; status?: string; finished_at?: string | null; headline?: string[]; exit_summary?: string }[] | null | undefined): string {
  const r = (runs || []).find((x) => x && x.target === "wiring_check" && x.status !== "queued" && x.status !== "running");
  if (!r) return "Wiring check: never run (Tests & training, or the Monday open pack).";
  const when = r.finished_at ? r.finished_at.slice(0, 16).replace("T", " ") : "?";
  const line = (r.headline || []).find((h) => /WIRING:/.test(h)) || r.exit_summary || "";
  const m = /WIRING:\s*(PASS|FAIL)(.*)$/.exec(line);
  if (m) return `Wiring check ${when} IST: ${m[1]}${m[1] === "FAIL" ? m[2] : ""}`;
  return `Wiring check ${when} IST: ${r.status}${line ? ` - ${line}` : ""}`;
}
