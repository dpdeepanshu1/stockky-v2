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
  onAnalyze,
}: {
  title: string;
  subtitle: string;
  items: HotItem[];
  liveQuotes: Record<string, { price: number; as_of?: string }>;
  onAnalyze?: (symbol: string) => void;
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
                onSelect={onAnalyze}
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
                  onAnalyze ? (
                    <button
                      type="button"
                      className="btn-terminal w-full text-[10px] mt-1"
                      onClick={(e) => {
                        e.stopPropagation();
                        onAnalyze(item.symbol);
                      }}
                    >
                      Analyse {item.symbol}
                    </button>
                  ) : null
                }
              />
            );
          })}
        </div>
      )}
    </section>
  );
}

export default function HotStocks(props: { onAnalyze?: (symbol: string) => void } = {}) {
  const { onAnalyze } = props;
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
            Ranked by scan BUY/PREPARE, bulk/insider, and results first — weak news-only names dropped.
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
            Universe {data.universe_size ?? "—"}
            {" · "}scan seeds {(data as any).scan_seed_count ?? "—"}
            {" · "}{data.cached ? "cached" : "fresh"}
            {" · "}{data.generated_at ? new Date(data.generated_at).toLocaleString("en-IN") : ""}
            {(data as any).quality_note ? ` · ${(data as any).quality_note}` : ""}
          </div>
          <Section
            title="News-driven"
            subtitle="Strict filter: 2+ headlines and score ≥55; prefers scan/event signal"
            items={data.news_driven || []}
            liveQuotes={liveQuotes}
            onAnalyze={onAnalyze}
          />
          <Section
            title="Results / earnings"
            subtitle="Results/earnings catalysts (preferred over thin news)"
            items={data.results_driven || []}
            liveQuotes={liveQuotes}
            onAnalyze={onAnalyze}
          />
          <Section
            title="Bulk / insider"
            subtitle="Bulk/block deals & insider — highest priority section"
            items={data.bulk_insider_driven || []}
            liveQuotes={liveQuotes}
            onAnalyze={onAnalyze}
          />
        </>
      )}
    </div>
  );
}
