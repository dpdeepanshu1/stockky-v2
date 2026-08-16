// frontend/src/api/api.ts

const STORAGE_KEY = "stockky:api_url";

export function getApiUrl(): string {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored) return stored;
  return (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");
}

export function setApiUrl(url: string) {
  const clean = url.trim().replace(/\/$/, "");
  if (clean) localStorage.setItem(STORAGE_KEY, clean);
  else localStorage.removeItem(STORAGE_KEY);
}

export function clearApiUrlOverride() {
  localStorage.removeItem(STORAGE_KEY);
}

export interface FundamentalMetrics {
  revenue_growth: number | null;
  earnings_growth: number | null;
  roe: number | null;
  debt_to_equity: number | null;
  free_cashflow: number | null;
  profit_margins: number | null;
  institutional_holding: number | null;
  pe_ratio: number | null;
  forward_pe: number | null;
}

export interface Decision {
  symbol: string;
  decision: "BUY NOW" | "PREPARE TO BUY" | "HOLD" | "DO NOT BUY" | "SELL" | "WAIT";
  confidence: "High" | "Medium" | "Low";
  combined_score: number;
  technical_score: number;
  fundamental_score: number;
  news_score: number | null;
  prediction_score: number | null;
  market_score: number;
  market_sentiment_adjustment: number;
  training_score: number;
  event_risk: boolean;
  entry_range: { low: number | null; high: number | null } | null;
  target: number | null;
  stop_loss: number | null;
  holding_period: string;
  close: number | null;
  support: number | null;
  resistance: number | null;
  reasons: {
    technical: string[];
    fundamental: string[];
    news?: string[];
    prediction?: string[];
    event?: string[];
    market?: string[];
    training?: string[];
  };
  valuation: string;
  sector: string | null;
  natural_language_summary?: string;
  fundamental_metrics?: FundamentalMetrics;
  data_insufficient?: boolean;
  fundamental_fallback?: boolean;
  event_score_delta?: number;
  event_data?: Record<string, unknown> | null;
  holding_period_estimate?: {
    min_days: number;
    max_days: number;
    expected_by_earliest: string;
    expected_by_latest: string;
    label: string;
  } | null;
  long_term_hold?: boolean;
  long_term_hold_estimate?: {
    min_months: number;
    max_months: number;
    expected_by_earliest: string;
    expected_by_latest: string;
    label: string;
  } | null;
}

export interface ScanResult {
  scanned: number;
  universe_size: number;
  watchlist_size: number;
  recommendations: Decision[];
  watchlist_candidates: Decision[];
  verdict: string;
  market_mood: string;
  market_stats: {
    buy_signals: number;
    sell_signals: number;
    hold_signals: number;
    cautious: number;
  };
  all_results: Decision[];
  errors: { symbol: string; error: string }[];
}

export interface ScanStatus {
  status: "running" | "done" | "error";
  total: number;
  processed: number;
  elapsed: number;
  estimated_remaining?: number | null;
  result?: ScanResult;
  error?: string;
}

export interface MarketStock {
  symbol: string;
  price: number;
  change: number;
  change_pct: number;
  volume?: number;
  high?: number;
  low?: number;
}

export interface MarketResponse {
  data: MarketStock[];
  count: number;
}

export interface NotificationChannelStatus {
  configured: boolean;
  enabled: boolean;
  masked: string;
  chat_id?: string;
}

export interface NotificationConfig {
  discord: NotificationChannelStatus;
  slack: NotificationChannelStatus;
  telegram: NotificationChannelStatus;
  persisted: boolean;
}

export interface SystemServiceStatus {
  ok: boolean;
  required: boolean;
  status: string;
  seconds?: number;
  error?: string;
  url?: string | null;
}

export interface SystemHealth {
  required_ok: boolean;
  all_ok: boolean;
  services: Record<string, SystemServiceStatus>;
}

// ───────────────────────────────────────────────
// Training Service types – updated to match actual responses
// ───────────────────────────────────────────────

export interface TrainingStatusResponse {
  service_url: string;
  production_model_exists: boolean;
  last_training: string | null;
  dataset_size: number;
  num_symbols: number;
  metrics: Record<string, number>;
  fold_details: Array<{
    fold: number;
    train_start: string;
    train_end: string;
    val_start: string;
    val_end: string;
    train_samples: number;
    val_samples: number;
  }>;
  model_version: string | null;
  training_in_progress: boolean;
}

export interface TriggerTrainingResponse {
  status: string;
  service_url?: string;
}

export interface TrainingModelStatus {
  production_model: {
    version: string;
    training_date: string;
    features: string[];
    metrics: {
      accuracy: number;
      precision: number;
      recall: number;
      f1: number;
      roc_auc: number;
      train_size: number;
      val_size: number;
    };
    status: string;
  } | null;
  candidate_model: {
    version: string;
    training_date: string;
    features: string[];
    metrics: {
      accuracy: number;
      precision: number;
      recall: number;
      f1: number;
      roc_auc: number;
      train_size: number;
      val_size: number;
    };
    status: string;
  } | null;
  last_training_date: string | null;
  dataset_size: number;
  performance: {
    'T+1 Success': number;
    'T+5 Success': number;
    'Average T+1': number;
    'Average T+5': number;
  };
}

export interface TrainingScore {
  symbol: string;
  training_score: number;
  t1_success_probability: number;
  t5_success_probability: number;
  model_success_probability: number | null;
  historical_similarity: number;
  similar_setups: Array<{
    symbol: string;
    similarity: number;
    outcome: string;
  }>;
}

export interface PredictionHistoryItem {
  prediction_id: string;
  symbol: string;
  timestamp: string;
  decision: string;
  price: number;
  t1_success: number;
  t5_success: number;
  outcomes: Array<{ period: string; return_pct: number | null; success: number }>;
}

export interface PeriodRollupItem {
  period: string;
  predictions_recorded: number;
  unique_symbols: number;
  buy_now: number;
  prepare_to_buy: number;
  t1_evaluated: number;
  t1_success_rate: number | null;
  t1_avg_return_pct: number | null;
  t5_evaluated: number;
  t5_success_rate: number | null;
  t5_avg_return_pct: number | null;
  t1_pending: number;
  t5_pending: number;
}

export interface PaperTrade {
  trade_id: string;
  prediction_id: string;
  symbol: string;
  capital_allocated: number;
  entry_price: number;
  quantity: number;
  entry_date: string;
  target: number | null;
  stop_loss: number | null;
  status: "OPEN" | "CLOSED";
  current_price: number | null;
  exit_price: number | null;
  exit_date: string | null;
  exit_reason: string | null;
  pnl_amount: number | null;
  pnl_pct: number | null;
  last_marked_at: string | null;
}

export interface PortfolioSummary {
  cash_balance: number;
  total_deposited: number;
  realized_pnl: number;
  open_positions_value: number;
  open_positions_pnl: number;
  total_equity: number;
  open_positions: number;
  closed_positions: number;
  win_rate: number | null;
}

export interface TradeReportBucket {
  period: string;
  trades_opened: number;
  trades_closed: number;
  realized_pnl: number;
  wins: number;
  losses: number;
  capital_deployed: number;
  win_rate: number | null;
}

export interface StockHistoryPoint {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface StockHistory {
  symbol: string;
  period: string;
  points: StockHistoryPoint[];
  change_pct: number | null;
}

export interface TrainingProgress {
  stage: "idle" | "loading_data" | "data_loaded" | "splitting" | "fitting_model" | "evaluating" | "saving_model" | "done" | "aborted";
  detail: Record<string, unknown>;
  timestamp: number | null;
  elapsed?: number | null;
}

export interface ActionablePick {
  symbol: string;
  decision: string;
  confidence: string;
  price: number;
  target?: number | null;
  stop_loss?: number | null;
  entry_range_low?: number | null;
  entry_range_high?: number | null;
  combined_score: number;
  technical_score: number;
  fundamental_score: number;
  news_score?: number | null;
  prediction_score?: number | null;
  market_score: number;
  training_score: number;
  event_risk: boolean;
  market_mood?: string | null;
  nifty_change_pct?: number | null;
  sensex_change_pct?: number | null;
  rsi?: number | null;
  macd?: string | null;
  ema?: string | null;
  volume_ratio?: number | null;
  debt_to_equity?: number | null;
  roe?: number | null;
  roce?: number | null;
  holding_period?: string | null;
  support?: number | null;
  resistance?: number | null;
  sector?: string | null;
  valuation?: string | null;
  market_sentiment_adjustment?: number | null;
  feature_snapshot?: Record<string, unknown> | null;
}

export interface ActionableCommitResult {
  symbol: string;
  prediction_id: string;
  record_status: "stored" | "already_recorded";
  trade_id: string | null;
  trade_status: string;
}

// ───────────────────────────────────────────────
// Market Indices types
// ───────────────────────────────────────────────

export interface IndexData {
  price: number;
  change: number;
  change_pct: number;
}

export interface MarketIndicesResponse {
  nifty: IndexData;
  sensex: IndexData;
  market_mood: "BULLISH" | "BEARISH" | "NEUTRAL";
  market_score: number;
  fetched_at?: string;
  stale?: boolean;
}

// ───────────────────────────────────────────────
// Event types (from first file)
// ───────────────────────────────────────────────

export interface EventItem {
  type: string;
  date: string | null;
  description: string;
}

export interface InstitutionalHolder {
  holder: string;
  shares: number | null;
  pct_held: number | null;
}

export interface CategorizedEvents {
  symbol: string;
  upcoming: EventItem[];
  recent: EventItem[];
  recent_changes: string[];
  institutional_holders: InstitutionalHolder[];
  checked_at: string | null;
}

// ───────────────────────────────────────────────
// Request helper
// ───────────────────────────────────────────────

async function request<T>(path: string, init?: RequestInit, retries = 2, timeoutMs = 60000): Promise<T> {
  const base = getApiUrl();
  if (!base) {
    throw new Error(
      "Backend URL isn't set. Open Settings (top right) and paste your API Gateway URL."
    );
  }

  const url = `${base}${path}`;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      ...init,
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`${response.status} ${response.statusText}${body ? `: ${body.slice(0, 240)}` : ""}`);
    }

    return response.json();
  } catch (error) {
    clearTimeout(timeoutId);

    if (retries > 0 && (error instanceof TypeError || error instanceof DOMException)) {
      await new Promise((resolve) => setTimeout(resolve, 1000 * (3 - retries)));
      return request<T>(path, init, retries - 1, timeoutMs);
    }

    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error(
        `Request timed out after ${timeoutMs / 1000} seconds. The backend may be waking up (free-tier cold start). Try again in a moment.`
      );
    }

    throw error;
  }
}

export async function wakeService(url: string): Promise<void> {
  if (!url) return;
  try {
    await fetch(url + "/health", { mode: "no-cors" });
  } catch {
    // Ignore – request still wakes the service
  }
}

// ───────────────────────────────────────────────
// API object
// ───────────────────────────────────────────────

export const api = {
  ping: () => request<{ status: string; service: string }>("/health", undefined, 2, 30000),

  systemHealth: () => request<SystemHealth>("/system/health", undefined, 2, 60000),

  getStock: (symbol: string, alreadyOwned = false) =>
    request<Decision>(`/stock/${symbol}?already_owned=${alreadyOwned}`, undefined, 2, 60000),

  runScan: () => request<ScanResult>("/scan", undefined, 2, 120000),

  scanStart: (forceRefresh = false) =>
    request<{ task_id: string }>(
      `/scan/start?force_refresh=${forceRefresh}`,
      { method: "POST" },
      2,
      120000
    ),

  scanStatus: (taskId: string) =>
    request<ScanStatus>(`/scan/status/${taskId}`, undefined, 2, 10000),

  scanCancel: (taskId: string) =>
    request<{ status: string; processed_so_far?: number; total?: number }>(
      `/scan/cancel/${taskId}`, { method: "POST" }, 1, 10000
    ),

  scanWatchlist: () =>
    request<ScanResult>("/scan/watchlist", undefined, 2, 120000),

  getWatchlist: () => request<{ symbols: string[] }>("/watchlist", undefined, 2, 30000),

  setWatchlist: (symbols: string[]) =>
    request<{ symbols: string[] }>(
      "/watchlist",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols }),
      },
      2,
      30000
    ),

  addToWatchlist: (symbol: string) =>
    request<{ symbols: string[] }>(
      "/watchlist/add",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: [symbol] }),
      },
      2,
      30000
    ),

  addManyToWatchlist: (symbols: string[]) =>
    request<{ symbols: string[] }>(
      "/watchlist/add",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols }),
      },
      2,
      30000
    ),

  marketTopGainers: () => request<MarketResponse>("/market/top-gainers", undefined, 2, 30000),
  marketTopLosers: () => request<MarketResponse>("/market/top-losers", undefined, 2, 30000),
  marketMostActive: () => request<MarketResponse>("/market/most-active", undefined, 2, 30000),
  marketTrending: () => request<MarketResponse>("/market/trending", undefined, 2, 30000),

  marketIndices: (forceRefresh = false) =>
    request<MarketIndicesResponse>(
      `/market/indices?force_refresh=${forceRefresh}`,
      undefined,
      2,
      10000
    ),

  getNotificationConfig: () => request<NotificationConfig>("/notifications/config", undefined, 2, 30000),

  saveNotificationConfig: (update: {
    discord_webhook_url?: string;
    slack_webhook_url?: string;
    telegram_bot_token?: string;
    telegram_chat_id?: string;
    enabled?: Partial<Record<"discord" | "slack" | "telegram", boolean>>;
  }) =>
    request<NotificationConfig>(
      "/notifications/config",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(update),
      },
      2,
      30000
    ),

  clearNotificationChannel: (channel: "discord" | "slack" | "telegram") =>
    request<NotificationConfig>(`/notifications/config/${channel}`, { method: "DELETE" }, 2, 30000),

  testNotifications: () =>
    request<{ delivered: boolean; note?: string; results?: Record<string, string> }>(
      "/notifications/test",
      { method: "POST" },
      2,
      30000
    ),

  sendPicksToTelegram: (payload: { type: string; recommendations: Decision[] }) =>
    request<{ success: boolean; sent: number; message: string }>(
      "/notifications/send-picks",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
      2,
      30000
    ),

  // ─── Training Service endpoints ───

  getTrainingStatus: () =>
    request<TrainingStatusResponse>("/training/status", undefined, 2, 30000),

  triggerTraining: (labelSource: "t1_outcome" | "trade_pnl" = "t1_outcome") =>
    request<TriggerTrainingResponse>(
      `/training/api/train?label_source=${labelSource}`,
      { method: "POST" },
      2,
      60000
    ),

  getTrainingScore: (symbol: string) =>
    request<TrainingScore>(`/training/score/${symbol}`, undefined, 2, 30000),

  clearTrainingLock: () =>
    request<{ status: string }>("/training/lock", { method: "DELETE" }, 1, 15000),

  getPredictionHistory: (limit = 20) =>
    request<{ predictions: PredictionHistoryItem[]; total: number }>(
      `/training/api/predictions/history?limit=${limit}`, undefined, 2, 30000
    ),

  getDailyRollup: (days = 30) =>
    request<PeriodRollupItem[]>(`/training/api/metrics/daily?days=${days}`, undefined, 2, 30000),

  getWeeklyRollup: (weeks = 12) =>
    request<PeriodRollupItem[]>(`/training/api/metrics/weekly?weeks=${weeks}`, undefined, 2, 30000),

  triggerEvaluation: (period: "t1" | "t5") =>
    request<{ status: string }>(`/training/api/evaluate/${period}`, { method: "POST" }, 1, 30000),

  commitActionablePicks: (picks: ActionablePick[], capitalPerTrade = 10000, openTrades = true) =>
    request<{ results: ActionableCommitResult[] }>(
      "/training/api/actionable/commit",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ picks, capital_per_trade: capitalPerTrade, open_trades: openTrades }),
      },
      1,
      60000
    ),

  getTrades: (status: "open" | "closed" | "all" = "all") =>
    request<PaperTrade[]>(`/training/api/trades?status=${status}`, undefined, 2, 30000),

  getTradesSummary: () =>
    request<PortfolioSummary>("/training/api/trades/summary", undefined, 2, 30000),

  getPortfolioSummary: () =>
    request<PortfolioSummary>("/training/api/portfolio/summary", undefined, 2, 30000),

  depositFunds: (amount: number, note?: string) =>
    request<{ status: string; cash_balance: number; total_deposited: number }>(
      "/training/api/portfolio/deposit",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ amount, note }) },
      1,
      15000
    ),

  getDailyTradeReport: (days = 30) =>
    request<TradeReportBucket[]>(`/training/api/trades/report/daily?days=${days}`, undefined, 2, 30000),

  getWeeklyTradeReport: (weeks = 12) =>
    request<TradeReportBucket[]>(`/training/api/trades/report/weekly?weeks=${weeks}`, undefined, 2, 30000),

  getStockHistory: (symbol: string, period: "1d" | "5d" | "1mo" | "1y" | "5y" = "1mo") =>
    request<StockHistory>(`/training/api/stock/history/${symbol}?period=${period}`, undefined, 2, 30000),

  // ─── Event endpoint (from first file) ───
  getSymbolEvents: (symbol: string) =>
    request<CategorizedEvents>(`/events/${symbol}`, undefined, 2, 30000),

  getTrainingProgress: () =>
    request<TrainingProgress>("/training/api/train/progress", undefined, 1, 10000),

  markTradesToMarket: () =>
    request<{ status: string }>("/training/api/trades/mark-to-market", { method: "POST" }, 1, 30000),

  // ─── New endpoints from second file ───
  clearTradesBackup: () =>
    request<any>("/training/api/trades/clear-backup", { method: "POST" }, 1, 30000),

  listTradeBackups: () =>
    request<any>("/training/api/trades/backups", undefined, 1, 15000),

  addToTrade: (tradeId: string, qty: number, price?: number) =>
    request<any>(`/training/api/trades/${tradeId}/add`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ quantity: qty, price }),
    }, 1, 30000),

  closeTrade: (tradeId: string) =>
    request<{ status: string; trade_id: string; exit_price: number; pnl_pct: number }>(
      `/training/api/trades/${tradeId}/close`, { method: "POST" }, 1, 30000
    ),
};