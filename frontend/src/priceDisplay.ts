/**
 * Sticky Fix Step 5 — unified price binding for scan / decision / trades UI.
 * Backend stamps: close | price | cmp | current_price | ltp
 */

export type PriceLike = {
  close?: number | null;
  price?: number | null;
  cmp?: number | null;
  current_price?: number | null;
  ltp?: number | null;
  live_price?: number | null;
  prev_close?: number | null;
};

export function resolveDisplayPrice(
  item: PriceLike | null | undefined,
  live?: number | null
): number {
  if (live != null && Number(live) > 0) return Number(live);
  if (!item) return 0;
  for (const k of ["cmp", "price", "current_price", "close", "ltp", "live_price"] as const) {
    const v = item[k];
    if (v != null && Number(v) > 0) return Number(v);
  }
  // Last resort: prev_close from feed/baseline
  if (item.prev_close != null && Number(item.prev_close) > 0) {
    return Number(item.prev_close);
  }
  return 0;
}

export function formatInrPrice(
  item: PriceLike | null | undefined,
  live?: number | null,
  empty = "Syncing…"
): string {
  const px = resolveDisplayPrice(item, live);
  if (px <= 0) return empty;
  return `₹${px.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}
