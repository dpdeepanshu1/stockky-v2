import { useEffect, useState } from "react";
import { api, MarketIndicesResponse } from "../api";

export default function MarketSentimentHeader() {
  const [data, setData] = useState<MarketIndicesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);

  const fetchData = async (forceRefresh = false) => {
    try {
      if (forceRefresh) {
        setIsRefreshing(true);
      } else {
        setLoading(true);
      }
      const result = await api.marketIndices(forceRefresh);
      setData(result);
      setError(null);
    } catch (err) {
      console.error("Failed to fetch market indices:", err);
      setError("Could not load indices. Please refresh.");
    } finally {
      setLoading(false);
      setIsRefreshing(false);
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(() => fetchData(false), 60000);
    return () => clearInterval(interval);
  }, []);

  if (loading && !data) {
    return (
      <div className="bg-graphite border border-slate rounded-2xl p-6 animate-pulse">
        <div className="flex justify-between items-center">
          <div className="h-6 w-32 bg-slate/50 rounded"></div>
          <div className="h-10 w-48 bg-slate/50 rounded"></div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-graphite border border-signal-sell/30 rounded-2xl p-6">
        <p className="text-signal-sell font-mono text-sm">{error}</p>
        <button
          onClick={() => fetchData(true)}
          className="mt-2 text-mist hover:text-paper text-xs font-mono underline"
        >
          Retry
        </button>
      </div>
    );
  }

  if (!data) return null;

  const { nifty, sensex, market_mood, market_score, fetched_at, stale } = data;

  const niftyColor = nifty.change >= 0 ? "text-signal-buy" : "text-signal-sell";
  const sensexColor = sensex.change >= 0 ? "text-signal-buy" : "text-signal-sell";

  const moodColor =
    market_mood === "BULLISH"
      ? "bg-signal-buy/20 text-signal-buy border-signal-buy/30"
      : market_mood === "BEARISH"
      ? "bg-signal-sell/20 text-signal-sell border-signal-sell/30"
      : "bg-signal-hold/20 text-signal-hold border-signal-hold/30";

  // `fetched_at` is already formatted in IST (e.g., "08:37:43 AM")
  const formattedTime = fetched_at || "";

  return (
    <div className="bg-graphite border border-slate rounded-2xl p-6 shadow-lg">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div className="flex items-center gap-4">
          <div>
            <h2 className="font-mono text-xs text-mist uppercase tracking-widest">
              📊 Market Sentiment
            </h2>
            <div className="flex items-center gap-3 mt-1 flex-wrap">
              <span className={`text-lg font-bold ${moodColor.split(' ')[1]}`}>
                {market_mood}
              </span>
              <span className={`px-2 py-0.5 rounded-full text-[10px] font-mono border ${moodColor}`}>
                Score: {Math.round(market_score)}/100
              </span>
              {formattedTime && (
                <span className="text-[10px] text-mist/40 font-mono flex items-center gap-1">
                  Updated: {formattedTime}
                  {stale && (
                    <span className="text-yellow-500/60 ml-1" title="Data is stale – showing last known values">
                      ⚠️
                    </span>
                  )}
                </span>
              )}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-6">
          {/* NIFTY 50 */}
          <div className="flex items-center gap-3 bg-ink/50 px-4 py-2 rounded-xl border border-slate/40">
            <div>
              <div className="font-mono text-[10px] text-mist/60 uppercase tracking-wider">NIFTY 50</div>
              <div className={`font-mono text-xl font-bold ${niftyColor}`}>
                {nifty.price.toLocaleString("en-IN")}
              </div>
            </div>
            <div className={`flex items-center gap-1 font-mono text-sm ${niftyColor}`}>
              {nifty.change >= 0 ? "▲" : "▼"}
              <span>{nifty.change >= 0 ? "+" : ""}{nifty.change.toFixed(2)}</span>
              <span className="text-[10px] opacity-70">
                ({nifty.change >= 0 ? "+" : ""}{nifty.change_pct.toFixed(2)}%)
              </span>
            </div>
          </div>

          {/* SENSEX */}
          <div className="flex items-center gap-3 bg-ink/50 px-4 py-2 rounded-xl border border-slate/40">
            <div>
              <div className="font-mono text-[10px] text-mist/60 uppercase tracking-wider">SENSEX</div>
              <div className={`font-mono text-xl font-bold ${sensexColor}`}>
                {sensex.price.toLocaleString("en-IN")}
              </div>
            </div>
            <div className={`flex items-center gap-1 font-mono text-sm ${sensexColor}`}>
              {sensex.change >= 0 ? "▲" : "▼"}
              <span>{sensex.change >= 0 ? "+" : ""}{sensex.change.toFixed(2)}</span>
              <span className="text-[10px] opacity-70">
                ({sensex.change >= 0 ? "+" : ""}{sensex.change_pct.toFixed(2)}%)
              </span>
            </div>
          </div>

          {/* Refresh button */}
          <button
            onClick={() => fetchData(true)}
            disabled={isRefreshing}
            className={`text-mist/50 hover:text-paper transition-colors p-2 rounded-full hover:bg-slate/20 ${
              isRefreshing ? "animate-spin" : ""
            }`}
            title="Refresh now"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 3v6h-6" />
              <path d="M3 21v-6h6" />
              <path d="M18.364 5.636a9 9 0 1 1-12.728 12.728" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}