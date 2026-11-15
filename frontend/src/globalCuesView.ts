// round 553 (E578, Tredye plan P4): the global cues strip - S&P 500, Nasdaq, Brent crude, dollar index, USD/INR, gold. DISPLAY ONLY: nothing here feeds a trading
// decision or a Market Weather dial. Pure words, no React. Acceptance rule (plan section 4): no number without a time (every tile says which bar it is from),
// a missing number is a dash and never 0, and an old number is never shown as if it were current (older than STALE_AFTER_DAYS it is a dash marked stale).
export interface CueRow { price: number; previous_close?: number | null; day_change_pct: number; bar_date: string }
export interface GlobalCuesBody {
  rows?: Record<string, CueRow>;
  missing?: string[];
  fetched_at?: string;
  today_ist?: string;
  enabled?: boolean;
  error?: string;
}
export type CueState = "ok" | "stale" | "missing";
export interface CueTile {
  key: string; label: string; value: string; delta?: string; tone: "up" | "down" | "flat"; sub: string; state: CueState;
}

// `neutral`: a rise is not good or bad news on its own (crude, dollar index, USD/INR), so no up / down colour: the signed number carries the direction.
export const CUE_LIST: ReadonlyArray<{ sym: string; label: string; neutral: boolean }> = [
  { sym: "^GSPC", label: "S&P 500", neutral: false },
  { sym: "^IXIC", label: "NASDAQ", neutral: false },
  { sym: "BZ=F", label: "BRENT CRUDE", neutral: true },
  { sym: "DX-Y.NYB", label: "DOLLAR INDEX", neutral: true },
  { sym: "INR=X", label: "USD / INR", neutral: true },
  { sym: "GC=F", label: "GOLD", neutral: false },
];

export const DASH = "—";
export const STALE_AFTER_DAYS = 5;
const MINUS = "−";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const fin = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);

/** "YYYY-MM-DD" to "13 Nov", or "" when it is not a real date. */
export function dayText(iso: unknown): string {
  const m = typeof iso === "string" ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso) : null;
  if (!m) return "";
  const mo = Number(m[2]), d = Number(m[3]);
  return mo >= 1 && mo <= 12 && d >= 1 && d <= 31 ? `${d} ${MONTHS[mo - 1]}` : "";
}

/** Whole days from a to b (both "YYYY-MM-DD"); null when either is not a date. Never negative (a bar dated after "today" is today's). */
export function ageDays(barDate: unknown, today: unknown): number | null {
  if (!dayText(barDate) || !dayText(today)) return null;
  const ms = Date.parse(`${today}T00:00:00Z`) - Date.parse(`${barDate}T00:00:00Z`);
  return Number.isFinite(ms) ? Math.max(0, Math.round(ms / 86400000)) : null;
}

export function priceText(n: unknown): string {
  return fin(n) && n > 0 ? n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : DASH;
}

/** "+0.42%" / "−0.42%" (real minus sign, so it can never be read as the dash for missing) / "0.00%". */
export function pctText(n: unknown): string {
  if (!fin(n)) return DASH;
  const t = Math.abs(n).toFixed(2);
  return n > 0 && t !== "0.00" ? `+${t}%` : n < 0 && t !== "0.00" ? `${MINUS}${t}%` : `${t}%`;
}

/** The IST clock of an ISO stamp like "2026-11-15T14:32:10+05:30" as "14:32", or "". */
export function clockText(iso: unknown): string {
  const m = typeof iso === "string" ? /T(\d{2}:\d{2})/.exec(iso) : null;
  return m ? m[1] : "";
}

export function cueTile(def: { sym: string; label: string; neutral: boolean }, row: CueRow | undefined, today: string): CueTile {
  const gone = (sub: string, state: CueState): CueTile => ({ key: def.sym, label: def.label, value: DASH, tone: "flat", sub, state });
  if (!row || !fin(row.price) || row.price <= 0 || !fin(row.day_change_pct)) return gone("no quote", "missing");
  const bar = dayText(row.bar_date);
  const age = ageDays(row.bar_date, today);
  if (!bar || age === null) return gone("no date, not shown", "missing");        // a number with no date is not shown
  if (age >= STALE_AFTER_DAYS) return gone(`stale, last ${bar}`, "stale");
  const pct = row.day_change_pct;
  const tone = def.neutral || pct === 0 ? "flat" : pct > 0 ? "up" : "down";
  return { key: def.sym, label: def.label, value: priceText(row.price), delta: pctText(pct), tone, sub: age === 0 ? `${bar}, today` : `last close ${bar}`, state: "ok" };
}

/** The six tiles in fixed order. `today` is the gateway's IST date; without it every tile is a dash (we cannot say how old anything is). */
export function globalCueTiles(body: GlobalCuesBody | null | undefined, todayFallback?: string): CueTile[] {
  const today = body && dayText(body.today_ist) ? (body.today_ist as string) : todayFallback ?? "";
  const rows = body && body.rows && typeof body.rows === "object" ? body.rows : {};
  return CUE_LIST.map((d) => cueTile(d, rows[d.sym], today));
}

/** The line under the title: when the gateway read it, or why nothing is shown. */
export function stripSub(body: GlobalCuesBody | null | undefined, failed: boolean): string {
  if (failed && !body) return "Could not load. Showing dashes.";
  if (body && body.enabled === false) return "Switched off (GLOBAL_STRIP_ENABLED=0).";
  const t = body ? clockText(body.fetched_at) : "";
  const base = t ? `Read ${t} IST from Yahoo daily bars` : "Yahoo daily bars";
  return failed ? `${base}. Refresh failed, showing the last read.` : body && body.error ? `${base}. ${body.error}.` : base;
}
