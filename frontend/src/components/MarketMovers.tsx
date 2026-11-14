import { useEffect, useState } from "react";
import { api, MarketStock } from "../api";
import { PriceRoll, Skeleton } from "./ui/motion";
import { Card, CardHeader } from "./kit/Card";   // E536
import { Tabs } from "./kit/Tabs";
import { pctText, toneOf } from "./app/homeView";
import { EmptyState } from "./ui/EmptyState";   // group 461 (phase 4)

type MarketType = "gainers" | "losers" | "active" | "trending";

export default function MarketMovers({ onSelect }: { onSelect: (symbol: string) => void }) {
  const [activeTab, setActiveTab] = useState<MarketType>("gainers");
  const [data, setData] = useState<MarketStock[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [staleSince, setStaleSince] = useState("");

  useEffect(() => {
    fetchData(activeTab);
  }, [activeTab]);

  async function fetchData(type: MarketType) {
    setLoading(true);
    setError("");
    setStale(false);
    setStaleSince("");
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
      setStale(res.stale === true);
      setStaleSince(res.stale === true && typeof res.stale_since === "string" ? res.stale_since : "");
    } catch (e) {
      setError((e as Error).message);
      setData([]);
      setStale(false);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card aria-label="Market movers" data-testid="market-movers">
      <CardHeader title="Market movers" sub="Tap a stock to analyse it." />
      <Tabs<MarketType>
        label="Mover list"
        value={activeTab}
        onChange={setActiveTab}
        items={[
          { id: "gainers", label: "Gainers" },
          { id: "losers", label: "Losers" },
          { id: "active", label: "Most active" },
          { id: "trending", label: "Trending" },
        ]}
      />

      {loading && (
        <div className="hm-movers-grid" role="status" aria-label="Loading market movers">
          {[...Array(6)].map((_, i) => (
            <Skeleton key={i} h={64} className="rounded-xl" />
          ))}
        </div>
      )}

      {error && (
        <p className="hm-note" role="alert">✕ {error}</p>
      )}

      {!loading && !error && data.length === 0 && (
        <EmptyState title="No market data right now" hint="The list fills in when prices are available. Try again in a minute." />
      )}

      {!loading && !error && stale && data.length > 0 && (
        <p className="hm-note">
          Live data unavailable — showing the last known list
          {staleSince ? ` (saved ${staleSince} IST)` : " (may be from an earlier session)"}.
        </p>
      )}

      {!loading && !error && data.length > 0 && (
        <div className="hm-movers-grid">
          {data.map((stock) => {
            const t = toneOf(stock.change_pct);
            return (
              <button
                type="button"
                key={stock.symbol}
                onClick={() => onSelect(stock.symbol)}
                aria-label={`Analyse ${stock.symbol}`}
                className="hm-mover"
              >
                <span className="hm-mover-top">
                  <span className="hm-mover-sym">{stock.symbol}</span>
                  <span className={`hm-mover-pct num ${t}`}>{t === "up" ? "▲ " : t === "down" ? "▼ " : ""}{pctText(stock.change_pct)}</span>
                </span>
                <span className="hm-mover-price num">
                  <PriceRoll value={stock.price} text={`₹${stock.price.toLocaleString("en-IN")}`} />
                </span>
                <span className="hm-mover-vol">{stock.volume ? `Vol ${(stock.volume / 1000).toFixed(0)}k` : "—"}</span>
              </button>
            );
          })}
        </div>
      )}
    </Card>
  );
}
