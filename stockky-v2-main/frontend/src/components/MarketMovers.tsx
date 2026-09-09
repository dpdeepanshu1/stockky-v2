import { useEffect, useState } from "react";
import { api, MarketStock } from "../api";

type MarketType = "gainers" | "losers" | "active" | "trending";

export default function MarketMovers({ onSelect }: { onSelect: (symbol: string) => void }) {
  const [activeTab, setActiveTab] = useState<MarketType>("gainers");
  const [data, setData] = useState<MarketStock[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    fetchData(activeTab);
  }, [activeTab]);

  async function fetchData(type: MarketType) {
    setLoading(true);
    setError("");
    try {
      let res;
      switch (type) {
        case "gainers":
          res = await api.marketTopGainers();
          break;
        case "losers":
          res = await api.marketTopLosers();
          break;
        case "active":
          res = await api.marketMostActive();
          break;
        case "trending":
          res = await api.marketTrending();
          break;
      }
      setData(res.data || []);
    } catch (e) {
      setError((e as Error).message);
      setData([]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="bg-graphite/30 rounded-2xl border border-slate/60 p-4 sm:p-6 animate-fadeIn">
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-display text-xl text-paper">📊 Market Movers</h2>
        <div className="flex gap-1 flex-wrap">
          {["gainers", "losers", "active", "trending"].map((t) => (
            <button
              key={t}
              onClick={() => setActiveTab(t as MarketType)}
              className={`px-3 py-1 text-xs font-mono rounded-full transition-all duration-300 ${
                activeTab === t
                  ? "bg-signal-prepare/20 text-signal-prepare border border-signal-prepare/30 shadow-glow-sm"
                  : "text-mist/60 hover:text-paper border border-slate/30 hover:border-mist/60"
              }`}
            >
              {t === "gainers" ? "🚀 Gainers" :
               t === "losers" ? "📉 Losers" :
               t === "active" ? "📈 Active" : "🔥 Trending"}
            </button>
          ))}
        </div>
      </div>

      {loading && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-2 animate-pulse">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="h-20 bg-slate/40 rounded-xl" />
          ))}
        </div>
      )}

      {error && (
        <p className="text-signal-sell text-xs font-mono">{error}</p>
      )}

      {!loading && !error && data.length === 0 && (
        <p className="text-mist/60 text-sm font-mono text-center py-8">
          No market data available at the moment. Please try again later.
        </p>
      )}

      {!loading && !error && data.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-2">
          {data.map((stock, index) => (
            <div
              key={stock.symbol}
              onClick={() => onSelect(stock.symbol)}
              className="group bg-ink/60 border border-slate/40 rounded-xl p-3 hover:border-mist/60 hover:shadow-glow transition-all duration-300 cursor-pointer animate-fadeIn"
              style={{ animationDelay: `${index * 50}ms` }}
            >
              <div className="flex justify-between items-start">
                <span className="font-mono text-xs text-mist/80 group-hover:text-paper transition">
                  {stock.symbol}
                </span>
                <span className={`text-xs font-mono ${stock.change_pct >= 0 ? 'text-signal-buy' : 'text-signal-sell'}`}>
                  {stock.change_pct > 0 ? '+' : ''}{stock.change_pct}%
                </span>
              </div>
              <div className="text-sm font-display text-paper mt-1">
                ₹{stock.price.toLocaleString("en-IN")}
              </div>
              <div className="text-[10px] font-mono text-mist/50">
                {stock.volume ? `Vol: ${(stock.volume / 1000).toFixed(0)}k` : '—'}
              </div>
              <button
                onClick={(e) => { e.stopPropagation(); onSelect(stock.symbol); }}
                className="mt-2 text-[10px] font-mono text-mist/40 hover:text-signal-prepare transition opacity-0 group-hover:opacity-100"
              >
                Analyse →
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}