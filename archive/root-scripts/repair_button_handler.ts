/**
 * Correct Repair button handler for Data Health Audit / Surprise Stocks.
 * Paste into SurpriseStocks.tsx, DataHealthAudit.tsx, or equivalent.
 *
 * Backend routes (any of these work after Step 6):
 *   POST /api/feed/repair-single/{SYMBOL}
 *   POST /data-feed/repair-single/{SYMBOL}
 *   POST /api/data-feed/repair-single/{SYMBOL}
 *   POST /feed/repair-single/{SYMBOL}
 *
 * Always returns 200 JSON: { status, ok, symbol, patched_fields, still_missing, complete, message }
 */
export async function handleRepairSingle(
  symbol: string,
  opts?: {
    apiBase?: string;
    onStart?: (sym: string) => void;
    onDone?: (sym: string, result: any) => void;
    onError?: (sym: string, err: unknown) => void;
    refresh?: () => Promise<void>;
  }
) {
  const base = (opts?.apiBase || "").replace(/\/$/, "");
  const sym = encodeURIComponent(String(symbol || "").toUpperCase().trim());
  opts?.onStart?.(symbol);
  try {
    const res = await fetch(`${base}/api/feed/repair-single/${sym}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok && data?.status !== "success") {
      throw new Error(data?.message || `HTTP ${res.status}`);
    }
    opts?.onDone?.(symbol, data);
    if (opts?.refresh) await opts.refresh();
    return data;
  } catch (err) {
    console.error("Repair failed", symbol, err);
    opts?.onError?.(symbol, err);
    throw err;
  }
}

/** Batch repair (rate-safe, default 10) */
export async function handleRepairBatch(limit = 10, apiBase = "") {
  const base = apiBase.replace(/\/$/, "");
  const res = await fetch(`${base}/api/feed/repair-batch?limit=${limit}`, {
    method: "POST",
  });
  return res.json();
}
