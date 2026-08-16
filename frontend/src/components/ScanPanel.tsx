import { useState, useMemo } from "react";
import { ScanResult, Decision, api, ActionablePick } from "../api";
import { decisionStyle } from "../decisionStyle";

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
  const price = d.close;
  if (price == null || price <= 0) return { score: d.combined_score, eligible: true, bonus: 0 };
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
    training_score: d.training_score,
    event_risk: d.event_risk,
    holding_period: d.holding_period,
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

export default function ScanPanel({ result, onSelect, onBack, onAddToWatchlist, onAddManyToWatchlist, onSendTopPicks, onSendAllActionable }: Props) {
  const allSorted = [...result.all_results].sort((a, b) => b.combined_score - a.combined_score);

  const valueAdjustedTopPicks = useMemo(() => {
    return result.all_results
      .filter((d) => d.decision === "BUY NOW" || d.decision === "PREPARE TO BUY")
      .map((d) => ({ decision: d, ...valueAdjustedScore(d) }))
      .filter((x) => x.eligible)
      .sort((a, b) => b.score - a.score)
      .slice(0, 5);
  }, [result.all_results]);

  const allActionable = useMemo(
    () =>
      result.all_results
        .filter((d) => d.decision === "BUY NOW" || d.decision === "PREPARE TO BUY")
        .sort((a, b) => valueAdjustedScore(b).score - valueAdjustedScore(a).score),
    [result.all_results]
  );

  // Local loading states for animations
  const [isSendingTelegram, setIsSendingTelegram] = useState<"top5" | "all" | null>(null);
  const [addingWatchlist, setAddingWatchlist] = useState<string | null>(null);
  const [committingTraining, setCommittingTraining] = useState(false);
  const [commitMessage, setCommitMessage] = useState<string | null>(null);

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

  const handleAddActionableToTraining = async () => {
    if (allActionable.length === 0) {
      setCommitMessage("No BUY NOW / PREPARE TO BUY picks in this scan.");
      setTimeout(() => setCommitMessage(null), 4000);
      return;
    }
    setCommittingTraining(true);
    setCommitMessage(null);
    try {
      const { results } = await api.commitActionablePicks(allActionable.map(toActionablePick));
      const stored = results.filter((r) => r.record_status === "stored").length;
      const already = results.filter((r) => r.record_status === "already_recorded").length;
      const tradesOpened = results.filter((r) => r.trade_status === "opened").length;
      setCommitMessage(
        `${stored} new, ${already} already recorded · ${tradesOpened} trades opened`
      );
    } catch (err) {
      console.error(err);
      setCommitMessage(`Failed: ${(err as Error).message || "unknown error"}`);
    } finally {
      setCommittingTraining(false);
      setTimeout(() => setCommitMessage(null), 6000);
    }
  };

  return (
    <div className="space-y-6 animate-fadeIn">
      <div className="flex items-center justify-between">
        <button
          onClick={onBack}
          className="font-mono text-xs text-mist hover:text-paper transition flex items-center gap-1"
        >
          ← Back
        </button>
        <div className="text-right">
          <div className="font-mono text-xs text-mist/60">
            Scanned {result.scanned} stocks · {result.universe_size} in universe
          </div>
          <div className="font-mono text-xs text-mist/60">
            {result.market_stats.buy_signals} BUY · {result.market_stats.sell_signals} SELL · {result.market_stats.hold_signals} HOLD
          </div>
        </div>
      </div>

      {/* Action buttons */}
      <div className="flex flex-wrap gap-3">
        <button
          onClick={handleSendTopPicks}
          disabled={!!isSendingTelegram}
          className={`font-mono text-xs bg-signal-prepare/20 border border-signal-prepare/40 rounded-lg px-4 py-2 transition hover:bg-signal-prepare/30 disabled:opacity-50 flex items-center gap-2`}
        >
          {isSendingTelegram === "top5" ? (
            <>
              <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-signal-prepare animate-spin"></span>
              Sending Top 5...
            </>
          ) : (
            "📤 Send Top 5 Picks"
          )}
        </button>
        <button
          onClick={handleSendAllActionable}
          disabled={!!isSendingTelegram}
          className={`font-mono text-xs bg-signal-buy/20 border border-signal-buy/40 rounded-lg px-4 py-2 transition hover:bg-signal-buy/30 disabled:opacity-50 flex items-center gap-2`}
        >
          {isSendingTelegram === "all" ? (
            <>
              <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-signal-buy animate-spin"></span>
              Sending All...
            </>
          ) : (
            "📤 Send All Actionable"
          )}
        </button>
        <button
          onClick={handleAddActionableToTraining}
          disabled={committingTraining}
          className="font-mono text-xs bg-mint/20 border border-mint/40 rounded-lg px-4 py-2 transition hover:bg-mint/30 disabled:opacity-50 flex items-center gap-2"
        >
          {committingTraining ? (
            <>
              <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-mint animate-spin"></span>
              Adding...
            </>
          ) : (
            `🎓 Add All Actionable to Training (${allActionable.length})`
          )}
        </button>
        <button
          onClick={() =>
            onAddManyToWatchlist(result.recommendations.map((r) => r.symbol), "Top Picks")
          }
          disabled={result.recommendations.length === 0}
          className="font-mono text-xs bg-signal-prepare/15 border border-signal-prepare/40 text-signal-prepare rounded-lg px-4 py-2 transition hover:bg-signal-prepare/25 disabled:opacity-40"
        >
          ⭐ Add Top Picks to Watchlist ({result.recommendations.length})
        </button>
        <button
          onClick={() => onAddManyToWatchlist(allActionable.map((d) => d.symbol), "All Actionable")}
          disabled={allActionable.length === 0}
          className="font-mono text-xs bg-signal-prepare/15 border border-signal-prepare/40 text-signal-prepare rounded-lg px-4 py-2 transition hover:bg-signal-prepare/25 disabled:opacity-40"
        >
          ⭐ Add All Actionable to Watchlist ({allActionable.length})
        </button>
      </div>
      {commitMessage && (
        <div className="font-mono text-xs text-mist/70 -mt-3">{commitMessage}</div>
      )}

      {/* Verdict banner */}
      {result.recommendations.length === 0 ? (
        <div className="rounded-2xl border border-slate bg-graphite/50 p-10 text-center">
          <div className="font-display text-4xl text-signal-avoid mb-3">DO NOT BUY ANY STOCK TODAY</div>
          <p className="text-mist text-sm max-w-md mx-auto">
            {result.scanned} stocks scanned. None cleared the conviction bar today. Waiting is the decision.
          </p>
          {result.watchlist_candidates.length > 0 && (
            <div className="mt-6">
              <p className="text-mist/60 text-sm mb-2">But these are worth watching:</p>
              <div className="flex flex-wrap justify-center gap-3">
                {result.watchlist_candidates.slice(0, 5).map((d) => (
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
        <>
          <div className="font-mono text-xs text-mist/60 uppercase tracking-widest">{result.verdict}</div>
          <div className="grid md:grid-cols-3 gap-4">
            {result.recommendations.map((r, i) => (
              <TopPick
                key={r.symbol}
                rank={i + 1}
                data={r}
                onSelect={onSelect}
                onAddToWatchlist={handleAddToWatchlist}
                addingWatchlist={addingWatchlist}
              />
            ))}
          </div>
        </>
      )}

      {/* Value-adjusted Top Picks: client-side re-rank applying the Rs 2000
          cap + low-price/good-fundamentals bonus. Additive to the section
          above, not a replacement — result.recommendations above still
          reflects api-gateway's own ranking untouched, since Telegram
          sends and anything scheduler-service records still key off that,
          not this view. */}
      {valueAdjustedTopPicks.length > 0 && (
        <div>
          <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">
            💎 Value-Adjusted Top Picks (≤ ₹{PRICE_CAP}, bonus for good fundamentals at a low price)
          </div>
          <div className="grid md:grid-cols-3 gap-4">
            {valueAdjustedTopPicks.map((x, i) => (
              <div
                key={x.decision.symbol}
                onClick={() => onSelect(x.decision.symbol)}
                className="rounded-xl border border-mint/40 bg-mint/5 p-4 cursor-pointer hover:border-mint/70 transition"
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="font-mono text-xs text-mist/50">#{i + 1}</span>
                  <span className="font-mono text-xs text-mint">+{x.bonus} bonus</span>
                </div>
                <div className="font-display text-lg text-paper">{x.decision.symbol}</div>
                <div className="font-mono text-xs text-mist/60">
                  ₹{x.decision.close} · fundamentals {x.decision.fundamental_score}/100
                </div>
                <div className="font-mono text-xs text-mist/60 mt-1">
                  raw {x.decision.combined_score} → adjusted {Math.round(x.score * 10) / 10}
                </div>
              </div>
            ))}
          </div>
          <p className="text-mist/40 text-[11px] mt-2">
            Client-side ranking only — doesn't change what api-gateway recorded as
            "recommendations" for this scan, or what Send Top 5 sends.
          </p>
        </div>
      )}

      {/* Watchlist candidates (fallback) */}
      {result.recommendations.length === 0 && result.watchlist_candidates.length > 0 && (
        <div className="mt-6">
          <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">Watchlist Candidates</div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {result.watchlist_candidates.slice(0, 6).map((d) => (
              <CandidateCard 
                key={d.symbol} 
                data={d} 
                onSelect={onSelect} 
                onAddToWatchlist={handleAddToWatchlist}
                addingWatchlist={addingWatchlist}
              />
            ))}
          </div>
        </div>
      )}

      {/* Full results table */}
      <div>
        <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">All results</div>
        <div className="rounded-xl border border-slate overflow-hidden">
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
                const style = decisionStyle[r.decision];
                return (
                  <tr key={r.symbol} className="border-b border-slate/40 hover:bg-graphite transition">
                    <td className="px-4 py-3 text-paper font-semibold">{r.symbol}</td>
                    <td className={`px-4 py-3 text-xs ${style.color}`}>{r.decision}</td>
                    <td className="px-4 py-3 text-right text-mist">{r.combined_score}</td>
                    <td className="px-4 py-3 text-right text-paper">
                      {r.close != null ? `₹${r.close.toLocaleString("en-IN")}` : "N/A"}
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
        </div>
      </div>

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
    </div>
  );
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
  const style = decisionStyle[data.decision];
  const upside = data.close != null && data.target != null ? (((data.target - data.close) / data.close) * 100).toFixed(1) : "N/A";
  const isAdding = addingWatchlist === data.symbol;

  return (
    <button
      onClick={() => onSelect(data.symbol)}
      className={`text-left rounded-xl border ${style.border} ${style.bg} p-6 hover:brightness-110 transition group`}
    >
      <div className="flex items-start justify-between mb-4">
        <span className="font-mono text-[10px] text-mist/60">#{rank}</span>
        <span className="font-mono text-xs text-signal-buy">+{upside}% target</span>
      </div>
      <div className="font-mono text-sm text-mist mb-1">{data.symbol}</div>
      <div className={`font-display text-2xl ${style.color} mb-3`}>{data.decision}</div>
      <div className="flex justify-between font-mono text-xs text-mist">
        <span>{data.close != null ? `₹${data.close.toLocaleString("en-IN")}` : "N/A"}</span>
        <span>Score {data.combined_score}/100</span>
      </div>
      <div className="mt-3 pt-3 border-t border-slate/40 flex items-center justify-between">
        <span className="font-mono text-[10px] text-mist/50 group-hover:text-mist transition">
          View full analysis →
        </span>
        <button
          onClick={(e) => { e.stopPropagation(); onAddToWatchlist(data.symbol); }}
          disabled={isAdding}
          className={`text-[10px] font-mono transition border px-2 py-0.5 rounded flex items-center gap-1 ${
            isAdding 
              ? "bg-signal-buy/20 border-signal-buy text-signal-buy" 
              : "text-signal-prepare hover:text-paper border-signal-prepare/30"
          }`}
        >
          {isAdding ? (
            <>
              <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-signal-buy animate-spin"></span>
              Adding...
            </>
          ) : (
            "+ Watchlist"
          )}
        </button>
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
          <div className="text-sm font-mono text-paper">{data.close != null ? `₹${data.close.toLocaleString("en-IN")}` : "N/A"}</div>
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

/** Renders Short / Mid / Long Top lists when present on ScanResult */
export function MultiHorizonScanLists({ data, onSendTelegram, onAddTraining }: {
  data: any;
  onSendTelegram?: (list: any[], label: string) => void;
  onAddTraining?: (list: any[], label: string) => void;
}) {
  const blocks = [
    { key: "recommendations_short", title: "Top 5 · Short-term (3–21d)" },
    { key: "recommendations_mid", title: "Top 5 · Mid-term (1–6m)" },
    { key: "recommendations_long", title: "Top 5 · Long-term (6–24m)" },
  ];
  return (
    <div className="space-y-6">
      {data?.final_verdict && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
          <div className="font-semibold text-amber-300">Final Verdict</div>
          <p className="text-sm text-slate-200 mt-1">{data.final_verdict.headline || data.final_verdict.summary}</p>
        </div>
      )}
      {blocks.map((b) => {
        const list = data?.[b.key] || [];
        return (
          <div key={b.key} className="rounded-xl border border-slate-700 p-4">
            <div className="flex items-center justify-between gap-2 mb-3">
              <h3 className="font-semibold text-white">{b.title}</h3>
              <div className="flex gap-2">
                {onSendTelegram && (
                  <button className="text-xs px-2 py-1 rounded bg-sky-600 text-white" onClick={() => onSendTelegram(list, b.title)}>Send to Telegram</button>
                )}
                {onAddTraining && (
                  <button className="text-xs px-2 py-1 rounded bg-violet-600 text-white" onClick={() => onAddTraining(list, b.title)}>Add to Training</button>
                )}
              </div>
            </div>
            {list.length === 0 ? (
              <p className="text-sm text-slate-400">No picks in this horizon.</p>
            ) : (
              <ul className="space-y-2">
                {list.map((r: any, i: number) => (
                  <li key={r.symbol || i} className="flex justify-between text-sm border-b border-slate-800 py-2">
                    <span className="font-medium text-white">{r.symbol}</span>
                    <span className="text-emerald-400">{r.decision || r.horizons?.short?.decision}</span>
                    <span className="text-slate-300">{r.combined_score ?? r._hz_score}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}
