// round 554 (E579, Tredye plan P5): the end-of-day F&O pack - OI Flow and futures basis from the exchange's F&O bhavcopy. DISPLAY ONLY: nothing here feeds a trading
// decision or a switch. Pure words, no React. Acceptance rule (plan section 4): no number without a date (the pack says which close it is), a missing number is a dash
// and never 0, and an old pack is never shown as if it were the latest (it says "latest file not out yet, showing the close of <date>").
import { DASH, dayText, ageDays, pctText } from "./globalCuesView.ts";

export type Buildup = "long_buildup" | "short_covering" | "short_buildup" | "long_unwinding";
export interface FoRow {
  symbol: string;
  near_expiry?: string | null;
  days_to_expiry?: number | null;
  fut_close?: number | null;
  price_chg_pct?: number | null;
  oi?: number | null;
  oi_chg?: number | null;
  oi_chg_pct?: number | null;
  buildup?: Buildup | null;
  spot?: number | null;
  basis_pct?: number | null;
  annualised_pct?: number | null;
  roll_pct?: number | null;
  rolling?: boolean | null;
}
export interface FoEodBody {
  rows?: FoRow[];
  counts?: Partial<Record<Buildup | "stocks" | "no_basis", number>>;
  trade_date?: string | null;
  expected_date?: string | null;
  stale?: boolean;
  fetched_at?: string;
  today_ist?: string;
  now_ist?: string;
  enabled?: boolean;
  error?: string;
}

export const BUILDUP_LIST: ReadonlyArray<{ id: Buildup; label: string; read: string; tone: "up" | "down" | "flat" }> = [
  { id: "long_buildup", label: "Long build-up", read: "price up, open interest up: new buying", tone: "up" },
  { id: "short_covering", label: "Short covering", read: "price up, open interest down: sellers closing out", tone: "up" },
  { id: "short_buildup", label: "Short build-up", read: "price down, open interest up: new selling", tone: "down" },
  { id: "long_unwinding", label: "Long unwinding", read: "price down, open interest down: buyers leaving", tone: "down" },
];

const fin = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);
const MINUS = "−";

/** A whole number with Indian grouping, or a dash. */
export function intText(n: unknown): string {
  return fin(n) ? Math.round(n).toLocaleString("en-IN") : DASH;
}

/** Signed whole number with a real minus sign; 0 is "0" (a real zero), missing is a dash. */
export function signedIntText(n: unknown): string {
  if (!fin(n)) return DASH;
  const r = Math.round(n);
  return r > 0 ? `+${r.toLocaleString("en-IN")}` : r < 0 ? `${MINUS}${Math.abs(r).toLocaleString("en-IN")}` : "0";
}

/** Signed percent with one decimal for the annualised carry ("+11.2%"), same minus rule as pctText. */
export function pct1Text(n: unknown): string {
  if (!fin(n)) return DASH;
  const t = Math.abs(n).toFixed(1);
  return n > 0 && t !== "0.0" ? `+${t}%` : n < 0 && t !== "0.0" ? `${MINUS}${t}%` : `${t}%`;
}

/** Basis % with three decimals is too long to read; two decimals, same sign rule. */
export const basisText = pctText;

export function plainPct(n: unknown): string {
  return fin(n) ? `${n.toFixed(1)}%` : DASH;
}

export function priceText(n: unknown): string {
  return fin(n) && n > 0 ? n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : DASH;
}

export function buildupLabel(b: unknown): string {
  return BUILDUP_LIST.find((x) => x.id === b)?.label ?? "No clear read";
}

/** "13 Nov", or a dash when the expiry is missing. */
export function expiryText(iso: unknown, days: unknown): string {
  const d = dayText(iso);
  if (!d) return DASH;
  return fin(days) ? (days === 0 ? `${d} (today)` : `${d} (${days} d)`) : d;
}

export interface PackHead { title: string; sub: string; tone: "ok" | "stale" | "none"; showDate: string }

/** The line under the title. The close date is always the pack's own trade date. */
export function packHead(body: FoEodBody | null | undefined, failed: boolean): PackHead {
  const none = (sub: string): PackHead => ({ title: "End-of-day F&O", sub, tone: "none", showDate: "" });
  if (!body) return none(failed ? "Could not load. Showing dashes." : "Loading");
  if (body.enabled === false) return none("Switched off (FO_EOD_ENABLED=0).");
  const day = dayText(body.trade_date);
  if (!day) return none(body.error ? `No file yet. ${body.error}.` : "No file yet. Nothing is shown rather than an old number.");
  const exp = dayText(body.expected_date);
  const behind = body.stale === true || (exp !== "" && typeof body.trade_date === "string" && typeof body.expected_date === "string" && body.trade_date < body.expected_date);
  const age = ageDays(body.trade_date, body.today_ist);
  const ageNote = age !== null && age >= 4 ? ` (${age} days ago)` : "";
  if (behind) {
    return { title: "End-of-day F&O", tone: "stale", showDate: day,
      sub: `Latest file (close of ${exp}) is not out yet. Showing the close of ${day}${ageNote}.${failed ? " Refresh failed." : ""}` };
  }
  return { title: "End-of-day F&O", tone: "ok", showDate: day, sub: `Close of ${day}${ageNote}. Read from the exchange's F&O file, not live.${failed ? " Refresh failed, showing the last read." : ""}` };
}

/** Rows of one build-up kind, biggest open-interest change (either way) first, at most `n`; `total` is how many there were. */
export function flowList(rows: FoRow[] | undefined, kind: Buildup, n = 10): { rows: FoRow[]; total: number } {
  const all = (rows ?? []).filter((r) => r && r.buildup === kind && fin(r.oi_chg_pct));
  all.sort((a, b) => Math.abs(b.oi_chg_pct as number) - Math.abs(a.oi_chg_pct as number) || a.symbol.localeCompare(b.symbol));
  return { rows: all.slice(0, Math.max(0, n)), total: all.length };
}

/** Stocks with a basis, sorted: "premium" = highest first, "discount" = lowest first (only the negative ones). */
export function basisList(rows: FoRow[] | undefined, side: "premium" | "discount", n = 12): { rows: FoRow[]; total: number } {
  const all = (rows ?? []).filter((r) => r && fin(r.basis_pct) && (side === "premium" ? (r.basis_pct as number) > 0 : (r.basis_pct as number) < 0));
  all.sort((a, b) => side === "premium"
    ? (b.basis_pct as number) - (a.basis_pct as number) || a.symbol.localeCompare(b.symbol)
    : (a.basis_pct as number) - (b.basis_pct as number) || a.symbol.localeCompare(b.symbol));
  return { rows: all.slice(0, Math.max(0, n)), total: all.length };
}

/** Rollover read: highest next-month share first, only rows that have one; near expiry (rolling) rows first because that is when it matters. */
export function rolloverList(rows: FoRow[] | undefined, n = 12): { rows: FoRow[]; total: number } {
  const all = (rows ?? []).filter((r) => r && fin(r.roll_pct));
  all.sort((a, b) => Number(!!b.rolling) - Number(!!a.rolling) || (b.roll_pct as number) - (a.roll_pct as number) || a.symbol.localeCompare(b.symbol));
  return { rows: all.slice(0, Math.max(0, n)), total: all.length };
}

export function countLine(counts: FoEodBody["counts"] | undefined): string {
  if (!counts || !fin(counts.stocks)) return DASH;
  return BUILDUP_LIST.map((b) => `${b.label} ${fin(counts[b.id]) ? counts[b.id] : DASH}`).join(" · ") + ` · of ${counts.stocks} stocks`;
}
