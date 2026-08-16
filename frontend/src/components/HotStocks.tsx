import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useStockkyRealtime } from "../useRealtime";
import ConvictionCard from "./ConvictionCard";

type HotItem = {
  symbol: string;
  decision?: string;
  score?: number | null;
  combined_score?: number | null;
  news_score?: number | null;
  headline_count?: number;
  summary?: string;
  news_summary?: string;
  event_summary?: string;
  reasons?: string[];
  headlines?: { title?: string; publisher?: string; url?: string }[];
  next_earnings_date?: string | null;
  earnings_surprise?: { surprise_pct?: number } | null;
  insider_transactions?: any[];
  bulk_deals?: any[];
  section?: string;
  data_quality?: any;
  data_insufficient?: boolean;
  news_data?: { summary?: string };
  event_data?: { summary?: string; next_earnings_date?: string };
};

type HotPayload = {
  news_driven: HotItem[];
  results_driven: HotItem[];
  bulk_insider_driven: HotItem[];
  generated_at?: string;
  universe_size?: number;
  cached?: boolean;
};

function hotSummary(item: HotItem): string {
  return (
    item.news_summary ||
    item.event_summary ||
    item.summary ||
    item.news_data?.summary ||
    item.event_data?.summary ||
    item.headlines?.[0]?.title ||
    ""
  );
}

function Section({
  title,
  subtitle,
  items,
  liveQuotes,
}: {
  title: string;
  subtitle: string;
  items: HotItem[];
  liveQuotes: Record<string, { price: number; as_of?: string }>;
}) {
  return (
    <section className="hot-section">
      <header className="hot-section-header">
        <h3 className="hot-section-title">{title}</h3>
        <p className="hot-section-sub">{subtitle}</p>
      </header>
      {items.length === 0 ? (
        <div className="hot-empty">No qualifying picks in this section right now.</div>
      ) : (
        <div className="hot-grid">
          {items.map((item) => {
            const sum = hotSummary(item);
            const live = liveQuotes[item.symbol];
            return (
              <ConvictionCard
                key={`${item.section || title}-${item.symbol}`}
                data={{
                  symbol: item.symbol,
                  decision: item.decision,
                  score: item.score,
                  combined_score: item.combined_score ?? item.score,
                  news_score: item.news_score,
                  summary: sum || item.summary,
                  reasons: item.reasons,
                  data_quality: item.data_quality,
                  data_insufficient: item.data_insufficient,
                  news_data: item.news_data || (sum ? { summary: sum } : undefined),
                  event_data: item.event_data,
                  live_price: live?.price,
                  quote_as_of: live?.as_of,
                  close: live?.price,
                }}
                compact
                footer={
                  <>
                    {sum && <p className="hot-meta text-xs text-mist/80">{sum}</p>}
                    {item.next_earnings_date && (
                      <p className="hot-meta mono">Next results: {item.next_earnings_date}</p>
                    )}
                    {item.earnings_surprise?.surprise_pct != null && (
                      <p className="hot-meta mono">
                        Surprise: {item.earnings_surprise.surprise_pct > 0 ? "+" : ""}
                        {item.earnings_surprise.surprise_pct}%
                      </p>
                    )}
                    {(item.headlines || []).slice(0, 2).map((h, i) => (
                      <p key={i} className="hot-meta mono text-[10px] text-mist/60">
                        {h.title}
                      </p>
                    ))}
                    {live?.price != null && (
                      <p className="hot-meta mono text-signal-buy text-[10px]">
                        Live ₹{live.price.toLocaleString("en-IN")}
                      </p>
                    )}
                  </>
                }
              />
            );
          })}
        </div>
      )}
    </section>
  );
}

export default function HotStocks() {
  const { subscribeQuotes, quotes: liveQuotes, connected: quoteWs } = useStockkyRealtime();
  const [data, setData] = useState<HotPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (force = false) => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.getStockkyHot(force);
      setData(res as HotPayload);
      const syms = [
        ...((res as HotPayload).news_driven || []),
        ...((res as HotPayload).results_driven || []),
        ...((res as HotPayload).bulk_insider_driven || []),
      ]
        .map((x) => x.symbol)
        .filter(Boolean);
      if (syms.length) subscribeQuotes(syms);
    } catch (e: any) {
      setError(e?.message || "Failed to load Stockky Hot Picks");
    } finally {
      setLoading(false);
    }
  }, [subscribeQuotes]);

  useEffect(() => {
    load(false);
  }, [load]);

  return (
    <div className="hot-page hot-picks-page">
      <div className="hot-toolbar terminal-panel">
        <div>
          <p className="dash-section-title">Hot Picks</p>
          <h2 className="hot-page-title">Stockky Hot Picks</h2>
          <p className="hot-page-sub text-xs text-mist/70">
            Curated from recent news, results, and bulk/insider activity — real data only.
            {quoteWs ? " · WS quotes on" : " · quotes connecting…"}
          </p>
        </div>
        <button type="button" className="btn-terminal" onClick={() => load(true)} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {error && <div className="hot-error">{error}</div>}
      {loading && !data && <div className="hot-loading mono">Loading curated picks…</div>}

      {data && (
        <>
          <div className="hot-meta-bar mono text-[10px] text-mist/50">
            Universe {data.universe_size ?? "—"} · {data.cached ? "cached" : "fresh"} ·{" "}
            {data.generated_at ? new Date(data.generated_at).toLocaleString("en-IN") : ""}
          </div>
          <Section
            title="News-driven"
            subtitle="Elevated news score or material headlines"
            items={data.news_driven || []}
            liveQuotes={liveQuotes}
          />
          <Section
            title="Results / earnings"
            subtitle="Upcoming or recent results catalysts"
            items={data.results_driven || []}
            liveQuotes={liveQuotes}
          />
          <Section
            title="Bulk / insider"
            subtitle="Bulk deals and insider transaction activity"
            items={data.bulk_insider_driven || []}
            liveQuotes={liveQuotes}
          />
        </>
      )}
    </div>
  );
}
