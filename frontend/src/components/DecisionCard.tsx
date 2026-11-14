import { useState, useEffect } from "react";
import { setVisibleInterval } from "../visibleInterval";
import { Decision, api, TrainingScore, FundamentalMetrics, CategorizedEvents } from "../api";
import StockChart from "./StockChart";
import PatternStrip from "./PatternStrip";
import BottomSheet from "./BottomSheet";
import { useStockkyRealtime } from "../useRealtime";
import { toActionablePick } from "./ScanPanel";
import { resolveDisplayPrice, formatInrPrice } from "../priceDisplay";
import StructurePanel from "./StructurePanel";
import { getNewsItems, computeNewsSentimentScore, formatWhen } from "./decision/newsScore";
import { EventSection } from "./decision/EventParts";
import { PriceLevelBar, ScoreBar, ReasonList, MetricItem, EventScoreBadge } from "./decision/Bits";
import { DecisionTop } from "./decision/DecisionTop";
import { isLongIdea } from "./decision/planView";
import { Tabs } from "./kit/Tabs";
export { HorizonStrip } from "./decision/Bits";   // E536: kept as an export of this module
import { decisionToSnapshot, policyRecord, waitForEntryText, prepareTriggerText, retestBadge } from "../structureView";

interface Props {
  data: Decision;
  onBack: () => void;
  onSearchRelated: (symbol: string) => void;
  onAddToWatchlist: (symbol: string) => void;
}

export default function DecisionCard({ data, onBack, onSearchRelated, onAddToWatchlist }: Props) {
  const isBullish = isLongIdea(data.decision);
  const [detailTab, setDetailTab] = useState<"overview" | "chart" | "reasons" | "news">("overview");
  useEffect(() => { setDetailTab("overview"); }, [data.symbol]);   // a new stock opens on its overview
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
  const sentimentColor = computedNewsScore >= 70 ? 'text-signal-buy' :
                         computedNewsScore >= 50 ? 'text-signal-hold' :
                         'text-signal-sell';

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
    const stop = setVisibleInterval(tick, 45000);   // group 184: paused while the browser tab is hidden
    return () => { cancelled = true; stop(); };
  }, [data.symbol, quoteWs]);


  // ── Scores breakdown (use computed news score) ──
  const scores = [
    { label: "Technical", value: data.technical_score },
    { label: "Fundamental", value: data.fundamental_score },
    { label: "News", value: computedNewsScore },
    ...(data.prediction_score !== null && data.prediction_score !== undefined ? [{ label: "AI Model", value: data.prediction_score }] : []),
    { label: "Market Sentiment", value: data.market_score ?? 50 },
    { label: "Training", value: data.training_score ?? 50 },
    // Event pillar was computed by the decision engine (nature-based
    // event_depth.compute_event_score, 0-100) and returned on every decision
    // response, but never had a row here — add it whenever it's present.
    ...(data.event_score !== null && data.event_score !== undefined ? [{ label: "Event", value: data.event_score }] : []),
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

  // E536: the Call Alert click handler, moved out of the old inline onClick (body unchanged).
  const handleCallAlert = async () => {
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
        className="font-display tabular-nums text-xs text-mist hover:text-paper transition flex items-center gap-1"
      >
        ← Back
      </button>
      {liveQuote?.price != null && (
        <p className="mono text-[10px] text-mist/60">
          Live quote ₹{liveQuote.price.toLocaleString("en-IN")} · {quoteWs ? "WS live" : "poll"} ·{" "}
          {liveQuote.as_of ? new Date(liveQuote.as_of).toLocaleTimeString("en-IN") : ""}
        </p>
      )}

      {/* group353: daily structure — policy record, wait-for-entry range and the collapsible Trend/Momentum/Structure/Location/Risk panel */}
      {(() => {
        const ds = data.structure;
        const snap = decisionToSnapshot(ds);
        if (!snap) return null;
        const rec = policyRecord(ds);
        const wait = waitForEntryText(ds);
        const retest = retestBadge(ds);
        const prep = data.decision === "PREPARE TO BUY" ? prepareTriggerText(ds) : null;
        const line = ds?.summary_line;
        return (
          <div className="space-y-2" data-testid="decision-structure">
            {retest && (
              <p className="font-display tabular-nums text-[11px] rounded-lg border px-3 py-2 border-signal-buy/40 text-signal-buy bg-signal-buy/10" title={retest.title} data-testid="decision-retest-badge">
                {retest.label}
              </p>
            )}
            {rec && (
              <p className={`font-display tabular-nums text-[11px] rounded-lg border px-3 py-2 ${rec.tone === "bad" ? "border-signal-sell/40 text-signal-sell bg-signal-sell/10" : "border-signal-hold/40 text-signal-hold bg-signal-hold/10"}`}>
                {rec.text}
              </p>
            )}
            {wait && <p className="font-display tabular-nums text-[11px] text-mist">{wait}</p>}
            {prep && <p className="font-display tabular-nums text-[11px] text-mist">{prep}</p>}
            {!rec && line && <p className="font-display tabular-nums text-[10px] text-mist/70 sm:hidden">{line}</p>}
            <StructurePanel snapshot={snap} atrStop={ds?.legacy_levels?.stop_loss ?? null} />
          </div>
        );
      })()}

      <DecisionTop
        data={data}
        displayClose={displayClose}
        hasPrice={hasPrice}
        calling={calling}
        isAddingWatchlist={isAddingWatchlist}
        onTrade={() => setShowTradeModal(true)}
        onCallAlert={handleCallAlert}
        onWatchlist={handleAddToWatchlist}
      />

      {/* Trade modal — 2026-09-03 UI upgrade: BottomSheet (slides up on mobile) */}
      <BottomSheet
        isOpen={showTradeModal}
        onClose={() => { setShowTradeModal(false); setTradeResult(null); }}
        title={`Trade ${data.symbol}`}
        desktopMaxWidth="sm:max-w-sm"
      >
        <div className="p-5">
          <p className="text-mist text-xs mb-4">
            Opens a paper trade at ₹{data.close ?? "current price"} using dummy capital from your
            shared portfolio balance, and records this pick to training either way.
          </p>
          <div className="flex items-center gap-2 mb-4">
            <span className="font-display text-lg text-mist">₹</span>
            <input
              type="number"
              value={tradeCapital}
              onChange={(e) => setTradeCapital(e.target.value)}
              className="flex-1 bg-ink border border-slate rounded-2xl px-3 py-2.5 font-display text-lg text-paper focus:outline-none focus:border-signal-buy"
              autoFocus
            />
          </div>
          {tradeResult && (
            <p className="text-xs text-mist mb-4">{tradeResult}</p>
          )}
          <div className="flex gap-2">
            <button
              onClick={() => { setShowTradeModal(false); setTradeResult(null); }}
              className="flex-1 text-xs font-semibold uppercase tracking-wider border border-slate rounded-2xl py-2.5 text-mist hover:text-paper transition"
            >
              {tradeResult ? "Close" : "Cancel"}
            </button>
            {!tradeResult && (
              <button
                onClick={handleTradeThis}
                disabled={tradingInProgress}
                className="flex-1 text-xs font-semibold uppercase tracking-wider bg-signal-buy text-white rounded-2xl py-2.5 hover:opacity-90 transition disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {tradingInProgress && (
                  <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-white animate-spin" />
                )}
                {tradingInProgress ? "Opening..." : "Confirm Trade"}
              </button>
            )}
          </div>
        </div>
      </BottomSheet>

      <Tabs label="Stock detail" value={detailTab} onChange={setDetailTab} items={[
        { id: "overview", label: "Overview" },
        { id: "chart", label: "Chart" },
        { id: "reasons", label: "Reasons" },
        { id: "news", label: "News & events" },
      ]} />

      {detailTab === "overview" && (
        <div className="space-y-4">
          {/* Natural-language summary */}
          {data.natural_language_summary && (
            <div className="rounded-2xl border border-slate/60 bg-graphite/50 p-5">
              <h4 className="font-display tabular-nums text-xs text-mist uppercase tracking-widest mb-2">
                💬 Final Remarks
              </h4>
              <p className="text-sm text-paper/90 leading-relaxed">
                {data.natural_language_summary}
              </p>
            </div>
          )}

          {/* Training score */}
          {loadingTrainingScore ? (
            <div className="rounded-2xl border border-slate/40 bg-graphite/30 p-4 text-xs text-mist/40 font-display tabular-nums flex items-center gap-2">
              <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-mist/40 animate-spin" />
              Checking model recommendation...
            </div>
          ) : trainingScore && (trainingScore as any).available !== false ? (
            <div className="rounded-2xl border border-signal-prepare/30 bg-graphite p-5">
              <h3 className="font-display tabular-nums text-[10px] text-mist uppercase tracking-widest mb-3">
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
            <div className="rounded-2xl border border-slate bg-graphite p-5">
              <div className="font-display tabular-nums text-[10px] text-mist uppercase tracking-widest mb-3">Price levels</div>
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
            <div className="rounded-2xl border border-slate bg-graphite p-5">
              <div className="font-display tabular-nums text-[10px] text-mist uppercase tracking-widest mb-3">Score breakdown</div>
              <div className="space-y-3">
                {scores.map((s) => (
                  <ScoreBar key={s.label} label={s.label} value={s.value} />
                ))}
              </div>
            </div>
          </div>

          {/* Holding period */}
          {(data.holding_period !== "N/A" || data.holding_period_estimate) && (
            <div className="rounded-2xl border border-slate bg-graphite px-5 py-4 font-display tabular-nums text-xs text-mist">
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
            <div className="rounded-2xl border border-signal-buy/40 bg-signal-buy/5 px-5 py-4">
              <div className="flex items-center gap-2 font-display tabular-nums text-xs uppercase tracking-widest text-signal-buy">
                💎 Highly Recommend for Long Term Hold
              </div>
              <p className="text-mist/60 text-xs mt-1">
                Strong fundamentals independent of current short-term entry timing.
              </p>
              {data.long_term_hold_estimate && (
                <div className="mt-2 pt-2 border-t border-signal-buy/20 flex justify-between text-xs">
                  <span className="text-mist/50 uppercase tracking-widest text-[10px]">Suggested hold</span>
                  <span className="text-paper font-display tabular-nums">{data.long_term_hold_estimate.label}</span>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {detailTab === "chart" && (
        <div className="space-y-4">
          <PatternStrip symbol={data.symbol} />

          <StockChart symbol={data.symbol} />
        </div>
      )}

      {detailTab === "reasons" && (
        <div className="space-y-4">
          {/* Fundamental Metrics */}
          {metrics && (
            <div className="rounded-2xl border border-slate/60 bg-graphite/30 p-5">
              <h3 className="font-display tabular-nums text-[10px] text-mist uppercase tracking-widest mb-3">
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
        </div>
      )}

      {detailTab === "news" && (
        <div className="space-y-4">
          {/* ── Single News panel (to the point) ── */}
          <section className="terminal-panel">
            <div className="flex items-center justify-between gap-2 mb-2 flex-wrap">
              <p className="dash-section-title mb-0">News</p>
              <span className={`font-display tabular-nums text-[11px] ${sentimentColor}`}>
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
                    <li key={i} className="font-display tabular-nums text-[11px] text-mist/85 leading-snug">
                      {url ? (
                        <a href={url} target="_blank" rel="noreferrer" className="text-signal-prepare hover:text-signal-prepare underline-offset-2 hover:underline">
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
                  <li key={idx} className="font-display tabular-nums text-[11px] text-mist/80">
                    {item.url ? (
                      <a href={item.url} target="_blank" rel="noreferrer" className="text-signal-prepare hover:underline">
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
              <p className="font-display tabular-nums text-[11px] text-signal-hold/90">
                Next earnings: {(data as any).event_data.next_earnings_date}
              </p>
            )}
            <EventScoreBadge data={data} />
            <EventSection symbol={data.symbol} compact />
          </section>
        </div>
      )}
    </div>
  );
}
