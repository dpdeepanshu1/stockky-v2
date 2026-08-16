import { useState, useEffect } from "react";
import { Decision, api, TrainingScore, FundamentalMetrics, CategorizedEvents } from "../api";
import { decisionStyle } from "../decisionStyle";
import StockChart from "./StockChart";
import { toActionablePick } from "./ScanPanel";

// ── Types & helper for structured news (from v2) ──
interface NewsItem {
  title: string;
  publisher: string;
  published: string;
  url: string;
}

function getNewsItems(data: Decision): NewsItem[] {
  const items: NewsItem[] = [];

  if (data.event_data && typeof data.event_data === 'object') {
    const newsArray = data.event_data['news'] || data.event_data['recent_news'];
    if (Array.isArray(newsArray)) {
      for (const n of newsArray) {
        if (n.title) {
          items.push({
            title: n.title,
            publisher: n.publisher || '',
            published: n.published || '',
            url: n.url || '',
          });
        }
      }
    }
  }

  if (items.length === 0 && data.reasons.news) {
    for (const title of data.reasons.news) {
      items.push({ title, publisher: '', published: '', url: '' });
    }
  }

  return items;
}

// ── Sentiment scoring (from v2) ──
function computeNewsSentimentScore(newsItems: NewsItem[]): number {
  if (!newsItems.length) return 50;

  const positiveWords = new Set([
    'beat', 'surpass', 'growth', 'strong', 'record', 'outperform', 'positive',
    'upbeat', 'rally', 'surge', 'jump', 'gain', 'profit', 'upgrade', 'buy',
    'bullish', 'recovery', 'breakthrough', 'exceed', 'rose', 'higher',
    'earnings', 'revenue', 'income', 'profit', 'margin', 'expansion'
  ]);
  const negativeWords = new Set([
    'miss', 'decline', 'drop', 'fall', 'warning', 'cut', 'downgrade',
    'loss', 'sell', 'bearish', 'slump', 'plunge', 'collapse', 'debt',
    'default', 'investigation', 'lawsuit', 'bankruptcy', 'layoff',
    'deficit', 'deteriorate'
  ]);

  const positivePhrases = [
    'earnings call', 'strong revenue', 'revenue growth', 'profit beat',
    'outperform', 'record high', 'positive outlook'
  ];
  const negativePhrases = [
    'earnings miss', 'revenue miss', 'profit warning', 'downgrade',
    'debt warning', 'losses'
  ];

  let score = 50;
  let totalWeight = 0;

  for (const item of newsItems) {
    const title = item.title.toLowerCase();
    const words = title.split(/\s+/);
    let pos = 0, neg = 0;

    for (const w of words) {
      const clean = w.replace(/[^a-z]/g, '');
      if (positiveWords.has(clean)) pos++;
      if (negativeWords.has(clean)) neg++;
    }

    let phraseBonus = 0;
    for (const phrase of positivePhrases) {
      if (title.includes(phrase)) phraseBonus += 3;
    }
    for (const phrase of negativePhrases) {
      if (title.includes(phrase)) phraseBonus -= 3;
    }

    const weight = Math.min(1, words.length / 4);
    const impact = (pos - neg) * 4 + phraseBonus;
    score += impact * weight;
    totalWeight += weight;
  }

  const avgImpact = totalWeight > 0 ? (score - 50) / totalWeight : 0;
  const clampedAvg = Math.min(30, Math.max(-30, avgImpact));
  const finalScore = 50 + clampedAvg;
  return Math.min(100, Math.max(0, Math.round(finalScore)));
}

interface Props {
  data: Decision;
  onBack: () => void;
  onSearchRelated: (symbol: string) => void;
  onAddToWatchlist: (symbol: string) => void;
}

export default function DecisionCard({ data, onBack, onSearchRelated, onAddToWatchlist }: Props) {
  const style = decisionStyle[data.decision] ?? decisionStyle["DO NOT BUY"];
  const isBullish = data.decision === "BUY NOW" || data.decision === "PREPARE TO BUY";
  const [isAddingWatchlist, setIsAddingWatchlist] = useState(false);

  const [showTradeModal, setShowTradeModal] = useState(false);
  const [tradeCapital, setTradeCapital] = useState("10000");
  const [tradingInProgress, setTradingInProgress] = useState(false);
  const [tradeResult, setTradeResult] = useState<string | null>(null);

  const [trainingScore, setTrainingScore] = useState<TrainingScore | null>(null);
  const [loadingTrainingScore, setLoadingTrainingScore] = useState(false);

  // ── News items and scoring (from v2) ──
  const newsItems = getNewsItems(data);
  const sevenDaysAgo = new Date();
  sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7);
  const recentNewsItems = newsItems.filter(item => {
    if (!item.published) return false;
    return new Date(item.published) >= sevenDaysAgo;
  });

  const itemsForScoring = recentNewsItems.length > 0 ? recentNewsItems : newsItems;
  const computedNewsScore = itemsForScoring.length > 0 ? computeNewsSentimentScore(itemsForScoring) : data.news_score ?? 50;

  const sentimentLabel = computedNewsScore >= 70 ? 'Positive' :
                         computedNewsScore >= 50 ? 'Neutral' :
                         'Negative';
  const sentimentColor = computedNewsScore >= 70 ? 'text-green-500' :
                         computedNewsScore >= 50 ? 'text-yellow-500' :
                         'text-red-500';

  // ── Fetch training score ──
  useEffect(() => {
    let cancelled = false;
    setTrainingScore(null);
    setLoadingTrainingScore(true);
    api
      .getTrainingScore(data.symbol)
      .then((r) => { if (!cancelled) setTrainingScore(r); })
      .catch(() => { /* no history */ })
      .finally(() => { if (!cancelled) setLoadingTrainingScore(false); });
    return () => { cancelled = true; };
  }, [data.symbol]);

  // ── Scores breakdown (use computed news score) ──
  const scores = [
    { label: "Technical", value: data.technical_score },
    { label: "Fundamental", value: data.fundamental_score },
    { label: "News", value: computedNewsScore },
    ...(data.prediction_score !== null && data.prediction_score !== undefined ? [{ label: "AI Model", value: data.prediction_score }] : []),
    { label: "Market Sentiment", value: data.market_score ?? 50 },
    { label: "Training", value: data.training_score ?? 50 },
  ];

  const metrics = data.fundamental_metrics;
  const hasMetrics = metrics && Object.values(metrics).some(v => v != null);
  const hasPrice = data.close != null;

  const hasNews = data.reasons.news && data.reasons.news.length > 0 &&
    !(data.event_data && (data.event_data.news || data.event_data.recent_news));
  const hasEvent = data.reasons.event && data.reasons.event.length > 0;
  const hasMarket = data.reasons.market && data.reasons.market.length > 0;

  // Filter out fallback message from fundamental reasons (v2)
  const fundamentalReasons = data.reasons.fundamental.filter(
    item => !item.startsWith("Live data was temporarily unavailable")
  );

  const [expandedNewsIndex, setExpandedNewsIndex] = useState<number | null>(null);

  const handleAddToWatchlist = async () => {
    if (isAddingWatchlist) return;
    setIsAddingWatchlist(true);
    try {
      await onAddToWatchlist(data.symbol);
    } finally {
      setIsAddingWatchlist(false);
    }
  };

  const handleTradeThis = async () => {
    const capital = parseFloat(tradeCapital);
    if (!capital || capital <= 0) {
      setTradeResult("Enter a valid amount");
      return;
    }
    setTradingInProgress(true);
    setTradeResult(null);
    try {
      const { results } = await api.commitActionablePicks([toActionablePick(data)], capital, true);
      const r = results[0];
      if (r.trade_status === "opened") {
        setTradeResult(`✅ Trade opened (${r.trade_id}) — recorded to training too.`);
      } else if (r.trade_status === "already_open_or_closed") {
        setTradeResult(`Already have a position from today's pick (${r.trade_id}).`);
      } else {
        setTradeResult(`Could not open trade: ${r.trade_status}`);
      }
    } catch (err) {
      console.error(err);
      setTradeResult(`Failed to open trade: ${(err as Error).message || "unknown error"}`);
    } finally {
      setTradingInProgress(false);
    }
  };

  const formatAdjustment = (adj: number) => {
    if (adj === 0) return "±0";
    return adj > 0 ? `+${adj}` : `${adj}`;
  };

  // ── News impact helpers (v2) ──
  function getNewsImpact(title: string): 'positive' | 'negative' | 'neutral' {
    const lower = title.toLowerCase();
    if (/(beat|surpass|growth|strong|record|outperform|positive|upbeat|rally|surge|jump|gain|profit|upgrade|buy|bullish|recovery|exceed|rose|higher|earnings)/i.test(lower))
      return 'positive';
    if (/(miss|decline|drop|fall|warning|cut|downgrade|loss|sell|bearish|slump|plunge|collapse|debt|default|investigation|lawsuit|bankruptcy|layoff)/i.test(lower))
      return 'negative';
    return 'neutral';
  }

  function getImpactDescription(title: string): string {
    const impact = getNewsImpact(title);
    if (impact === 'positive') return '✅ Positive sentiment — likely to support price';
    if (impact === 'negative') return '⚠️ Negative sentiment — may pressure price';
    return '⚪ Neutral sentiment — no clear directional bias';
  }

  return (
    <div className="space-y-4">
      <button
        onClick={onBack}
        className="font-mono text-xs text-mist hover:text-paper transition flex items-center gap-1"
      >
        ← Back
      </button>

      {/* Decision header */}
      <div className={`rounded-2xl border ${style.border} ${style.bg} p-8`}>
        <div className="flex items-start justify-between gap-6 flex-wrap">
          <div>
            <div className="font-mono text-xs text-mist tracking-widest uppercase mb-3 flex items-center gap-2 flex-wrap">
              <span>{data.symbol}</span>
              <HorizonStrip data={data} />
              {data.sector && <><span className="text-slate">·</span><span>{data.sector}</span></>}
              {data.valuation && <><span className="text-slate">·</span><span className="text-mist/60">{data.valuation}</span></>}
            </div>
            <h2 className={`font-display text-5xl leading-none ${style.color} mb-2`}>
              {data.decision}
            </h2>
            <p className="text-mist text-sm">{style.verb} · {data.confidence} confidence</p>
          </div>

          <div className="text-right font-mono">
            <div className="text-4xl text-paper">
              {hasPrice ? `₹${data.close!.toLocaleString("en-IN")}` : data.data_insufficient ? "Awaiting Data" : "N/A"}
            </div>
            <div className="text-xs text-mist/60 mt-1 flex items-center justify-end gap-2">
              <span>Combined {data.combined_score}/100</span>
              {data.market_sentiment_adjustment !== undefined && data.market_sentiment_adjustment !== 0 && (
                <span className={`text-xs font-medium ${data.market_sentiment_adjustment > 0 ? 'text-signal-buy' : 'text-signal-sell'}`}>
                  ({formatAdjustment(data.market_sentiment_adjustment)})
                </span>
              )}
            </div>
          </div>
        </div>

        {data.event_risk && (
          <div className="mt-6 rounded-lg border border-signal-hold/40 bg-signal-hold/10 px-4 py-3 text-sm text-signal-hold font-mono flex items-start gap-2">
            <span>⚠</span>
            <span>{data.reasons.event?.[0] || "Upcoming corporate event — elevated near-term risk"}</span>
          </div>
        )}

        {isBullish && data.entry_range && hasPrice && (
          <div className="grid grid-cols-3 gap-4 mt-8 pt-6 border-t border-slate/40">
            <div>
              <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-1">Entry range</div>
              <div className="font-mono text-sm text-paper">
                ₹{data.entry_range.low?.toLocaleString("en-IN") ?? "N/A"} – 
                ₹{data.entry_range.high?.toLocaleString("en-IN") ?? "N/A"}
              </div>
            </div>
            <div>
              <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-1">Target</div>
              <div className="font-mono text-sm text-signal-buy">
                ₹{data.target?.toLocaleString("en-IN") ?? "N/A"}
              </div>
              {data.target != null && data.close != null && data.close !== 0 && (
                <div className="font-mono text-[10px] text-mist/50 mt-0.5">
                  +{(((data.target - data.close) / data.close) * 100).toFixed(1)}%
                </div>
              )}
            </div>
            <div>
              <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-1">Stop loss</div>
              <div className="font-mono text-sm text-signal-sell">
                ₹{data.stop_loss?.toLocaleString("en-IN") ?? "N/A"}
              </div>
              {data.stop_loss != null && data.close != null && data.close !== 0 && (
                <div className="font-mono text-[10px] text-mist/50 mt-0.5">
                  -{(((data.close - data.stop_loss) / data.close) * 100).toFixed(1)}%
                </div>
              )}
            </div>
          </div>
        )}

        <div className="mt-4 flex justify-end gap-2">
          {isBullish && (
            <button
              onClick={() => setShowTradeModal(true)}
              className="text-[10px] font-mono transition border px-3 py-1 rounded flex items-center gap-1 bg-emerald-500/15 border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/25"
            >
              💰 Trade This
            </button>
          )}
          <button
            onClick={handleAddToWatchlist}
            disabled={isAddingWatchlist}
            className={`text-[10px] font-mono transition border px-3 py-1 rounded flex items-center gap-1 ${
              isAddingWatchlist 
                ? "bg-signal-buy/20 border-signal-buy text-signal-buy" 
                : "text-signal-prepare hover:text-paper border-signal-prepare/30"
            }`}
          >
            {isAddingWatchlist ? (
              <>
                <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-signal-buy animate-spin"></span>
                Adding...
              </>
            ) : (
              "+ Watchlist"
            )}
          </button>
        </div>
      </div>

      {/* Trade modal */}
      {showTradeModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/70 backdrop-blur-sm p-4">
          <div className="bg-graphite border border-slate/60 rounded-2xl p-6 w-full max-w-sm">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-1">
              Trade {data.symbol}
            </h3>
            <p className="text-mist/50 text-xs mb-4">
              Opens a paper trade at ₹{data.close ?? "current price"} using dummy capital from your
              shared portfolio balance, and records this pick to training either way.
            </p>
            <div className="flex items-center gap-2 mb-4">
              <span className="font-mono text-lg text-mist">₹</span>
              <input
                type="number"
                value={tradeCapital}
                onChange={(e) => setTradeCapital(e.target.value)}
                className="flex-1 bg-ink/50 border border-slate/40 rounded-lg px-3 py-2 font-mono text-lg text-paper focus:outline-none focus:border-emerald-500/60"
                autoFocus
              />
            </div>
            {tradeResult && (
              <p className="text-xs font-mono text-mist/70 mb-4">{tradeResult}</p>
            )}
            <div className="flex gap-2">
              <button
                onClick={() => { setShowTradeModal(false); setTradeResult(null); }}
                className="flex-1 text-xs font-mono uppercase tracking-wider border border-slate/40 rounded-lg py-2 text-mist hover:text-paper transition"
              >
                {tradeResult ? "Close" : "Cancel"}
              </button>
              {!tradeResult && (
                <button
                  onClick={handleTradeThis}
                  disabled={tradingInProgress}
                  className="flex-1 text-xs font-mono uppercase tracking-wider bg-emerald-500/20 border border-emerald-500/50 text-emerald-400 rounded-lg py-2 hover:bg-emerald-500/30 transition disabled:opacity-50 flex items-center justify-center gap-2"
                >
                  {tradingInProgress && (
                    <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-emerald-400 animate-spin" />
                  )}
                  {tradingInProgress ? "Opening..." : "Confirm Trade"}
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      <StockChart symbol={data.symbol} />

      {/* Training score */}
      {loadingTrainingScore ? (
        <div className="rounded-xl border border-slate/40 bg-graphite/30 p-4 text-xs text-mist/40 font-mono flex items-center gap-2">
          <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-mist/40 animate-spin" />
          Checking model recommendation...
        </div>
      ) : trainingScore ? (
        <div className="rounded-xl border border-signal-prepare/30 bg-graphite p-5">
          <h3 className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">
            🤖 Model Recommendation
          </h3>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
            <MetricItem label="Training Score" value={`${trainingScore.training_score}`} />
            <MetricItem label="T+1 Success" value={`${trainingScore.t1_success_probability}%`} />
            <MetricItem label="T+5 Success" value={`${trainingScore.t5_success_probability}%`} />
            <MetricItem
              label="Model Confidence"
              value={trainingScore.model_success_probability == null ? "—" : `${trainingScore.model_success_probability}%`}
            />
          </div>
          {trainingScore.similar_setups && trainingScore.similar_setups.length > 0 && (
            <div className="text-xs text-mist/60">
              Based on {trainingScore.similar_setups.length} similar past setups in this system's own history.
            </div>
          )}
        </div>
      ) : null}

      {/* Price levels + Score breakdown */}
      <div className="grid grid-cols-2 gap-4">
        <div className="rounded-xl border border-slate bg-graphite p-5">
          <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">Price levels</div>
          {hasPrice && data.support != null && data.resistance != null ? (
            <PriceLevelBar close={data.close!} support={data.support} resistance={data.resistance} />
          ) : (
            <p className="text-sm text-mist/60 italic">
              {data.data_insufficient 
                ? `Insufficient price data for ${data.symbol} (newly listed stock). Please check back in 2-3 days after Yahoo Finance updates its database.` 
                : "Insufficient data for price levels"}
            </p>
          )}
        </div>
        <div className="rounded-xl border border-slate bg-graphite p-5">
          <div className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">Score breakdown</div>
          <div className="space-y-3">
            {scores.map((s) => (
              <ScoreBar key={s.label} label={s.label} value={s.value} />
            ))}
          </div>
        </div>
      </div>

      {/* Fundamental Metrics */}
      {metrics && (
        <div className="rounded-xl border border-slate/60 bg-graphite/30 p-5">
          <h3 className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">
            📊 Fundamental Metrics
          </h3>
          {data.fundamental_fallback ? (
            <p className="text-sm text-mist/60 italic">
              Live data temporarily unavailable — score is based on last known or default values.
            </p>
          ) : hasMetrics ? (
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
              {metrics.revenue_growth != null && (
                <MetricItem label="Revenue Growth" value={`${metrics.revenue_growth.toFixed(1)}%`} />
              )}
              {metrics.earnings_growth != null && (
                <MetricItem label="Earnings Growth" value={`${metrics.earnings_growth.toFixed(1)}%`} />
              )}
              {metrics.roe != null && (
                <MetricItem label="ROE" value={`${metrics.roe.toFixed(1)}%`} />
              )}
              {metrics.debt_to_equity != null && (
                <MetricItem label="Debt/Equity" value={`${metrics.debt_to_equity.toFixed(1)}`} />
              )}
              {metrics.free_cashflow != null && (
                <MetricItem label="Free Cash Flow" value={metrics.free_cashflow > 0 ? "Positive" : "Negative"} />
              )}
              {metrics.profit_margins != null && (
                <MetricItem label="Net Margin" value={`${metrics.profit_margins.toFixed(1)}%`} />
              )}
              {metrics.institutional_holding != null && (
                <MetricItem label="Institutional Holding" value={`${metrics.institutional_holding.toFixed(1)}%`} />
              )}
              {metrics.pe_ratio != null && (
                <MetricItem label="P/E Ratio" value={`${metrics.pe_ratio.toFixed(1)}`} />
              )}
              {metrics.forward_pe != null && (
                <MetricItem label="Forward P/E" value={`${metrics.forward_pe.toFixed(1)}`} />
              )}
            </div>
          ) : (
            <p className="text-sm text-mist/60 italic">
              No fundamental metrics available for this symbol. The score is based on limited available data.
            </p>
          )}
        </div>
      )}

      {/* Reasons – excluding News (handled separately below) */}
      <div className="grid md:grid-cols-2 gap-4">
        <ReasonList title="Technical" items={data.reasons.technical} />
        <ReasonList title="Fundamental" items={fundamentalReasons} maxItems={4} />
        {data.reasons.prediction && data.reasons.prediction.length > 0 && (
          <ReasonList title="AI Prediction" items={data.reasons.prediction} maxItems={4} />
        )}
        {hasEvent && <ReasonList title="Event Tracker" items={data.reasons.event!} />}
        {hasMarket && <ReasonList title="Market Sentiment" items={data.reasons.market!} />}
        {data.reasons.training && data.reasons.training.length > 0 && (
          <ReasonList title="Training Intelligence" items={data.reasons.training} maxItems={4} />
        )}
      </div>

      {/* Holding period */}
      {(data.holding_period !== "N/A" || data.holding_period_estimate) && (
        <div className="rounded-xl border border-slate bg-graphite px-5 py-4 font-mono text-xs text-mist">
          <div className="flex justify-between">
            <span className="uppercase tracking-widest">Suggested holding period</span>
            <span className="text-paper">{data.holding_period}</span>
          </div>
          {data.holding_period_estimate && (
            <div className="mt-2 pt-2 border-t border-slate/30 flex justify-between text-mist/70">
              <span className="uppercase tracking-widest text-[10px]">Estimated date range</span>
              <span className="text-paper">{data.holding_period_estimate.label}</span>
            </div>
          )}
        </div>
      )}

      {/* Long-term hold */}
      {data.long_term_hold && (
        <div className="rounded-xl border border-signal-buy/40 bg-signal-buy/5 px-5 py-4">
          <div className="flex items-center gap-2 font-mono text-xs uppercase tracking-widest text-signal-buy">
            💎 Highly Recommend for Long Term Hold
          </div>
          <p className="text-mist/60 text-xs mt-1">
            Strong fundamentals independent of current short-term entry timing.
          </p>
          {data.long_term_hold_estimate && (
            <div className="mt-2 pt-2 border-t border-signal-buy/20 flex justify-between text-xs">
              <span className="text-mist/50 uppercase tracking-widest text-[10px]">Suggested hold</span>
              <span className="text-paper font-mono">{data.long_term_hold_estimate.label}</span>
            </div>
          )}
        </div>
      )}

      {/* ── NEWS SECTION (v2) ── */}
      {(() => {
        if (recentNewsItems.length === 0) {
          return (
            <div className="rounded-xl border border-slate/60 bg-graphite/50 p-5">
              <div className="flex items-center justify-between mb-3">
                <h4 className="font-mono text-xs text-mist uppercase tracking-widest">
                  📰 News
                </h4>
                <span className="text-xs text-mist/40">No recent news (last 7 days)</span>
              </div>
              <p className="text-sm text-mist/60 italic">
                No news articles found for the last 7 days.
              </p>
            </div>
          );
        }

        const sortedRecent = [...recentNewsItems].sort((a, b) => {
          const da = a.published ? new Date(a.published).getTime() : 0;
          const db = b.published ? new Date(b.published).getTime() : 0;
          return db - da;
        });

        const recent = sortedRecent.slice(0, 1);
        const previous = sortedRecent.slice(1);

        return (
          <div className="rounded-xl border border-slate/60 bg-graphite/50 p-5">
            <div className="flex items-center justify-between mb-3">
              <h4 className="font-mono text-xs text-mist uppercase tracking-widest">
                📰 News
              </h4>
              <div className="flex items-center gap-2">
                <span className="font-mono text-[10px] text-mist/50">Score:</span>
                <span className={`font-mono text-sm font-medium ${sentimentColor}`}>
                  {computedNewsScore}
                </span>
                <span className={`text-[10px] font-medium ${sentimentColor}`}>
                  ({sentimentLabel})
                </span>
              </div>
            </div>

            {recent.length > 0 && (
              <div className="mb-4">
                <h5 className="text-sm font-medium text-green-600 dark:text-green-400 mb-1.5">
                  🔹 Recent Event
                </h5>
                <NewsItemWithFundamentals
                  item={recent[0]}
                  isExpanded={expandedNewsIndex === 0}
                  onToggle={() => setExpandedNewsIndex(expandedNewsIndex === 0 ? null : 0)}
                  fundamentalMetrics={data.fundamental_metrics}
                  fundamentalScore={data.fundamental_score}
                  impact={getNewsImpact(recent[0].title)}
                  impactDescription={getImpactDescription(recent[0].title)}
                />
              </div>
            )}

            {previous.length > 0 && (
              <div>
                <h5 className="text-sm font-medium text-blue-600 dark:text-blue-400 mb-1.5">
                  📄 Previous News (last 7 days)
                </h5>
                {previous.map((item, idx) => (
                  <NewsItemWithFundamentals
                    key={idx}
                    item={item}
                    isExpanded={expandedNewsIndex === idx + 1}
                    onToggle={() => setExpandedNewsIndex(expandedNewsIndex === idx + 1 ? null : idx + 1)}
                    fundamentalMetrics={data.fundamental_metrics}
                    fundamentalScore={data.fundamental_score}
                    impact={getNewsImpact(item.title)}
                    impactDescription={getImpactDescription(item.title)}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })()}

      {/* ── EVENT DATA VIEW (from event_data, v2) ── */}
      {data.event_data && Object.keys(data.event_data).length > 0 && (
        <div className="rounded-xl border border-slate/60 bg-graphite/50 p-5">
          <h4 className="font-mono text-xs text-mist uppercase tracking-widest mb-3">
            📅 Event Update
          </h4>
          <EventDataView data={data.event_data} />
        </div>
      )}

      {/* ── EVENT SECTION (fetched separately, v1) ── */}
      <EventSection symbol={data.symbol} />

      {/* Natural-language summary */}
      {data.natural_language_summary && (
        <div className="rounded-xl border border-slate/60 bg-graphite/50 p-5">
          <h4 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
            💬 Final Remarks
          </h4>
          <p className="text-sm text-paper/90 leading-relaxed">
            {data.natural_language_summary}
          </p>
        </div>
      )}
    </div>
  );
}

// ── EventSection (v1) ──
function EventSection({ symbol }: { symbol: string }) {
  const [events, setEvents] = useState<CategorizedEvents | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    api
      .getSymbolEvents(symbol)
      .then((r) => { if (!cancelled) setEvents(r); })
      .catch(() => { if (!cancelled) setError(true); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [symbol]);

  if (loading) {
    return (
      <div className="rounded-xl border border-slate/40 bg-graphite/30 p-4 text-xs text-mist/40 font-mono flex items-center gap-2">
        <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-mist/40 animate-spin" />
        Loading events...
      </div>
    );
  }
  if (error || !events) return null;
  const hasAnything = events.upcoming.length > 0 || events.recent.length > 0 || events.recent_changes.length > 0;
  if (!hasAnything) return null;

  return (
    <div className="rounded-xl border border-slate/60 bg-graphite/50 p-5">
      <h4 className="font-mono text-xs text-mist uppercase tracking-widest mb-3">📅 Event Update</h4>

      {events.recent_changes.length > 0 && (
        <div className="mb-4 bg-signal-prepare/10 border border-signal-prepare/30 rounded-lg p-3">
          <div className="font-mono text-[10px] text-signal-prepare uppercase tracking-wider mb-2">
            Newly Detected
          </div>
          <ul className="space-y-1.5">
            {events.recent_changes.map((c, i) => (
              <li key={i} className="text-sm text-paper flex gap-2">
                <span className="text-signal-prepare shrink-0">●</span>
                <span>{c}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {events.upcoming.length > 0 && (
        <div className="mb-3">
          <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider mb-2">Upcoming</div>
          <ul className="space-y-1.5">
            {events.upcoming.map((e, i) => (
              <li key={i} className="text-sm text-mist/80 flex gap-2">
                <span className="text-slate mt-1 shrink-0">–</span>
                <span>{e.description}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {events.recent.length > 0 && (
        <div>
          <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider mb-2">Previous</div>
          <ul className="space-y-1.5">
            {events.recent.slice(0, 5).map((e, i) => (
              <li key={i} className="text-sm text-mist/70 flex gap-2">
                <span className="text-slate mt-1 shrink-0">–</span>
                <span>{e.description}</span>
                {e.date && <span className="text-mist/30 text-xs ml-auto shrink-0">{e.date}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ── EventDataView (v2) ──
function EventDataView({ data }: { data: Record<string, unknown> }) {
  const skipKeys = new Set(['news', 'recent_news', 'symbol']);
  const entries = Object.entries(data).filter(([key]) => !skipKeys.has(key));

  if (entries.length === 0) {
    return <p className="text-sm text-mist/40 italic">No additional event data.</p>;
  }

  return (
    <div className="space-y-3">
      {entries.map(([key, value]) => {
        const label = key.replace(/_/g, " ").replace(/\b\w/g, l => l.toUpperCase());
        if (value == null) return null;

        if (Array.isArray(value)) {
          if (value.length === 0) return null;
          if (value.every(item => typeof item === 'string')) {
            return (
              <div key={key}>
                <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider mb-1">{label}</div>
                <ul className="space-y-1">
                  {value.map((item, i) => (
                    <li key={i} className="text-sm text-mist/80 flex gap-2 leading-relaxed">
                      <span className="text-slate mt-1 shrink-0">–</span>
                      <span>{item}</span>
                    </li>
                  ))}
                </ul>
              </div>
            );
          }
          if (value.every(item => typeof item === 'object' && item !== null)) {
            return (
              <div key={key}>
                <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider mb-1">{label}</div>
                <div className="space-y-2">
                  {value.map((item, idx) => {
                    const obj = item as Record<string, any>;
                    const title = obj.title || obj.name || obj.description || obj.event || 'Item';
                    const details = Object.entries(obj)
                      .filter(([k]) => !['title', 'name', 'description', 'event'].includes(k))
                      .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v}`)
                      .join(' · ');
                    return (
                      <div key={idx} className="border-b border-slate/30 pb-2 last:border-0">
                        <div className="text-sm text-paper font-medium">{title}</div>
                        {details && (
                          <div className="text-xs text-mist/60">{details}</div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          }
          return (
            <div key={key}>
              <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider mb-1">{label}</div>
              <ul className="space-y-1">
                {value.map((item, i) => (
                  <li key={i} className="text-sm text-mist/80 flex gap-2 leading-relaxed">
                    <span className="text-slate mt-1 shrink-0">–</span>
                    <span>{String(item)}</span>
                  </li>
                ))}
              </ul>
            </div>
          );
        }

        if (typeof value === 'object' && value !== null) {
          const obj = value as Record<string, unknown>;
          return (
            <div key={key}>
              <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider mb-1">{label}</div>
              <div className="bg-ink/30 rounded p-2 space-y-1">
                {Object.entries(obj).map(([k, v]) => (
                  <div key={k} className="flex justify-between text-sm">
                    <span className="text-mist/60 capitalize">{k.replace(/_/g, ' ')}</span>
                    <span className="text-paper">{String(v)}</span>
                  </div>
                ))}
              </div>
            </div>
          );
        }

        let displayValue = String(value);
        if (typeof value === 'boolean') displayValue = value ? 'Yes' : 'No';
        return (
          <div key={key} className="flex justify-between text-sm">
            <span className="text-mist/50 font-mono text-[10px] uppercase tracking-wider">{label}</span>
            <span className="text-paper">{displayValue}</span>
          </div>
        );
      })}
    </div>
  );
}

// ── NewsItemWithFundamentals (v2) ──
function NewsItemWithFundamentals({
  item,
  isExpanded,
  onToggle,
  fundamentalMetrics,
  fundamentalScore,
  impact,
  impactDescription,
}: {
  item: NewsItem;
  isExpanded: boolean;
  onToggle: () => void;
  fundamentalMetrics?: FundamentalMetrics;
  fundamentalScore?: number;
  impact: 'positive' | 'negative' | 'neutral';
  impactDescription: string;
}) {
  const date = item.published ? new Date(item.published) : null;
  const formattedDate = date
    ? date.toLocaleDateString('en-IN', {
        day: '2-digit',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        timeZone: 'UTC',
      }) + ' UTC'
    : 'Date not available';

  const impactColor = impact === 'positive' ? 'text-green-500' :
                      impact === 'negative' ? 'text-red-500' :
                      'text-yellow-500';
  const impactEmoji = impact === 'positive' ? '📈' :
                      impact === 'negative' ? '📉' :
                      '⚪';

  return (
    <div className="border-b border-gray-200 dark:border-gray-700 py-2 last:border-0">
      <div className="flex items-start gap-2">
        <span className="text-sm mt-0.5">{impactEmoji}</span>
        <div className="flex-1">
          {item.url ? (
            <a
              href={item.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-blue-600 dark:text-blue-400 hover:underline font-medium"
            >
              {item.title}
            </a>
          ) : (
            <span className="font-medium text-gray-800 dark:text-gray-200">{item.title}</span>
          )}
          <div className="flex items-center gap-3 mt-0.5 flex-wrap">
            <div className="text-xs text-gray-500 dark:text-gray-400">
              {item.publisher && <span className="mr-2">{item.publisher}</span>}
              <span>{formattedDate}</span>
            </div>
            <span className={`text-[10px] font-medium ${impactColor}`}>
              {impact.toUpperCase()} impact
            </span>
            <button
              onClick={onToggle}
              className="text-[10px] font-mono text-mist/50 hover:text-mist transition border border-slate/30 rounded px-2 py-0.5"
            >
              {isExpanded ? 'Hide Fundamentals' : 'Show Fundamentals'}
            </button>
          </div>
        </div>
      </div>

      {isExpanded && fundamentalMetrics && (
        <div className="mt-2 ml-6 p-3 bg-ink/30 rounded-lg border border-slate/40">
          <div className="flex items-center justify-between mb-2">
            <span className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">
              📊 Fundamental Impact
            </span>
            <span className="text-xs text-mist/60">
              Score: <span className="font-mono font-medium text-paper">{fundamentalScore ?? 'N/A'}</span>
            </span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
            {fundamentalMetrics.revenue_growth !== null && fundamentalMetrics.revenue_growth !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">Revenue Growth</div>
                <div className={`text-sm font-mono ${fundamentalMetrics.revenue_growth > 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {fundamentalMetrics.revenue_growth.toFixed(1)}%
                </div>
              </div>
            )}
            {fundamentalMetrics.earnings_growth !== null && fundamentalMetrics.earnings_growth !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">Earnings Growth</div>
                <div className={`text-sm font-mono ${fundamentalMetrics.earnings_growth > 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {fundamentalMetrics.earnings_growth.toFixed(1)}%
                </div>
              </div>
            )}
            {fundamentalMetrics.roe !== null && fundamentalMetrics.roe !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">ROE</div>
                <div className={`text-sm font-mono ${fundamentalMetrics.roe > 15 ? 'text-green-400' : fundamentalMetrics.roe > 0 ? 'text-yellow-400' : 'text-red-400'}`}>
                  {fundamentalMetrics.roe.toFixed(1)}%
                </div>
              </div>
            )}
            {fundamentalMetrics.debt_to_equity !== null && fundamentalMetrics.debt_to_equity !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">Debt/Equity</div>
                <div className={`text-sm font-mono ${fundamentalMetrics.debt_to_equity < 1 ? 'text-green-400' : fundamentalMetrics.debt_to_equity < 2 ? 'text-yellow-400' : 'text-red-400'}`}>
                  {fundamentalMetrics.debt_to_equity.toFixed(1)}
                </div>
              </div>
            )}
            {fundamentalMetrics.profit_margins !== null && fundamentalMetrics.profit_margins !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">Net Margin</div>
                <div className={`text-sm font-mono ${fundamentalMetrics.profit_margins > 10 ? 'text-green-400' : fundamentalMetrics.profit_margins > 0 ? 'text-yellow-400' : 'text-red-400'}`}>
                  {fundamentalMetrics.profit_margins.toFixed(1)}%
                </div>
              </div>
            )}
            {fundamentalMetrics.pe_ratio !== null && fundamentalMetrics.pe_ratio !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">P/E Ratio</div>
                <div className={`text-sm font-mono ${fundamentalMetrics.pe_ratio < 20 ? 'text-green-400' : fundamentalMetrics.pe_ratio < 30 ? 'text-yellow-400' : 'text-red-400'}`}>
                  {fundamentalMetrics.pe_ratio.toFixed(1)}
                </div>
              </div>
            )}
          </div>
          <div className="mt-2 text-xs text-mist/70 border-t border-slate/30 pt-2">
            {impactDescription}
          </div>
          <div className="mt-1 text-[10px] text-mist/40">
            💡 News sentiment computed from headlines — score reflects overall tone.
          </div>
        </div>
      )}
    </div>
  );
}

// ── Helper Components ──

function PriceLevelBar({ close, support, resistance }: { close: number; support: number; resistance: number }) {
  const range = resistance - support;
  const closePct = range > 0 ? ((close - support) / range) * 100 : 50;

  return (
    <div>
      <div className="relative h-2 rounded-full bg-slate overflow-visible mb-3">
        <div
          className="absolute h-full rounded-full bg-gradient-to-r from-signal-sell/30 to-signal-buy/30"
          style={{ width: "100%" }}
        />
        <div
          className="absolute top-1/2 -translate-y-1/2 w-3 h-3 rounded-full bg-paper border-2 border-signal-prepare shadow-lg"
          style={{ left: `${Math.min(95, Math.max(5, closePct))}%`, transform: "translate(-50%, -50%)" }}
        />
      </div>
      <div className="flex justify-between font-mono text-[10px]">
        <span className="text-signal-sell">S ₹{support.toLocaleString("en-IN")}</span>
        <span className="text-signal-prepare">₹{close.toLocaleString("en-IN")}</span>
        <span className="text-signal-buy">R ₹{resistance.toLocaleString("en-IN")}</span>
      </div>
    </div>
  );
}

function ScoreBar({ label, value }: { label: string; value: number }) {
  const color = value >= 70 ? "bg-signal-buy" : value >= 50 ? "bg-signal-prepare" : "bg-signal-sell/60";
  return (
    <div>
      <div className="flex justify-between font-mono text-[10px] text-mist mb-1">
        <span className="uppercase tracking-wide">{label}</span>
        <span className={value >= 70 ? "text-signal-buy" : value >= 50 ? "text-signal-prepare" : "text-signal-sell"}>{value}</span>
      </div>
      <div className="h-1 rounded-full bg-slate overflow-hidden">
        <div className={`h-full rounded-full ${color} transition-all duration-700`} style={{ width: `${value}%` }} />
      </div>
    </div>
  );
}

// ReasonList with maxItems and expand (v1 style) but also accepts no maxItems
function ReasonList({ title, items, maxItems }: { title: string; items: string[]; maxItems?: number }) {
  const [expanded, setExpanded] = useState(false);
  const hasItems = items && items.length > 0;
  const shouldTruncate = maxItems != null && items.length > maxItems && !expanded;
  const visibleItems = shouldTruncate ? items.slice(0, maxItems) : items;

  return (
    <div className="rounded-xl border border-slate bg-graphite p-5">
      <h3 className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">{title}</h3>
      {hasItems ? (
        <>
          <ul className="space-y-2">
            {visibleItems.map((item, i) => (
              <li key={i} className="text-sm text-mist/80 flex gap-2 leading-relaxed">
                <span className="text-slate mt-1 shrink-0">–</span>
                <span>{item}</span>
              </li>
            ))}
          </ul>
          {maxItems != null && items.length > maxItems && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="mt-2 text-[10px] font-mono uppercase tracking-wider text-mist/40 hover:text-mist/70 transition"
            >
              {expanded ? "Show less" : `+${items.length - maxItems} more`}
            </button>
          )}
        </>
      ) : (
        <p className="text-sm text-mist/40 italic">No specific {title.toLowerCase()} insights available</p>
      )}
    </div>
  );
}

function MetricItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-ink/40 border border-slate/40 rounded-lg px-3 py-2">
      <div className="font-mono text-[9px] text-mist/50 uppercase tracking-wider">{label}</div>
      <div className="font-mono text-sm text-paper mt-0.5">{value}</div>
    </div>
  );
}

// ── HorizonStrip (v2) ──
export function HorizonStrip({ data }: { data: any }) {
  const hz = data?.horizons || {};
  const order = ["short", "mid", "long"] as const;
  const fv = data?.final_verdict;
  return (
    <div className="mt-4 space-y-3">
      {fv && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
          <div className="font-semibold text-amber-300">Final Verdict (Short preferred)</div>
          <div className="text-slate-200 mt-1">{fv.summary || fv.headline}</div>
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {order.map((k) => {
          const h = hz[k];
          if (!h) return null;
          return (
            <div key={k} className="rounded-xl border border-slate-700 bg-slate-900/60 p-3">
              <div className="text-xs uppercase tracking-wide text-slate-400">{h.label || k}</div>
              <div className="text-lg font-bold text-white mt-1">{h.decision}</div>
              <div className="text-sm text-emerald-400">Score {h.score}</div>
              <div className="text-xs text-slate-400 mt-1">{h.holding_period}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}