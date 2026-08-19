/**
 * Universal safe price binding for scan / decision / trades / surprise UI.
 *
 * Backend + feeds may stamp any of:
 *   price | cmp | last_price | ltp | close | current_price | prev_close | live_price
 *
 * Step 4 fix: include every known alias so the UI never shows "Syncing…" when
 * a valid positive price exists under another key.
 */

export type PriceLike = {
  close?: number | null;
  price?: number | null;
  cmp?: number | null;
  current_price?: number | null;
  ltp?: number | null;
  last_price?: number | null;
  live_price?: number | null;
  prev_close?: number | null;
};

/** Ordered candidate keys — live-ish first, then decision aliases, then baseline. */
const CANDIDATE_KEYS = [
  "price",
  "cmp",
  "last_price",
  "ltp",
  "close",
  "current_price",
  "live_price",
  "prev_close",
] as const;

/**
 * Step 4 — universal safe price extractor (matches fix spec `getSafePrice`).
 * Returns 0 when no positive numeric price is found.
 */
export function getSafePrice(item: any): number {
  if (!item || typeof item !== "object") return 0;

  for (const key of CANDIDATE_KEYS) {
    const val = item[key];
    if (val !== undefined && val !== null && val !== "") {
      const num = Number(val);
      if (!Number.isNaN(num) && num > 0) {
        return num;
      }
    }
  }

  return 0;
}

/**
 * Resolve display price with optional live override (tick / websocket).
 * Prefer live when valid; otherwise fall through getSafePrice keys.
 */
export function resolveDisplayPrice(
  item: PriceLike | null | undefined,
  live?: number | null
): number {
  if (live != null && Number(live) > 0) return Number(live);
  return getSafePrice(item);
}

/**
 * Format as ₹ Indian locale, or empty placeholder when no price yet.
 */
export function formatInrPrice(
  item: PriceLike | null | undefined,
  live?: number | null,
  empty = "Syncing…"
): string {
  const px = resolveDisplayPrice(item, live);
  if (px <= 0) return empty;
  return `₹${px.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}
