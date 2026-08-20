import { useState, useMemo, useEffect } from "react";
import { BuySniperModal, type BuySuggestion } from "./BuySniperModal";
import { ScanResult, Decision, api, ActionablePick, streamMarketScan } from "../api";
// streamMarketScan available for progressive NDJSON consumption (see upsertScanResultItem below)
import { sendStockUniverseForTraining, buildUniversePayloadFromScan } from "../api_universe";
import { useStockkyRealtime } from "../useRealtime";
import { decisionStyle } from "../decisionStyle";
import { resolveDisplayPrice, formatInrPrice } from "../priceDisplay";

interface Props {
  result: ScanResult;
  onSelect: (symbol: string) => void;
  onBack: () => void;
  onAddToWatchlist: (symbol: string) => void;
  onAddManyToWatchlist: (symbols: string[], label: string) => void;
  onSendTopPicks: () => Promise<void>;      
  onSendAllActionable: () => Promise<void>; 
}

// Value-buying adjustment: a stock priced under Rs 2000 with already-decent
// fundamentals gets a score bonus (bigger the further under 2000, so a
// Rs 200 stock is favored over a Rs 1900 stock, all else equal) — cheap
// alone isn't rewarded, only cheap + fundamentally sound, so this doesn't
// just push penny stocks to the top. Stocks over Rs 2000 are excluded
// entirely from the value-adjusted "Top Picks" ranking, per the Rs 2000
// cap, though they still appear normally in "All results" below —
// nothing is hidden, this is an additional ranked view, not a filter on
// the underlying data.
const PRICE_CAP = 2000;
const VALUE_BONUS_MAX = 8; // max points added to combined_score
const MIN_FUNDAMENTAL_FOR_BONUS = 50; // out of 100 — "good stock", not just cheap

function valueAdjustedScore(d: Decision): { score: number; eligible: boolean; bonus: number } {
  const price = resolveDisplayPrice(d);
  if (price <= 0) return { score: d.combined_score, eligible: true, bonus: 0 };
  const eligible = price <= PRICE_CAP;
  if (!eligible) return { score: d.combined_score, eligible: false, bonus: 0 };
  const isGoodStock = d.fundamental_score >= MIN_FUNDAMENTAL_FOR_BONUS;
  const bonus = isGoodStock ? (1 - price / PRICE_CAP) * VALUE_BONUS_MAX : 0;
  return { score: d.combined_score + bonus, eligible: true, bonus: Math.round(bonus * 10) / 10 };
}

export function toActionablePick(d: Decision): ActionablePick {
  return {
    symbol: d.symbol,
    decision: d.decision,
    confidence: d.confidence,
    price: d.close ?? 0,
    target: d.target,
    stop_loss: d.stop_loss,
    entry_range_low: d.entry_range?.low ?? null,
    entry_range_high: d.entry_range?.high ?? null,
    combined_score: d.combined_score,
    technical_score: d.technical_score,
    fundamental_score: d.fundamental_score,
    news_score: d.news_score,
    prediction_score: d.prediction_score,
    market_score: d.market_score,
    training_score: d.training_score ?? 0,
    // Backend requires bool — null from lite/stream paths caused HTTP 422
    event_risk: Boolean(d.event_risk),
    holding_period: d.holding_period ?? null,
    support: d.support,
    resistance: d.resistance,
    sector: d.sector,
    valuation: d.valuation,
    market_sentiment_adjustment: d.market_sentiment_adjustment,
    debt_to_equity: d.fundamental_metrics?.debt_to_equity ?? null,
    roe: d.fundamental_metrics?.roe ?? null,
    roce: null, // not present in Decision/FundamentalMetrics from api-gateway today
    rsi: null, macd: null, ema: null, volume_ratio: null, // decision-engine doesn't populate these yet either
    market_mood: null, nifty_change_pct: null, sensex_change_pct: null, // not exposed on Decision today
  };
}

function rowSummary(r: any): string {
  return (
    r?.news_data?.summary ||
    r?.event_data?.summary ||
    (Array.isArray(r?.reasons?.news) && r.reasons.news[0]) ||
    (Array.isArray(r?.reasons?.event) && r.reasons.event[0]) ||
    r?.natural_language_summary ||
    ""
  );
}

function qualityLabel(r: any): string {
  const dq = r?.data_quality;
  if (typeof dq === "string") return dq;
  if (dq?.quality) return String(dq.quality);
  if (r?.data_insufficient) return "low";
  if (r?.circuit_open) return "low";
  return "";
}

export default function ScanPanel({ result, onSelect, onBack, onAddToWatchlist, onAddManyToWatchlist, onSendTopPicks, onSendAllActionable }: Props) {
  // Local loading states for animations
  const [isSendingTelegram, setIsSendingTelegram] = useState<"top5" | "all" | null>(null);
  const [addingWatchlist, setAddingWatchlist] = useState<string | null>(null);
  const [committing, setCommitting] = useState<"training" | "trade_top5" | "trade_all" | "universe" | null>(null);
  const [commitMessage, setCommitMessage] = useState<string | null>(null);
  const [filterChip, setFilterChip] = useState<"all" | "buy" | "prepare" | "avoid">("all");
  const [balanceLow, setBalanceLow] = useState<{ needed: number; available: number } | null>(null);
  // Step 4 — Buy Sniper
  const [sniperOpen, setSniperOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<BuySuggestion[]>([]);
  const [sniperLoading, setSniperLoading] = useState(false);
  const [sniperError, setSniperError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshMsg, setRefreshMsg] = useState<string | null>(null);
  const { connected: quoteWs, subscribeQuotes, quotes: liveQuotes } = useStockkyRealtime();

  /** Keep first occurrence of each symbol (normalized). Prevents the same stock
   *  showing 2–3 times across Top Picks / value board / full table. */
  const uniqueBySymbol = (rows: Decision[]): Decision[] => {
    const seen = new Set<string>();
    const out: Decision[] = [];
    for (const d of rows) {
      if (!d || !d.symbol) continue;
      const key = String(d.symbol).toUpperCase().replace(/\.NS$|\.BO$/i, "").trim();
      if (!key || seen.has(key)) continue;
      seen.add(key);
      out.push(d);
    }
    return out;
  };

  const filteredResults = useMemo(() => {
    // Drop high-ticket SKIP rows (price > ₹5000) so they never clutter the table
    const raw = (result.all_results || []).filter((d: any) => {
      if (!d || d.skipped_high_price) return false;
      const dec = String(d.decision || "").toUpperCase();
      if (dec === "SKIP" || dec === "SKIPPED") return false;
      const px = Number(d.price ?? d.close ?? d.cmp ?? d.ltp ?? 0);
      if (px > 5000) return false;
      return true;
    });
    const rows = uniqueBySymbol(raw);
    if (filterChip === "buy") return rows.filter((d) => d.decision === "BUY NOW");
    if (filterChip === "prepare") return rows.filter((d) => d.decision === "PREPARE TO BUY");
    if (filterChip === "avoid") return rows.filter((d) => d.decision === "DO NOT BUY" || d.decision === "WAIT" || d.decision === "SELL");
    return rows;
  }, [result.all_results, filterChip]);

  const allSorted = useMemo(
    () => [...filteredResults].sort((a, b) => (b.combined_score ?? 0) - (a.combined_score ?? 0)),
    [filteredResults]
  );

  /** Single conviction board: value-adjusted ranking, max 5 unique symbols.
   *  Replaces the old dual "recommendations" + "value-adjusted" grids that
   *  repeated the same stocks. */
  const convictionBoard = useMemo(() => {
    const actionable = filteredResults.filter(
      (d) => d.decision === "BUY NOW" || d.decision === "PREPARE TO BUY"
    );
    const ranked = actionable
      .map((d) => ({ decision: d, ...valueAdjustedScore(d) }))
      .filter((x) => x.eligible)
      .sort((a, b) => b.score - a.score);
    // Fallback: if value filter emptied the board, use raw server recommendations (deduped)
    if (ranked.length === 0) {
      const recs = uniqueBySymbol(result.recommendations || []).slice(0, 5);
      return recs.map((d) => ({ decision: d, score: d.combined_score, eligible: true, bonus: 0 }));
    }
    return ranked.slice(0, 5);
  }, [filteredResults, result.recommendations]);

  const allActionable = useMemo(
    () =>
      uniqueBySymbol(result.all_results || [])
        .filter((d) => d.decision === "BUY NOW" || d.decision === "PREPARE TO BUY")
        .sort((a, b) => valueAdjustedScore(b).score - valueAdjustedScore(a).score),
    [result.all_results]
  );

  const top5Actionable = useMemo(
    () => convictionBoard.map((x) => x.decision),
    [convictionBoard]
  );

  /** Symbols already on the conviction board — omit from "All results" hero feel,
   *  but still list them once in the full table (table is the source of truth). */
  const convictionSymbols = useMemo(() => {
    const s = new Set<string>();
    for (const x of convictionBoard) {
      const k = String(x.decision.symbol || "").toUpperCase().replace(/\.NS$|\.BO$/i, "").trim();
      if (k) s.add(k);
    }
    return s;
  }, [convictionBoard]);

  useEffect(() => {
    const rows = result.all_results || [];
    const syms = rows.slice(0, 25).map((r) => r.symbol).filter(Boolean);
    if (syms.length) subscribeQuotes(syms);
  }, [result.all_results, subscribeQuotes]);


  const handleSearchBuys = async () => {
    setSniperOpen(true);
    setSniperLoading(true);
    setSniperError(null);
    setSuggestions([]);
    try {
      const rows =
        (result?.all_results && result.all_results.length > 0
          ? result.all_results
          : result?.recommendations) || [];
      const data = await api.findBuys({
        stocks: rows,
        all_results: result?.all_results || [],
        recommendations: result?.recommendations || [],
        target_count: 4,
        min_conviction: 58,
      });
      setSuggestions((data?.suggestions || []) as BuySuggestion[]);
      if (data?.error) setSniperError(String(data.error));
    } catch (err: any) {
      console.error("Failed to find buys:", err);
      setSniperError(err?.message || "Failed to find buy setups");
      setSuggestions([]);
    } finally {
      setSniperLoading(false);
    }
  };

  const handleRefreshPrepareToBuy = async () => {
    setIsRefreshing(true);
    setRefreshMsg(null);
    try {
      const res = await api.refreshPrepareToBuy(58, 68);
      const n = res?.refreshed_count ?? 0;
      const total = (res?.symbols || []).length;
      setRefreshMsg(
        res?.message || `Refreshed ${n}/${total} Prepare-to-Buy quotes`
      );
    } catch (err: any) {
      setRefreshMsg(err?.message || "Prepare-to-Buy refresh failed");
    } finally {
      setIsRefreshing(false);
    }
  };



  const handleSendTopPicks = async () => {
    setIsSendingTelegram("top5");
    try {
      await onSendTopPicks();
    } finally {
      setIsSendingTelegram(null);
    }
  };

  const handleSendAllActionable = async () => {
    setIsSendingTelegram("all");
    try {
      await onSendAllActionable();
    } finally {
      setIsSendingTelegram(null);
    }
  };

  const handleAddToWatchlist = async (symbol: string) => {
    setAddingWatchlist(symbol);
    try {
      await onAddToWatchlist(symbol);
    } finally {
      setAddingWatchlist(null);
    }
  };

  /** Training only — records for T+1/T+5 tracking, does NOT open trades. */
  const handleAddActionableToTraining = async () => {
    if (allActionable.length === 0) {
      setCommitMessage("No BUY NOW / PREPARE TO BUY picks in this scan.");
      setTimeout(() => setCommitMessage(null), 4000);
      return;
    }
    setCommitting("training");
    setCommitMessage(null);
    try {
      const res = await api.commitActionablePicks(
        allActionable.map(toActionablePick),
        10000,
        false
      );
      const results = res.results || (res as any);
      const list = Array.isArray(results) ? results : (res as any).results || [];
      const stored = list.filter((r: any) => r.record_status === "stored").length;
      const updated = list.filter((r: any) => r.record_status === "updated").length;
      const already = list.filter((r: any) => r.record_status === "already_recorded").length;
      const parts: string[] = [];
      if (stored) parts.push(`${stored} new`);
      if (updated) parts.push(`${updated} refreshed`);
      if (already) parts.push(`${already} already in today's training set`);
      if (!parts.length) parts.push("no changes");
      const dbNote = (res as any).db_message
        || ((res as any).db_durable ? "saved to Postgres" : (res as any).db_backend === "sqlite" ? "SQLite (may reset on redeploy)" : "");
      setCommitMessage(
        `🎓 Training: ${parts.join(", ")} · T+1/T+5 tracking · no trades${dbNote ? " · " + dbNote : ""}`
      );
    } catch (err) {
      console.error(err);
      setCommitMessage(`Failed: ${(err as Error).message || "unknown error"}`);
    } finally {
      setCommitting(null);
      setTimeout(() => setCommitMessage(null), 8000);
    }
  };

  /** Full scan universe → training (INTEGRATION ScanPanel_UniverseButton) */
  const handleSendUniverseForTraining = async () => {
    if (!result) {
      setCommitMessage("Run a market scan first");
      return;
    }
    try {
      setCommitting("universe");
      setCommitMessage("Sending full stock universe for training…");
      const payload = buildUniversePayloadFromScan({
        all_results: (result as any).all_results || result.recommendations,
        recommendations: result.recommendations,
        universe: (result as any).universe,
      });
      if (!payload.symbols.length) {
        setCommitMessage("No symbols in scan universe");
        return;
      }
      const res = await sendStockUniverseForTraining(payload);
      if (res.ok) {
        setCommitMessage(
          `✅ ${res.ingested} symbols sent to training (kept ${res.retention_hours || 48}h)` +
            (res.training_triggered ? " · training triggered" : "")
        );
      } else {
        setCommitMessage(`❌ ${res.message || "Failed to send universe"}`);
      }
    } catch (e: any) {
      setCommitMessage(`❌ ${e?.message || "Failed to send universe for training"}`);
    } finally {
      setCommitting(null);
    }
  };


  const summarizeTradeResults = (results: { record_status: string; trade_status: string | null }[], dbNote?: string) => {
    const stored = results.filter((r) => r.record_status === "stored" || r.record_status === "updated").length;
    const already = results.filter((r) => r.record_status === "already_recorded").length;
    const opened = results.filter((r) => r.trade_status === "opened").length;
    const failed = results.filter((r) => r.trade_status && r.trade_status.startsWith("failed"));
    let msg = `📈 ${opened} trades opened · ${stored} new records · ${already} already recorded`;
    if (dbNote) msg += ` · ${dbNote}`;
    if (failed.length > 0) {
      const balanceFails = failed.filter((r) => (r.trade_status || "").toLowerCase().includes("balance") || (r.trade_status || "").toLowerCase().includes("not enough"));
      if (balanceFails.length > 0) {
        msg += ` · ${balanceFails.length} skipped (low balance)`;
        const needed = 10000 * balanceFails.length;
        setBalanceLow({ needed, available: 0 });
        // Enrich with live cash balance
        api.getPortfolioSummary()
          .then((s) => {
            setBalanceLow({
              needed,
              available: typeof s?.cash_balance === "number" ? s.cash_balance : 0,
            });
          })
          .catch(() => {});
      } else {
        msg += ` · ${failed.length} failed`;
      }
    }
    return msg;
  };

  /** Pre-check portfolio cash; show deposit popup if clearly short. */
  const ensureCashForTrades = async (count: number): Promise<boolean> => {
    const approxNeeded = Math.max(count, 1) * 10000;
    try {
      const s = await api.getPortfolioSummary();
      const available = typeof s?.cash_balance === "number" ? s.cash_balance : 0;
      // Need at least ~1x capital for first trade; warn if clearly insufficient
      if (available < 1000 || available < Math.min(approxNeeded * 0.3, 10000)) {
        setBalanceLow({ needed: approxNeeded, available });
        setCommitMessage(
          `Low balance: ₹${available.toLocaleString("en-IN")} available, ~₹${approxNeeded.toLocaleString("en-IN")} needed for ${count} trade(s).`
        );
        setTimeout(() => setCommitMessage(null), 8000);
        return false;
      }
    } catch {
      // If summary fails, still allow attempt — backend will enforce
    }
    return true;
  };

  /** Top 5 value-adjusted actionable → open paper trades + record for T+1/T+5 */
  const handleTop5ToTrade = async () => {
    if (top5Actionable.length === 0) {
      setCommitMessage("No top picks eligible for trade.");
      setTimeout(() => setCommitMessage(null), 4000);
      return;
    }
    setCommitting("trade_top5");
    setCommitMessage(null);
    setBalanceLow(null);
    try {
      const ok = await ensureCashForTrades(top5Actionable.length);
      if (!ok) {
        setCommitting(null);
        return;
      }
      const res = await api.commitActionableToTrade(top5Actionable.map(toActionablePick));
      setCommitMessage(summarizeTradeResults(res.results, (res as any).db_message));
    } catch (err) {
      console.error(err);
      setCommitMessage(`Failed: ${(err as Error).message || "unknown error"}`);
    } finally {
      setCommitting(null);
      setTimeout(() => setCommitMessage(null), 8000);
    }
  };

  /** All actionable → open paper trades + record for T+1/T+5 */
  const handleAllActionableToTrade = async () => {
    if (allActionable.length === 0) {
      setCommitMessage("No BUY NOW / PREPARE TO BUY picks in this scan.");
      setTimeout(() => setCommitMessage(null), 4000);
      return;
    }
    setCommitting("trade_all");
    setCommitMessage(null);
    setBalanceLow(null);
    try {
      const ok = await ensureCashForTrades(allActionable.length);
      if (!ok) {
        setCommitting(null);
        return;
      }
      const res = await api.commitActionableToTrade(allActionable.map(toActionablePick));
      setCommitMessage(summarizeTradeResults(res.results, (res as any).db_message));
    } catch (err) {
      console.error(err);
      setCommitMessage(`Failed: ${(err as Error).message || "unknown error"}`);
    } finally {
      setCommitting(null);
      setTimeout(() => setCommitMessage(null), 8000);
    }
  };

  return (
    <div className="scan-bento animate-fadeIn space-y-5">
      {/* ── Sticky header (responsive) ── */}
      <div className="sticky top-0 z-30 -mx-1 px-1 py-3 mb-1 bg-ink/95 backdrop-blur-md border-b border-slate/50 shadow-lg">
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
          {/* Left: Back + stats */}
          <div className="flex items-center gap-3 w-full sm:w-auto">
            <button
              onClick={onBack}
              className="font-mono text-xs text-mist hover:text-paper transition flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-graphite/40 hover:bg-graphite/60 border border-slate/40"
            >
              ← Back
            </button>
            <span className="font-mono text-xs text-mist/70">
              Scanned: <strong className="text-paper">{result.scanned}</strong>
              {" / "}
              Universe {result.universe_size}
              {(result as any).lite && (
                <span className="ml-2 text-amber-300/90 border border-amber-500/30 px-1.5 py-0.5 rounded text-[10px]">LITE</span>
              )}
              <span className="text-mist/40"> · ≤ ₹5000</span>
            </span>
          </div>

          {/* Right: action buttons */}
          <div className="flex flex-wrap items-center gap-2 w-full sm:w-auto">
            <button
              type="button"
              onClick={handleRefreshPrepareToBuy}
              disabled={isRefreshing}
              className="font-mono text-xs px-3 py-2 rounded-lg bg-amber-600/80 hover:bg-amber-500 text-white font-bold transition disabled:opacity-50 flex-1 sm:flex-none"
            >
              {isRefreshing ? "⏳ Syncing…" : "🔄 Refresh 'Prepare to Buy'"}
            </button>
            <button
              type="button"
              onClick={handleSearchBuys}
              disabled={!result || sniperLoading}
              className="font-mono text-xs bg-emerald-600 hover:bg-emerald-500 text-white font-bold rounded-lg px-4 py-2 transition disabled:opacity-50 flex items-center justify-center gap-2 shadow-md hover:shadow-emerald-900/40 animate-pulse flex-1 sm:flex-none"
            >
              {sniperLoading ? (
                <>
                  <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-white animate-spin"></span>
                  Sniping…
                </>
              ) : (
                "🎯 Search for Buy Stocks (1-4)"
              )}
            </button>
          </div>
        </div>
        {refreshMsg && (
          <p className="font-mono text-[10px] text-amber-200/90 mt-2 mb-0">{refreshMsg}</p>
        )}
      </div>

      {/* ── Bento: stats + filters + actions ── */}
      <div className="scan-bento-grid">
        <div className="scan-bento-card scan-bento-stats">
          <p className="dash-section-title mb-2">Scan summary</p>
          <div className="grid grid-cols-3 gap-2">
            <div className="scan-stat">
              <span className="scan-stat-label">Scanned</span>
              <span className="scan-stat-value">{result.scanned}</span>
            </div>
            <div className="scan-stat">
              <span className="scan-stat-label">Actionable</span>
              <span className="scan-stat-value text-signal-buy">{allActionable.length}</span>
            </div>
            <div className="scan-stat">
              <span className="scan-stat-label">Top board</span>
              <span className="scan-stat-value text-signal-prepare">{convictionBoard.length}</span>
            </div>
          </div>
          {quoteWs && <p className="mono text-[10px] text-signal-buy mt-2 mb-0">WS quotes live</p>}
        </div>

        <div className="scan-bento-card scan-bento-filters">
          <p className="dash-section-title mb-2">Filter</p>
          <div className="chip scanner-chips" role="tablist" aria-label="Filter by decision">
            {([
              ["all", "All"],
              ["buy", "BUY NOW"],
              ["prepare", "PREPARE"],
              ["avoid", "Avoid"],
            ] as const).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={filterChip === id}
                className={`scanner-chip${filterChip === id ? " is-active" : ""}`}
                data-active={filterChip === id ? "true" : "false"}
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setFilterChip(id);
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        <div className="scan-bento-card scan-bento-actions">
          <p className="dash-section-title mb-2">Actions</p>
          <div className="scan-action-row">
            <button onClick={handleSendTopPicks} disabled={!!isSendingTelegram || top5Actionable.length === 0} className="scan-action-btn">
              {isSendingTelegram === "top5" ? "Sending…" : "📤 Top 5"}
            </button>
            <button onClick={handleSendAllActionable} disabled={!!isSendingTelegram || allActionable.length === 0} className="scan-action-btn">
              {isSendingTelegram === "all" ? "Sending…" : "📤 All actionable"}
            </button>
            <button onClick={handleTop5ToTrade} disabled={!!committing || top5Actionable.length === 0} className="scan-action-btn scan-action-trade">
              {committing === "trade_top5" ? "Opening…" : `📈 Trade top (${top5Actionable.length})`}
            </button>
            <button onClick={handleAllActionableToTrade} disabled={!!committing || allActionable.length === 0} className="scan-action-btn scan-action-trade">
              {committing === "trade_all" ? "Opening…" : `📈 Trade all (${allActionable.length})`}
            </button>
            <button onClick={handleAddActionableToTraining} disabled={!!committing || allActionable.length === 0} className="scan-action-btn">
              {committing === "training" ? "Adding…" : "🎓 Train actionable"}
            </button>
            <button type="button" onClick={handleSendUniverseForTraining} disabled={!!committing || !result} className="scan-action-btn scan-action-violet" title="Send entire scan universe into training">
              {committing === "universe" ? "Sending…" : "🌌 Universe → train"}
            </button>
            <button
              onClick={() => onAddManyToWatchlist(top5Actionable.map((r) => r.symbol), "Top Picks")}
              disabled={top5Actionable.length === 0}
              className="scan-action-btn"
            >
              ⭐ Watchlist top
            </button>
            <button
              onClick={() => onAddManyToWatchlist(allActionable.map((d) => d.symbol), "All Actionable")}
              disabled={allActionable.length === 0}
              className="scan-action-btn"
            >
              ⭐ Watchlist all
            </button>
          </div>
        </div>
      </div>

      {commitMessage && (
        <div className="font-mono text-xs text-mist/70">{commitMessage}</div>
      )}
      {balanceLow && (
        <div className="balance-modal-overlay" role="dialog" aria-modal="true">
          <div className="balance-modal">
            <h3 className="mono text-sm text-amber-300 uppercase tracking-widest mb-2">Insufficient cash</h3>
            <p className="text-xs text-mist/90 mb-3">
              Some paper trades could not be opened. Deposit funds to continue.
            </p>
            <div className="balance-modal-grid mono text-xs mb-4">
              <div>
                <span className="text-mist">Available cash</span>
                <strong>₹{(balanceLow.available ?? 0).toLocaleString("en-IN")}</strong>
              </div>
              <div>
                <span className="text-mist">Approx. required</span>
                <strong>₹{(balanceLow.needed ?? 0).toLocaleString("en-IN")}</strong>
              </div>
              <div>
                <span className="text-mist">Shortfall</span>
                <strong className="text-amber-300">
                  ₹{Math.max(0, (balanceLow.needed ?? 0) - (balanceLow.available ?? 0)).toLocaleString("en-IN")}
                </strong>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="btn-terminal"
                onClick={() => {
                  const needed = Math.max(0, (balanceLow.needed ?? 0) - (balanceLow.available ?? 0));
                  setBalanceLow(null);
                  window.dispatchEvent(
                    new CustomEvent("stockky:goto-trades", {
                      detail: { openDeposit: true, suggestedAmount: needed || 10000 },
                    })
                  );
                }}
              >
                Add Balance / Open Trades
              </button>
              <button
                type="button"
                className="font-mono text-xs px-3 py-1.5 rounded-lg border border-amber-500/40 text-amber-200 hover:bg-amber-500/20"
                onClick={() => setBalanceLow(null)}
              >
                Dismiss
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Conviction board (single, deduped, value-aware) ── */}
      {convictionBoard.length === 0 ? (
        <div className="no-conviction scan-bento-card">
          <h2>NO HIGH-CONVICTION OPPORTUNITY — WAIT</h2>
          <p className="text-mist text-sm max-w-md mx-auto mb-0">
            {result.scanned} stocks scanned. None cleared the conviction bar today.
          </p>
          {(result.watchlist_candidates?.length ?? 0) > 0 && (
            <div className="mt-6">
              <p className="text-mist/60 text-sm mb-2">Worth watching:</p>
              <div className="flex flex-wrap justify-center gap-3">
                {uniqueBySymbol(result.watchlist_candidates || []).slice(0, 6).map((d) => (
                  <button
                    key={d.symbol}
                    onClick={() => onSelect(d.symbol)}
                    className="font-mono text-sm border border-slate/60 px-4 py-2 rounded-lg hover:border-mist/60 hover:text-paper transition"
                  >
                    {d.symbol} <span className="text-mist/50">({d.combined_score})</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      ) : (
        <section className="scan-conviction">
          <div className="flex flex-wrap items-end justify-between gap-2 mb-3">
            <div>
              <p className="dash-section-title mb-0">Conviction board</p>
              <p className="mono text-[11px] text-mist/60 mb-0">
                {result.verdict || "Ranked by value-adjusted conviction"} · ≤ ₹{PRICE_CAP} preferred
              </p>
            </div>
            <span className="mono text-[10px] text-mist/50">{convictionBoard.length} unique picks</span>
          </div>

          {/* Hero pick (rank 1) spans wider on large screens */}
          <div className="scan-conviction-grid grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 items-stretch">
            {convictionBoard.map((x, i) => {
              const r = x.decision;
              const isHero = i === 0;
              return (
                <div
                  key={r.symbol}
                  className={`scan-pick-wrap h-full${isHero ? " is-hero md:col-span-2 xl:col-span-1" : ""}`}
                >
                  <TopPick
                    rank={i + 1}
                    data={{
                      ...r,
                      close: resolveDisplayPrice(r, liveQuotes[r.symbol]?.price) || r.close,
                    }}
                    onSelect={onSelect}
                    onAddToWatchlist={handleAddToWatchlist}
                    addingWatchlist={addingWatchlist}
                  />
                  <div className="quality-gate-inline mono text-[10px] px-1 mt-1 flex flex-wrap gap-x-2 gap-y-0.5">
                    {x.bonus > 0 && (
                      <span className="text-mint">+{x.bonus} value bonus</span>
                    )}
                    {qualityLabel(r) && (
                      <span className={`qg-${qualityLabel(r).toLowerCase() === "high" ? "high" : qualityLabel(r).toLowerCase() === "medium" ? "med" : "low"}`}>
                        DATA {qualityLabel(r).toUpperCase()}
                      </span>
                    )}
                    {rowSummary(r) && (
                      <span className="text-mist/70">{rowSummary(r).slice(0, 100)}</span>
                    )}
                    {liveQuotes[r.symbol]?.price != null && (
                      <span className="text-signal-buy">₹{Number(liveQuotes[r.symbol].price).toLocaleString("en-IN")}</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      )}

      {/* ── Full results (unique symbols only) ── */}
      <section className="scan-table-section">
        <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
          <div className="font-mono text-[10px] text-mist uppercase tracking-widest">
            All results · {allSorted.length} unique
          </div>
          <span className="mono text-[10px] text-mist/40">
            Top board symbols marked · filter: {filterChip}
          </span>
        </div>
        <div className="rounded-xl border border-slate overflow-hidden scan-table-wrap">
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="border-b border-slate bg-graphite">
                <th className="text-left px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Symbol</th>
                <th className="text-left px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Decision</th>
                <th className="text-right px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Score</th>
                <th className="text-right px-4 py-3 text-[10px] text-mist uppercase tracking-widest">Price</th>
                <th className="text-right px-4 py-3 text-[10px] text-mist uppercase tracking-widest hidden md:table-cell">Technical</th>
                <th className="text-right px-4 py-3 text-[10px] text-mist uppercase tracking-widest hidden md:table-cell">Fundamental</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody>
              {allSorted.map((r) => {
                const style = decisionStyle[r.decision] || { color: "text-mist", border: "border-slate", bg: "bg-graphite/20" };
                const key = String(r.symbol || "").toUpperCase().replace(/\.NS$|\.BO$/i, "").trim();
                const onBoard = convictionSymbols.has(key);
                return (
                  <tr
                    key={key || r.symbol}
                    className={`border-b border-slate/40 hover:bg-graphite transition${onBoard ? " scan-row-onboard" : ""}`}
                  >
                    <td className="px-4 py-3 text-paper font-semibold">
                      {r.symbol}
                      {onBoard && <span className="ml-1.5 text-[9px] text-mint/80 uppercase tracking-wide">board</span>}
                    </td>
                    <td className={`px-4 py-3 text-xs ${style.color}`}>{r.decision}</td>
                    <td className="px-4 py-3 text-right text-mist">{r.combined_score}</td>
                    <td className="px-4 py-3 text-right text-paper">
                      {formatInrPrice(r, liveQuotes[r.symbol]?.price)}
                    </td>
                    <td className="px-4 py-3 text-right text-mist hidden md:table-cell">{r.technical_score}</td>
                    <td className="px-4 py-3 text-right text-mist hidden md:table-cell">{r.fundamental_score}</td>
                    <td className="px-4 py-3 text-right">
                      <button
                        onClick={() => onSelect(r.symbol)}
                        className="text-[10px] text-signal-prepare hover:text-paper transition uppercase tracking-wide"
                      >
                        View →
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {allSorted.length === 0 && (
            <p className="mono text-xs text-mist/50 p-6 text-center mb-0">No rows match this filter.</p>
          )}
        </div>
      </section>

      {result.errors.length > 0 && (
        <div className="rounded-xl border border-slate bg-graphite p-4">
          <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-2">Skipped ({result.errors.length})</div>
          <div className="flex flex-wrap gap-2">
            {result.errors.map((e) => (
              <span key={e.symbol} className="font-mono text-xs text-mist/50">{e.symbol}</span>
            ))}
          </div>
        </div>
      )}

      <BuySniperModal
        isOpen={sniperOpen}
        onClose={() => setSniperOpen(false)}
        suggestions={suggestions}
        loading={sniperLoading}
        error={sniperError}
        onSelectSymbol={onSelect}
      />
    </div>
  );
}

/** Horizon stamps for actionable picks (short / mid / long). Multiple allowed. */
function deriveHorizonStamps(data: Decision): { key: string; label: string }[] {
  const stamps: { key: string; label: string }[] = [];
  const labels: Record<string, string> = {
    short: "Short 3–21d",
    mid: "Mid 1–6m",
    long: "Long 6–24m",
  };
  const hz = ((data as any).horizons || {}) as Record<string, any>;
  const tech = Number(data.technical_score ?? 50);
  const fund = Number(data.fundamental_score ?? 50);
  const comb = Number(data.combined_score ?? 50);
  const dec = String(data.decision || "");
  const actionable = dec === "BUY NOW" || dec === "PREPARE TO BUY";
  if (!actionable) return stamps;

  const buyish = (h: any) => {
    if (!h || typeof h !== "object") return false;
    const d = String(h.decision || "");
    const sc = Number(h.score ?? 0);
    return d === "BUY NOW" || d === "PREPARE TO BUY" || sc >= 54;
  };

  for (const k of ["short", "mid", "long"] as const) {
    if (buyish(hz[k])) stamps.push({ key: k, label: labels[k] });
  }
  if (stamps.length) return stamps;

  // Synthetic from scores when backend omitted horizons (stream / lite path)
  if (tech >= 65 || comb >= 72) stamps.push({ key: "short", label: labels.short });
  if (comb >= 68 || (tech >= 55 && fund >= 55)) stamps.push({ key: "mid", label: labels.mid });
  if (fund >= 70 || (fund >= 60 && comb >= 65)) stamps.push({ key: "long", label: labels.long });
  if (!stamps.length) stamps.push({ key: "short", label: labels.short });
  // de-dupe by key
  const seen = new Set<string>();
  return stamps.filter((s) => (seen.has(s.key) ? false : (seen.add(s.key), true)));
}

function TopPick({
  rank,
  data,
  onSelect,
  onAddToWatchlist,
  addingWatchlist,
}: {
  rank: number;
  data: Decision;
  onSelect: (s: string) => void;
  onAddToWatchlist: (s: string) => void;
  addingWatchlist: string | null;
}) {
  const style = decisionStyle[data.decision] || decisionStyle["HOLD"];
  const upside =
    data.close != null && data.target != null
      ? (((data.target - data.close) / data.close) * 100).toFixed(1)
      : "N/A";
  const isAdding = addingWatchlist === data.symbol;
  const stamps = deriveHorizonStamps(data);

  return (
    <button
      onClick={() => onSelect(data.symbol)}
      className={`text-left rounded-xl border ${style.border} ${style.bg} p-5 sm:p-6 hover:brightness-110 transition group h-full min-h-[11.5rem] flex flex-col w-full`}
    >
      <div className="flex items-start justify-between mb-3 gap-2">
        <span className="font-mono text-[10px] text-mist/60 shrink-0">#{rank}</span>
        <span className="font-mono text-xs text-signal-buy shrink-0">+{upside}% target</span>
      </div>
      <div className="font-mono text-sm text-mist mb-1 truncate">{data.symbol}</div>
      <div className={`font-display text-xl sm:text-2xl ${style.color} mb-2 leading-tight`}>
        {data.decision}
      </div>
      {/* Horizon stamps — replace empty Top-5 horizon blocks */}
      {stamps.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-3">
          {stamps.map((s) => (
            <span
              key={s.key}
              className={
                s.key === "short"
                  ? "font-mono text-[9px] uppercase tracking-wide px-2 py-0.5 rounded-full border border-sky-400/40 bg-sky-500/15 text-sky-200"
                  : s.key === "mid"
                  ? "font-mono text-[9px] uppercase tracking-wide px-2 py-0.5 rounded-full border border-violet-400/40 bg-violet-500/15 text-violet-200"
                  : "font-mono text-[9px] uppercase tracking-wide px-2 py-0.5 rounded-full border border-amber-400/40 bg-amber-500/15 text-amber-200"
              }
            >
              {s.label}
            </span>
          ))}
        </div>
      )}
      <div className="flex justify-between font-mono text-xs text-mist mt-auto">
        <span>{formatInrPrice(data)}</span>
        <span>Score {data.combined_score}/100</span>
      </div>
      <div className="mt-3 pt-3 border-t border-slate/40 flex items-center justify-between gap-2">
        <span className="font-mono text-[10px] text-mist/50 group-hover:text-mist transition">
          View full analysis →
        </span>
        <span
          role="button"
          tabIndex={0}
          onClick={(e) => {
            e.stopPropagation();
            onAddToWatchlist(data.symbol);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.stopPropagation();
              onAddToWatchlist(data.symbol);
            }
          }}
          className={`text-[10px] font-mono transition border px-2 py-0.5 rounded flex items-center gap-1 cursor-pointer ${
            isAdding
              ? "bg-signal-buy/20 border-signal-buy text-signal-buy"
              : "text-signal-prepare hover:text-paper border-signal-prepare/30"
          }`}
        >
          {isAdding ? "Adding…" : "+ Watchlist"}
        </span>
      </div>
    </button>
  );
}

function CandidateCard({
  data,
  onSelect,
  onAddToWatchlist,
  addingWatchlist,
}: {
  data: Decision;
  onSelect: (s: string) => void;
  onAddToWatchlist: (s: string) => void;
  addingWatchlist: string | null;
}) {
  const style = decisionStyle[data.decision];
  const isAdding = addingWatchlist === data.symbol;

  return (
    <div
      onClick={() => onSelect(data.symbol)}
      className="border border-slate/60 bg-graphite/30 rounded-xl p-4 hover:border-mist/60 cursor-pointer transition-all duration-300 hover:shadow-glow-sm"
    >
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-sm text-paper">{data.symbol}</div>
          <div className={`font-display text-lg ${style.color}`}>{data.decision}</div>
        </div>
        <div className="text-right">
          <div className="text-sm font-mono text-paper">{formatInrPrice(data)}</div>
          <div className="text-xs text-mist/60">Score: {data.combined_score}</div>
        </div>
      </div>
      <button
        onClick={(e) => { e.stopPropagation(); onAddToWatchlist(data.symbol); }}
        disabled={isAdding}
        className={`mt-2 text-[10px] font-mono transition border px-2 py-0.5 rounded ${
          isAdding 
            ? "bg-signal-buy/20 border-signal-buy text-signal-buy" 
            : "text-signal-prepare hover:text-paper border-signal-prepare/30"
        }`}
      >
        {isAdding ? (
          <span className="flex items-center gap-1">
            <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-signal-buy animate-spin"></span>
            Adding...
          </span>
        ) : (
          "+ Watchlist"
        )}
      </button>
    </div>
  );
}

/**
 * Horizon Top-5 blocks removed from UI — stamps live on each PREPARE TO BUY /
 * BUY NOW card (see deriveHorizonStamps / TopPick). Export kept so App imports stay valid.
 */
export function MultiHorizonScanLists(_props: {
  data: any;
  onSendTelegram?: (list: any[], label: string) => void;
  onAddTraining?: (list: any[], label: string) => void;
}) {
  return null;
}
