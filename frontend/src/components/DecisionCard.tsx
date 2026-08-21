import { useState, useEffect } from "react";
import { Decision, api, TrainingScore, FundamentalMetrics, CategorizedEvents } from "../api";
import { decisionStyle } from "../decisionStyle";
import StockChart from "./StockChart";
import { useStockkyRealtime } from "../useRealtime";
import { toActionablePick } from "./ScanPanel";
import { resolveDisplayPrice, formatInrPrice } from "../priceDisplay";

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

function formatWhen(iso?: string | null): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    }) + " IST";
  } catch {
    return String(iso);
  }
}

export default function DecisionCard({ data, onBack, onSearchRelated, onAddToWatchlist }: Props) {
  const style = decisionStyle[data.decision] ?? decisionStyle["DO NOT BUY"];
  const isBullish = data.decision === "BUY NOW" || data.decision === "PREPARE TO BUY";
  const [isAddingWatchlist, setIsAddingWatchlist] = useState(false);

  const [showTradeModal, setShowTradeModal] = useState(false);
  const [calling, setCalling] = useState(false);
  const [liveQuote, setLiveQuote] = useState<{ price?: number; as_of?: string } | null>(null);

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

  // Live quotes via WebSocket (HTTP fallback every 45s)
  const { connected: quoteWs, subscribeQuotes, unsubscribeQuotes, quotes: wsQuotes } = useStockkyRealtime();
  useEffect(() => {
    const sym = data.symbol.toUpperCase().replace(/\.NS$/i, "").replace(/\.BO$/i, "");
    subscribeQuotes([sym]);
    return () => unsubscribeQuotes([sym]);
  }, [data.symbol, subscribeQuotes, unsubscribeQuotes]);
  useEffect(() => {
    const sym = data.symbol.toUpperCase().replace(/\.NS$/i, "").replace(/\.BO$/i, "");
    const q = wsQuotes[sym];
    if (q?.price != null) {
      setLiveQuote({ price: q.price, as_of: q.as_of || new Date().toISOString() });
    }
  }, [wsQuotes, data.symbol]);
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (quoteWs) return; // WS live
      try {
        const q = await api.getQuote(data.symbol);
        if (!cancelled && q && (q.price != null || q.close != null)) {
          setLiveQuote({ price: q.price ?? q.close, as_of: q.as_of || new Date().toISOString() });
        }
      } catch { /* optional */ }
    };
    tick();
    const id = window.setInterval(tick, 45000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [data.symbol, quoteWs]);


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
  const displayClose = resolveDisplayPrice(data, liveQuote?.price);
  const hasPrice = displayClose > 0;

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

  const newsHeadlineCount = (data as any).news_data?.headline_count ?? newsItems.length;
  const topHeadlines = ((data as any).news_data?.headlines || newsItems || []).slice(0, 3);

  return (
    <div className="space-y-4 stock-detail-terminal">
      <button
        onClick={onBack}
        className="font-mono text-xs text-mist hover:text-paper transition flex items-center gap-1"
      >
        ← Back
      </button>
      {liveQuote?.price != null && (
        <p className="mono text-[10px] text-mist/60">
          Live quote ₹{liveQuote.price.toLocaleString("en-IN")} · {quoteWs ? "WS live" : "poll"} ·{" "}
          {liveQuote.as_of ? new Date(liveQuote.as_of).toLocaleTimeString("en-IN") : ""}
        </p>
      )}

      {/* Decision header — compact terminal */}
      <div className={`dc-hero rounded-2xl border ${style.border} ${style.bg} p-4 sm:p-6`}>
        <div className="dc-grid">
          <div className="dc-col-main">
            <div className="dc-sym-row">
              <span className="font-display text-xl sm:text-2xl font-extrabold tracking-wide text-white">
                {data.symbol}
              </span>
              {data.sector && (
                <span className="font-mono text-[10px] text-mist/70 uppercase tracking-wider">{data.sector}</span>
              )}
              {data.valuation && (
                <span className="font-mono text-[10px] text-mist/50">· {data.valuation}</span>
              )}
            </div>
            <h2 className={`font-display text-2xl sm:text-4xl leading-none ${style.color} mt-2 tracking-tight`}>
              {data.decision}
            </h2>
            <p className="text-mist text-xs sm:text-sm mt-1.5">{style.verb} · {data.confidence} confidence</p>
            {data.data_quality && (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <span
                  className={`font-mono text-[10px] uppercase tracking-wider px-2 py-0.5 rounded border ${
                    data.data_quality.level === "high"
                      ? "border-emerald-500/40 text-emerald-300 bg-emerald-500/10"
                      : data.data_quality.level === "low"
                      ? "border-amber-500/40 text-amber-200 bg-amber-500/10"
                      : "border-slate-400/40 text-mist bg-slate/20"
                  }`}
                >
                  Data quality: {data.data_quality.level || "unknown"}
                </span>
                {data.data_quality.note && (
                  <span className="font-mono text-[10px] text-mist/65">{data.data_quality.note}</span>
                )}
              </div>
            )}
            {data.data_quality?.flags && data.data_quality.flags.length > 0 && (
              <ul className="mt-1 font-mono text-[10px] text-mist/55 list-disc list-inside">
                {data.data_quality.flags.slice(0, 3).map((f, i) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
            )}
          </div>

          <div className="dc-col-side">
            <div className="dc-price-block font-mono">
              <div className="text-2xl sm:text-3xl text-paper tabular-nums">
                {hasPrice ? `₹${displayClose.toLocaleString("en-IN")}` : data.data_insufficient ? "Awaiting Data" : "Syncing…"}
              </div>
              <div className="text-[11px] text-mist/65 mt-0.5 flex items-center justify-end gap-2 flex-wrap">
                <span>Combined {data.combined_score}/100</span>
                {data.market_sentiment_adjustment !== undefined && data.market_sentiment_adjustment !== 0 && (
                  <span className={`font-medium ${data.market_sentiment_adjustment > 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                    ({formatAdjustment(data.market_sentiment_adjustment)})
                  </span>
                )}
              </div>
            </div>
            <HorizonStrip data={data} />
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
              className="text-[10px] font-mono transition border px-3 py-1.5 rounded-lg flex items-center gap-1 bg-emerald-500/15 border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/25"
            >
              💰 Trade This
            </button>
          )}
          {(data.decision === "BUY NOW" || data.decision === "PREPARE TO BUY") && (
            <button
              disabled={calling}
              onClick={async () => {
                setCalling(true);
                try {
                  const reason =
                    data.natural_language_summary ||
                    `${data.decision}, score ${data.combined_score}. Entry ${data.entry_range?.low ?? "—"} to ${data.entry_range?.high ?? "—"}, target ${data.target ?? "—"}, stop ${data.stop_loss ?? "—"}.`;
                  const msg = `${data.symbol} ${data.decision}. ${String(reason).replace(/\s+/g, " ").slice(0, 150)}`;
                  const r = await api.testCallMeBot(msg);
                  if (r.ok) {
                    window.alert(`Call Alert sent for ${data.symbol}.\n\n${msg}`);
                  } else {
                    window.alert(`Call Alert failed: ${r.result || r.error || "Check CallMeBot settings in Notifications"}`);
                  }
                } catch (e) {
                  window.alert(`Call Alert failed: ${(e as Error).message || "unknown error"}`);
                } finally {
                  setCalling(false);
                }
              }}
              className="text-[10px] font-mono transition border px-3 py-1.5 rounded-lg flex items-center gap-1 bg-amber-500/15 border-amber-500/40 text-amber-300 hover:bg-amber-500/25 disabled:opacity-50"
            >
              {calling ? "Calling…" : "📞 Call Alert"}
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
      ) : trainingScore && (trainingScore as any).available !== false ? (
        <div className="rounded-xl border border-signal-prepare/30 bg-graphite p-5">
          <h3 className="font-mono text-[10px] text-mist uppercase tracking-widest mb-3">
            🤖 Model Recommendation
          </h3>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
            <MetricItem label="Training Score" value={trainingScore.training_score != null ? `${trainingScore.training_score}` : "—"} />
            <MetricItem label="T+1 Success" value={trainingScore.t1_success_probability != null ? `${trainingScore.t1_success_probability}%` : "—"} />
            <MetricItem label="T+5 Success" value={trainingScore.t5_success_probability != null ? `${trainingScore.t5_success_probability}%` : "—"} />
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
          {displayClose != null && data.support != null && data.resistance != null ? (
            <PriceLevelBar close={displayClose} support={data.support} resistance={data.resistance} />
          ) : (
            <p className="text-sm text-mist/60 italic">
              {displayClose == null && data.data_insufficient
                ? `Price feed temporarily unavailable for ${data.symbol}. Chart may still load from market-data; retry Analyse shortly.` 
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
                <MetricItem label="Debt/Equity" value={`${Number(metrics.debt_to_equity).toFixed(2)}x`} />
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
        <ReasonList
          title="Technical"
          items={
            (data.reasons?.technical && data.reasons.technical.length > 0)
              ? data.reasons.technical
              : [
                  (data as any).technical?.summary
                    || (data as any).technical_summary
                    || `Technical indicators neutral (RSI: ${(data as any).technical?.rsi ?? (data as any).rsi ?? 50})`,
                ]
          }
        />
        <ReasonList title="Fundamental" items={fundamentalReasons} maxItems={4} />
        {(() => {
          const raw = (data.reasons?.prediction || []).map(String).filter(Boolean);
          const cleaned = raw
            .map((s) => s.replace(/^[-–•\s]+/, "").trim())
            .filter((s) => s.length > 3 && !/^(price|n\/?a|null|undefined)$/i.test(s));
          const score = data.prediction_score;
          let line: string | null = cleaned[0] || null;
          if (!line && score != null) {
            if (score >= 60) {
              line = `Model estimates about ${score}% odds of a meaningful upside move (~5%+) over the next ~10 trading sessions.`;
            } else if (score >= 45) {
              line = `Model sees mixed odds (~${score}%) of a ~5%+ move within ~10 sessions — no clear edge.`;
            } else {
              line = `Model assigns only ~${score}% probability of a ~5%+ move within ~10 sessions — weak short-term signal.`;
            }
          }
          if (!line && (data as any).prediction_note) {
            line = String((data as any).prediction_note);
          }
          if (!line) {
            line = "AI prediction is limited right now; treat technical and fundamental pillars as primary.";
          }
          const items = cleaned.length > 0 ? [line, ...cleaned.slice(1, 3)] : [line];
          return <ReasonList title="AI Prediction" items={items} maxItems={4} />;
        })()}
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

      {/* ── Single News panel (to the point) ── */}
      <section className="terminal-panel">
        <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
          <p className="dash-section-title mb-0">News</p>
          <span className={`font-mono text-[11px] ${sentimentColor}`}>
            Score {computedNewsScore} · {sentimentLabel}
            {newsHeadlineCount ? ` · ${newsHeadlineCount} items` : ""}
          </span>
        </div>
        <p className="text-sm text-paper/90 leading-relaxed mb-2">
          {(data as any).news_data?.summary
            || (data as any).news_summary
            || (Array.isArray(data.reasons?.news) && data.reasons.news[0])
            || (recentNewsItems[0]?.title
              ? `Latest: ${recentNewsItems[0].title}`
              : "No material news in the last 7 days.")}
        </p>
        {topHeadlines.length > 0 && (
          <ul className="space-y-1.5 mb-2">
            {topHeadlines.slice(0, 3).map((h: any, i: number) => {
              const title = typeof h === "string" ? h : h.title || h.headline || "";
              const url = typeof h === "object" ? h.url : undefined;
              if (!title) return null;
              return (
                <li key={i} className="font-mono text-[11px] text-mist/85 leading-snug">
                  {url ? (
                    <a href={url} target="_blank" rel="noreferrer" className="text-sky-400 hover:text-sky-300 underline-offset-2 hover:underline">
                      {title.length > 110 ? title.slice(0, 109) + "…" : title}
                    </a>
                  ) : (
                    <span>{title.length > 110 ? title.slice(0, 109) + "…" : title}</span>
                  )}
                  {formatWhen(typeof h === "object" ? (h.published || h.published_at || h.date || h.datetime) : undefined) && (
                    <span className="block text-[10px] text-mist/45 mt-0.5">
                      {formatWhen(typeof h === "object" ? (h.published || h.published_at || h.date || h.datetime) : undefined)}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {recentNewsItems.length > 3 && (
          <button
            type="button"
            className="btn-terminal text-[10px]"
            onClick={() => setExpandedNewsIndex(expandedNewsIndex === -1 ? null : -1)}
          >
            {expandedNewsIndex === -1 ? "Hide full news" : "Full news — click here"}
          </button>
        )}
        {expandedNewsIndex === -1 && recentNewsItems.length > 3 && (
          <ul className="mt-2 space-y-1.5 border-t border-slate/40 pt-2">
            {recentNewsItems.slice(3, 12).map((item, idx) => (
              <li key={idx} className="font-mono text-[11px] text-mist/80">
                {item.url ? (
                  <a href={item.url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">
                    {item.title}
                  </a>
                ) : (
                  item.title
                )}
                <span className="block text-[10px] text-mist/45 mt-0.5">
                  {[item.publisher, formatWhen((item as any).published || (item as any).published_at)].filter(Boolean).join(" · ")}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* ── Single Event panel ── */}
      <section className="terminal-panel">
        <p className="dash-section-title">Events</p>
        <p className="text-sm text-paper/90 leading-relaxed mb-1">
          {(data as any).event_data?.summary
            || (data as any).event_summary
            || (Array.isArray(data.reasons?.event) && data.reasons.event[0])
            || (data.event_risk
              ? "Elevated event risk near earnings or corporate action."
              : "No major corporate events detected in the recent window.")}
        </p>
        {(data as any).event_data?.next_earnings_date && (
          <p className="font-mono text-[11px] text-amber-300/90">
            Next earnings: {(data as any).event_data.next_earnings_date}
          </p>
        )}
        <EventScoreBadge data={data} />
        <EventSection symbol={data.symbol} compact />
      </section>

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
function EventSection({ symbol, compact }: { symbol: string; compact?: boolean }) {
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
    if (compact) return null;
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
    <div className={compact ? "mt-2" : "rounded-xl border border-slate/60 bg-graphite/50 p-5"}>
      {!compact && (
        <h4 className="font-mono text-xs text-mist uppercase tracking-widest mb-3">📅 Event Update</h4>
      )}

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
                <span>
                  {e.description}
                  {(e.date || (e as any).datetime) && (
                    <span className="block font-mono text-[10px] text-mist/45 mt-0.5">
                      {formatWhen(e.date || (e as any).datetime)}
                    </span>
                  )}
                </span>
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
                <span>
                  {e.description}
                  {(e.date || (e as any).datetime) && (
                    <span className="block font-mono text-[10px] text-mist/45 mt-0.5">
                      {formatWhen(e.date || (e as any).datetime)}
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ── EventDataView (v2) — clean, readable event cards ──
function EventDataView({ data }: { data: Record<string, unknown> }) {
  const skipKeys = new Set([
    "news",
    "recent_news",
    "symbol",
    "raw",
    "raw_response",
    "debug",
  ]);

  const formatVal = (v: unknown): string => {
    if (v == null) return "";
    if (typeof v === "boolean") return v ? "Yes" : "No";
    if (typeof v === "number") return String(v);
    if (typeof v === "string") {
      const s = v.trim();
      // ISO-ish timestamps
      if (/^\d{4}-\d{2}-\d{2}T/.test(s)) {
        try {
          return new Date(s).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
        } catch {
          return s;
        }
      }
      // Hide mega Google News redirect URLs in plain text dumps
      if (s.length > 160 && (s.includes("http") || s.includes("news.google"))) {
        return s.slice(0, 120) + "…";
      }
      return s;
    }
    if (Array.isArray(v)) return v.map(formatVal).filter(Boolean).join("; ");
    if (typeof v === "object") {
      const o = v as Record<string, unknown>;
      const title = o.title || o.name || o.description || o.headline;
      if (typeof title === "string" && title.trim()) return title.trim();
      try {
        return JSON.stringify(o);
      } catch {
        return String(v);
      }
    }
    return String(v);
  };

  const shortUrl = (url: string) => {
    try {
      const u = new URL(url);
      return u.hostname.replace(/^www\./, "");
    } catch {
      return "link";
    }
  };

  const renderNewsish = (items: unknown[], key: string) => {
    if (!items.length) return null;
    return (
      <div key={key} className="space-y-2">
        <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">
          {key.replace(/_/g, " ")}
        </div>
        <ul className="space-y-2">
          {items.slice(0, 8).map((raw, idx) => {
            if (typeof raw === "string") {
              // Parse "Title - publisher · published: … · url: …"
              let title = raw;
              let meta = "";
              const urlMatch = raw.match(/https?:\/\/\S+/);
              const url = urlMatch ? urlMatch[0].replace(/[),.;]+$/, "") : "";
              title = raw
                .replace(/\s*[·•]\s*url:\s*https?:\/\/\S+/gi, "")
                .replace(/\s*url:\s*https?:\/\/\S+/gi, "")
                .replace(/\s*https?:\/\/\S+/g, "")
                .trim();
              const pubMatch = title.match(/\b(?:publisher|published):\s*([^·•]+)/i);
              if (pubMatch) meta = pubMatch[1].trim();
              title = title
                .replace(/\bpublisher:\s*[^·•]+/gi, "")
                .replace(/\bpublished:\s*[^·•]+/gi, "")
                .replace(/\s*[·•]\s*/g, " · ")
                .replace(/\s{2,}/g, " ")
                .trim();
              return (
                <li
                  key={idx}
                  className="rounded-lg border border-slate/40 bg-ink/40 px-3 py-2 text-sm"
                >
                  <div className="text-paper leading-snug font-medium">{title || "Event"}</div>
                  <div className="mt-1 flex flex-wrap gap-2 text-[10px] font-mono text-mist/50">
                    {meta && <span>{meta}</span>}
                    {url && (
                      <a
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-sky-400 hover:underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {shortUrl(url)} ↗
                      </a>
                    )}
                  </div>
                </li>
              );
            }
            if (raw && typeof raw === "object") {
              const o = raw as Record<string, any>;
              const title = String(o.title || o.name || o.description || o.event || "Item");
              const publisher = o.publisher || o.source || "";
              const published = o.published || o.published_at || o.date || o.datetime || "";
              const url = typeof o.url === "string" ? o.url : "";
              let pubLabel = "";
              if (published) {
                try {
                  pubLabel = new Date(String(published)).toLocaleString("en-IN", {
                    dateStyle: "medium",
                    timeStyle: "short",
                  });
                } catch {
                  pubLabel = String(published);
                }
              }
              return (
                <li
                  key={idx}
                  className="rounded-lg border border-slate/40 bg-ink/40 px-3 py-2 text-sm"
                >
                  <div className="text-paper leading-snug font-medium">{title}</div>
                  <div className="mt-1 flex flex-wrap gap-2 text-[10px] font-mono text-mist/50">
                    {publisher && <span>{String(publisher)}</span>}
                    {pubLabel && <span>{pubLabel}</span>}
                    {url && (
                      <a
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-sky-400 hover:underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {shortUrl(url)} ↗
                      </a>
                    )}
                  </div>
                </li>
              );
            }
            return null;
          })}
        </ul>
      </div>
    );
  };

  const entries = Object.entries(data).filter(([key]) => !skipKeys.has(key));
  if (entries.length === 0) {
    return <p className="text-sm text-mist/40 italic">No additional event data.</p>;
  }

  // Prefer structured news-like arrays
  const preferredOrder = [
    "classified_events",
    "events",
    "results",
    "upcoming",
    "recent",
    "recent_changes",
    "summary",
    "checked_at",
    "cached",
  ];
  entries.sort((a, b) => {
    const ia = preferredOrder.indexOf(a[0]);
    const ib = preferredOrder.indexOf(b[0]);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });

  return (
    <div className="space-y-3">
      {entries.map(([key, value]) => {
        if (value == null || value === "") return null;
        const label = key.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());

        if (Array.isArray(value)) {
          if (value.length === 0) return null;
          // News / event objects or messy strings
          if (
            value.every(
              (item) =>
                typeof item === "string" ||
                (typeof item === "object" && item !== null)
            )
          ) {
            return renderNewsish(value, label);
          }
          return (
            <div key={key}>
              <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider mb-1">
                {label}
              </div>
              <p className="text-sm text-mist/80">{formatVal(value)}</p>
            </div>
          );
        }

        if (typeof value === "object") {
          const o = value as Record<string, unknown>;
          // Nested list inside object
          for (const nestedKey of ["items", "events", "results", "news"]) {
            if (Array.isArray(o[nestedKey])) {
              return renderNewsish(o[nestedKey] as unknown[], label);
            }
          }
          const title = formatVal(o.title || o.summary || o.description);
          return (
            <div key={key} className="rounded-lg border border-slate/40 bg-ink/40 px-3 py-2">
              <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider mb-1">
                {label}
              </div>
              {title ? (
                <p className="text-sm text-paper leading-snug">{title}</p>
              ) : (
                <p className="text-xs text-mist/60">{formatVal(value)}</p>
              )}
            </div>
          );
        }

        // Scalar: checked_at, cached, summary, etc.
        const display = formatVal(value);
        if (!display) return null;
        const isMeta = ["checked_at", "cached", "event_type", "type"].includes(key);
        return (
          <div
            key={key}
            className={
              isMeta
                ? "flex justify-between gap-4 font-mono text-[10px] text-mist/50"
                : "rounded-lg border border-slate/40 bg-ink/40 px-3 py-2"
            }
          >
            <span className={isMeta ? "uppercase tracking-wider" : "font-mono text-[10px] text-mist/50 uppercase tracking-wider block mb-1"}>
              {label}
            </span>
            <span className={isMeta ? "text-right text-mist/70" : "text-sm text-paper leading-snug"}>
              {display}
            </span>
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
                  {Number(fundamentalMetrics.debt_to_equity).toFixed(2)}x
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

// ── Event Score badge (nature-based 0-100 score + top contributors) ──
function EventScoreBadge({ data }: { data: any }) {
  const score: number | undefined =
    data?.event_score ?? data?.event_data?.event_score ?? data?.events?.event_score;
  const breakdown: any[] =
    data?.event_score_breakdown ?? data?.event_data?.event_score_breakdown ?? data?.events?.event_score_breakdown ?? [];
  if (score == null) return null;

  const tone = score >= 65 ? "text-emerald-400 border-emerald-500/40 bg-emerald-500/10"
    : score >= 50 ? "text-amber-300 border-amber-500/30 bg-amber-500/10"
    : "text-rose-400 border-rose-500/40 bg-rose-500/10";

  const top = [...breakdown]
    .sort((a, b) => Math.abs(b.decayed_impact ?? 0) - Math.abs(a.decayed_impact ?? 0))
    .slice(0, 3);

  return (
    <div className="mt-2 mb-3 flex flex-wrap items-center gap-2">
      <span className={`font-mono text-[11px] px-2 py-1 rounded-md border ${tone}`}>
        Event Score {Math.round(score)}/100
      </span>
      {top.map((b, i) => (
        <span
          key={i}
          className={`font-mono text-[10px] px-1.5 py-0.5 rounded border ${
            (b.decayed_impact ?? 0) >= 0
              ? "text-emerald-300/90 border-emerald-500/25 bg-emerald-500/5"
              : "text-rose-300/90 border-rose-500/25 bg-rose-500/5"
          }`}
          title={`base ${b.base_impact} · age ${b.age_days ?? "?"}d`}
        >
          {String(b.type || "").replace(/_/g, " ")} {(b.decayed_impact ?? 0) >= 0 ? "+" : ""}
          {b.decayed_impact}
        </span>
      ))}
    </div>
  );
}

// ── HorizonStrip (v2) ──


export function HorizonStrip({ data }: { data: any }) {
  const hz = data?.horizons || {};
  const order = ["short", "mid", "long"] as const;
  const fv = data?.final_verdict;
  const hasCards = order.some((k) => hz[k]);
  if (!fv && !hasCards) return null;
  return (
    <div className="hz-strip">
      {fv && (
        <div className="hz-verdict">
          <div className="hz-verdict-title">Final verdict · short preferred</div>
          <div className="hz-verdict-body">{fv.summary || fv.headline}</div>
        </div>
      )}
      <div className="hz-cards">
        {order.map((k) => {
          const h = hz[k];
          if (!h) return null;
          const dec = String(h.decision || "");
          const tone =
            /BUY NOW/i.test(dec) ? "buy" :
            /PREPARE/i.test(dec) ? "prep" :
            /HOLD|WAIT/i.test(dec) ? "hold" : "sell";
          return (
            <div key={k} className={`hz-card hz-${tone}`}>
              <div className="hz-card-label">{h.label || k}</div>
              <div className="hz-card-decision">{h.decision}</div>
              <div className="hz-card-score">Score {h.score}</div>
              <div className="hz-card-period">{h.holding_period}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}