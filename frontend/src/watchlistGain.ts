// E28 (2026-10-22): "% since added" for the watchlist. Pure helpers (no React, no network) so they can be tested with
//   node --experimental-strip-types watchlistGain.test.mjs   (Node >= 22.6)
// The baseline is the price seen when a symbol was added on THIS device (browser storage); a symbol that was already on the list gets its first
// seen price instead and is labelled "first seen". Nothing here places or changes a trade.

export interface AddedRecord { price: number; ts: number; source: "added" | "first_seen" }
export type AddedMap = Record<string, AddedRecord>;

export const STORAGE_KEY = "stockky.watchlist.added.v1";
export const MOVE_ALERT_PCT = 5;   // highlight a move of at least this many percent either way

function validPrice(p: unknown): p is number {
  return typeof p === "number" && Number.isFinite(p) && p > 0;
}

export function parseAdded(raw: unknown): AddedMap {
  let obj: unknown = raw;
  if (typeof raw === "string") {
    try { obj = JSON.parse(raw); } catch { return {}; }
  }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return {};
  const out: AddedMap = {};
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    const r = v as Partial<AddedRecord> | null;
    if (r && validPrice(r.price) && typeof r.ts === "number" && Number.isFinite(r.ts) && (r.source === "added" || r.source === "first_seen")) {
      out[k.toUpperCase()] = { price: r.price, ts: r.ts, source: r.source };
    }
  }
  return out;
}

/** Keep only symbols still on the list (a removed symbol forgets its baseline, so re-adding starts fresh). */
export function prune(map: AddedMap, symbols: string[]): AddedMap {
  const keep = new Set(symbols.map((s) => s.toUpperCase()));
  const out: AddedMap = {};
  for (const [k, v] of Object.entries(map)) if (keep.has(k)) out[k] = v;
  return out;
}

/** Record a baseline if the symbol has none. An explicit add always replaces a first-seen guess. Returns a new map. */
export function withBaseline(map: AddedMap, symbol: string, price: unknown, source: AddedRecord["source"], now: number): AddedMap {
  const key = symbol.toUpperCase();
  if (!validPrice(price)) return map;
  const cur = map[key];
  if (cur && !(source === "added" && cur.source === "first_seen")) return map;
  return { ...map, [key]: { price, ts: now, source } };
}

/** Percent change from the baseline, or null when either price is unusable. */
export function pctSince(rec: AddedRecord | undefined | null, price: unknown): number | null {
  if (!rec || !validPrice(rec.price) || !validPrice(price)) return null;
  return (price / rec.price - 1) * 100;
}

export function formatPct(p: number | null): string {
  if (p === null || !Number.isFinite(p)) return "—";
  const r = Math.round(p * 100) / 100;
  return `${r > 0 ? "+" : ""}${r.toFixed(2)}%`;
}

export function tone(p: number | null): "up" | "down" | "flat" | "none" {
  if (p === null || !Number.isFinite(p)) return "none";
  if (p > 0.005) return "up";
  if (p < -0.005) return "down";
  return "flat";
}

export function isBigMove(p: number | null, threshold: number = MOVE_ALERT_PCT): boolean {
  return p !== null && Number.isFinite(p) && Math.abs(p) >= threshold;
}

export function label(rec: AddedRecord | undefined | null): string {
  if (!rec) return "";
  return rec.source === "added" ? "since added" : "since first seen";
}

// E166: what the Add box accepts (pure). The old box added ANY text (spaces, punctuation, a pasted company name) to the saved
// watchlist and silently cleared a duplicate. NSE codes are letters, digits, "&" and "-" (M&M, BAJAJ-AUTO), up to 20 characters.
export type SymbolCheck =
  | { ok: true; symbol: string }
  | { ok: false; reason: "empty" | "duplicate" | "invalid"; symbol: string; message: string };
export function checkSymbol(raw: unknown, existing: string[]): SymbolCheck {
  const sym = (typeof raw === "string" ? raw : "").trim().toUpperCase().replace(/\.NS$/i, "").replace(/\.BO$/i, "");
  if (!sym) return { ok: false, reason: "empty", symbol: "", message: "" };
  if (!/^[A-Z0-9][A-Z0-9&-]{0,19}$/.test(sym)) return { ok: false, reason: "invalid", symbol: sym, message: "Use the NSE code (letters and digits, for example TCS or M&M), not a name." };
  if (existing.includes(sym)) return { ok: false, reason: "duplicate", symbol: sym, message: `${sym} is already on your watchlist.` };
  return { ok: true, symbol: sym };
}
