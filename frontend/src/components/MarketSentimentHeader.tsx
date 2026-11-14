import { useEffect, useState } from "react";
import { MoodGauge } from "./ui/MoodGauge";   // design v5
import { Card, CardHeader } from "./kit/Card";   // E536
import { Stat, Stats } from "./kit/Stat";
import { Button } from "./kit/Button";
import { StatusPill } from "./kit/StatusPill";
import { SkeletonLines } from "./kit/Skeleton";
import { indexDelta, indexValue, moodView, toneOf } from "./app/homeView";
import { setVisibleInterval } from "../visibleInterval";
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
    return setVisibleInterval(() => fetchData(false), 60000);   // group 184: paused while the browser tab is hidden
  }, []);

  if (loading && !data) {
    return (
      <Card role="status" aria-label="Loading market sentiment">
        <SkeletonLines rows={3} />
      </Card>
    );
  }

  if (error) {
    return (
      <Card tone="sell">
        <p className="hm-note" role="alert">✕ {error}</p>
        <Button variant="soft" size="sm" onClick={() => fetchData(true)}>Retry</Button>
      </Card>
    );
  }

  if (!data) return null;

  const { nifty, sensex, market_mood, market_score, fetched_at, stale } = data;
  const mood = moodView(market_mood, market_score);

  // E536: the old header as three Stat tiles (Nifty 50, Sensex, market mood). `fetched_at` is already formatted in IST (e.g. "08:37:43 AM").
  // The index feed carries only these fields, so no further tile is shown (nothing is made up).
  return (
    <Card aria-label="Market today">
      <CardHeader
        title="Market today"
        sub={fetched_at ? `Updated ${fetched_at} IST` : undefined}
        action={
          <>
            {stale && <StatusPill state="paused">Delayed</StatusPill>}
            <Button variant="ghost" size="sm" onClick={() => fetchData(true)} loading={isRefreshing} aria-label="Refresh market data">Refresh</Button>
          </>
        }
      />
      <Stats>
        <Stat label="NIFTY 50" value={indexValue(nifty.price)} tone={toneOf(nifty.change)} delta={indexDelta(nifty.change, nifty.change_pct)} />
        <Stat label="SENSEX" value={indexValue(sensex.price)} tone={toneOf(sensex.change)} delta={indexDelta(sensex.change, sensex.change_pct)} />
        <Stat label="Market mood" value={mood.word} tone={mood.tone} delta={`Score ${mood.scoreText}`} sub={stale ? "Showing the last known values" : undefined} />
      </Stats>
      <MoodGauge score={market_score} mood={market_mood} />
    </Card>
  );
}
