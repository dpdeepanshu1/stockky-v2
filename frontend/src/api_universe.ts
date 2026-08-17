/**
 * API helpers for "Send the Stock Universe For Training".
 * Sends the FULL daily scan universe (not only actionable) to the training service.
 */

import { api } from "./api";

export type UniverseIngestPayload = {
  symbols: string[];
  decisions?: Record<string, string>;
  scores?: Record<string, number>;
  feature_snapshots?: Record<string, Record<string, unknown>>;
  source?: "daily_scan" | "manual_universe";
  retention_hours?: number;
  trigger_training?: boolean;
};

export type UniverseIngestResult = {
  ok: boolean;
  ingested: number;
  message: string;
  source?: string;
  expires_at?: string;
  retention_hours?: number;
  training_triggered?: boolean;
};

/**
 * Send the full stock universe from a market scan into training storage.
 * Button label in UI: "Send the Stock Universe For Training"
 */
export async function sendStockUniverseForTraining(
  payload: UniverseIngestPayload
): Promise<UniverseIngestResult> {
  const body = {
    symbols: payload.symbols,
    decisions: payload.decisions || {},
    scores: payload.scores || {},
    feature_snapshots: payload.feature_snapshots || {},
    source: payload.source || "manual_universe",
    retention_hours: payload.retention_hours ?? 48,
    trigger_training: payload.trigger_training ?? true,
  };

  // Prefer dedicated universe endpoint; fall back to generic training path if needed
  try {
    const res = await api.post("/training/api/universe/train-from-universe", body);
    return res.data as UniverseIngestResult;
  } catch (e1) {
    try {
      const res = await api.post("/training/api/universe/ingest", body);
      return res.data as UniverseIngestResult;
    } catch (e2) {
      // Last resort: older training record endpoint (best-effort)
      console.warn("Universe endpoints unavailable, falling back", e2);
      throw e2;
    }
  }
}

/**
 * Build payload from a full scan result (all symbols, not only BUY/PREPARE).
 */
export function buildUniversePayloadFromScan(scanResult: {
  recommendations?: Array<{
    symbol?: string;
    decision?: string;
    combined_score?: number;
    score?: number;
    feature_snapshot?: Record<string, unknown>;
  }>;
  all_results?: Array<{
    symbol?: string;
    decision?: string;
    combined_score?: number;
    score?: number;
    feature_snapshot?: Record<string, unknown>;
  }>;
  universe?: string[];
}): UniverseIngestPayload {
  const rows =
    scanResult.all_results ||
    scanResult.recommendations ||
    (scanResult.universe || []).map((s) => ({ symbol: s }));

  const symbols: string[] = [];
  const decisions: Record<string, string> = {};
  const scores: Record<string, number> = {};
  const feature_snapshots: Record<string, Record<string, unknown>> = {};

  for (const r of rows) {
    const sym = (r.symbol || "").toUpperCase();
    if (!sym) continue;
    symbols.push(sym);
    if (r.decision) decisions[sym] = r.decision;
    const sc = r.combined_score ?? r.score;
    if (typeof sc === "number") scores[sym] = sc;
    if (r.feature_snapshot) feature_snapshots[sym] = r.feature_snapshot;
  }

  return {
    symbols: [...new Set(symbols)],
    decisions,
    scores,
    feature_snapshots,
    source: "manual_universe",
    retention_hours: 48,
    trigger_training: true,
  };
}
