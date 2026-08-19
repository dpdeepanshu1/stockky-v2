import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useStockkyRealtime } from "../useRealtime";
import ConvictionCard from "./ConvictionCard";
import Pipeline from "./Pipeline";
import { BuySniperModal, type BuySuggestion } from "./BuySniperModal";
import { getSafePrice } from "../priceDisplay";

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
  signal_strength?: string;
  news_data?: { summary?: string };
  event_data?: { summary?: string; next_earnings_date?: string };
};

type HotPayload = {
  news_driven: HotItem[];
  results_driven: HotItem[];
  bulk_insider_driven: HotItem[];
  generated_at?: string;
  persisted_at?: string;
  universe_size?: number;
  cached?: boolean;
  quality_note?: string;
  scan_seed_count?: number;
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

function fmtSec(s?: number | null) {
  if (s == null || Number.isNaN(s)) return "—";
  if (s < 60) return `${Math.max(0, s)}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
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
                  ...item,
                  close: live?.price,
                  combined_score: item.score ?? item.combined_score,
                  natural_language_summary: sum,
                } as any}
              />
            );
          })}
        </div>
      )}
    </section>
  );
}

export default function HotStocks({ onAnalyze }: { onAnalyze?: (symbol: string) => void }) {
  const [data, setData] = useState<HotPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobMsg, setJobMsg] = useState<string | null>(null);
  const [progress, setProgress] = useState<{
    processed: number;
    total: number;
    elapsed: number;
    remaining?: number | null;
    pct: number;
  } | null>(null);
  const [sniperOpen, setSniperOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<BuySuggestion[]>([]);
  const [sniperLoading, setSniperLoading] = useState(false);
  const [sniperError, setSniperError] = useState<string | null>(null);

  const { quotes: liveQuotes, subscribeQuotes } = useStockkyRealtime();

  useEffect(() => {
    const symbols = (data?.news_driven || [])
      .concat(data?.results_driven || [])
      .concat(data?.bulk_insider_driven || [])
      .map((x) => x.symbol)
      .filter(Boolean) as string[];
    if (symbols.length) subscribeQuotes(symbols);
  }, [data, subscribeQuotes]);

  const loadCached = useCallback(async () => {
    try {
      const res = await api.getStockkyHotResult();
      if (res?.ok !== false && (res?.news_driven || res?.bulk_insider_driven)) {
        setData(res as HotPayload);
      }
    } catch {
      /* no cached result yet */
    }
  }, []);

  useEffect(() => {
    loadCached();
  }, [loadCached]);

  const pollJob = useCallback(async () => {
    try {
      const st = await api.getStockkyHotStatus();
      const processed = st.processed ?? 0;
      const total = st.total ?? 100;
      setProgress({
        processed,
        total,
        elapsed: st.elapsed_sec ?? 0,
        remaining: st.estimated_remaining_sec,
        pct: total ? Math.min(100, Math.round((processed / total) * 100)) : 0,
      });
      setJobMsg(st.message || null);
      if (st.status === "done") {
        setLoading(false);
        const res = await api.getStockkyHotResult();
        if (res) setData(res as HotPayload);
        return false;
      }
      if (st.status === "error") {
        setLoading(false);
        setError(st.message || "Hot Picks failed");
        return false;
      }
      return st.status === "running";
    } catch (e: any) {
      setError(e?.message || "Status poll failed");
      setLoading(false);
      return false;
    }
  }, []);

  useEffect(() => {
    if (!loading) return;
    const id = setInterval(async () => {
      const keep = await pollJob();
      if (!keep) clearInterval(id);
    }, 2000);
    return () => clearInterval(id);
  }, [loading, pollJob]);


  const handleSearchBuysFromHot = async () => {
    setSniperOpen(true);
    setSniperLoading(true);
    setSniperError(null);
    setSuggestions([]);
    try {
      const list = [
        ...(data?.bulk_insider_driven || []),
        ...(data?.results_driven || []),
        ...(data?.news_driven || []),
      ];
      const mapped = list.map((p) => {
        const live = (liveQuotes as any)?.[p.symbol!];
        const safePrice = getSafePrice({
          price: live?.price ?? (p as any).price,
          cmp: (p as any).cmp,
          last_price: (p as any).last_price,
          ltp: (p as any).ltp,
          close: (p as any).close,
          current_price: (p as any).current_price,
          prev_close: (p as any).prev_close,
        });
        return {
          symbol: p.symbol,
          decision: p.decision || "PREPARE TO BUY",
          combined_score: p.combined_score ?? p.score ?? 75,
          conviction: p.combined_score ?? p.score ?? 75,
          price: safePrice,
          cmp: safePrice,
          ltp: safePrice,
          close: safePrice,
          change_pct: Number((p as any).change_pct || 0),
          technical_score: Number((p as any).technical_score || 75),
          fundamental_score: Number((p as any).fundamental_score || 70),
          news_score: p.news_score,
        };
      }).filter((x) => x.symbol && Number(x.price) > 0);
      const res = await api.findBuys({
        stocks: mapped,
        target_count: 4,
        min_conviction: 55,
      });
      setSuggestions((res?.suggestions || []) as BuySuggestion[]);
      if (res?.error) setSniperError(String(res.error));
    } catch (err: any) {
      console.error("Hot picks sniper error:", err);
      setSniperError(err?.message || "Failed to find buy setups");
    } finally {
      setSniperLoading(false);
    }
  };

  const startSearch = async () => {
    setError(null);
    setLoading(true);
    setJobMsg("Starting Hot Picks search…");
    setProgress({ processed: 0, total: 100, elapsed: 0, remaining: null, pct: 0 });
    try {
      await api.runStockkyHot(true);
      await pollJob();
    } catch (e: any) {
      setError(e?.message || "Failed to start Hot Picks");
      setLoading(false);
    }
  };

  return (
    <div className="hot-root space-y-4">
      <div className="rounded-2xl border border-slate/50 bg-graphite/80 p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-display text-xl text-paper">🔥 Hot Picks</h2>
            <p className="text-mist/70 text-xs mt-1 max-w-xl">
              On-demand catalyst search. Scoring is ~70–80% news / events / bulk / results and
              20–30% other pillars. Results persist until the next run or midnight scheduler.
            </p>
          </div>
          <button
            type="button"
            onClick={startSearch}
            disabled={loading}
            className="font-mono text-xs px-4 py-2 rounded-lg bg-rose-500/20 border border-rose-500/40 text-rose-100 hover:bg-rose-500/30 disabled:opacity-50"
          >
            {loading ? "Searching…" : "Search Hot Picks Stocks"}
          </button>
          <button
            type="button"
            onClick={handleSearchBuysFromHot}
            disabled={sniperLoading || !data}
            className="font-mono text-xs px-4 py-2 rounded-lg border border-emerald-500/50 bg-emerald-600/20 text-emerald-200 hover:bg-emerald-600/35 disabled:opacity-50 shadow-lg shadow-emerald-900/20"
          >
            {sniperLoading ? "Sniping…" : "🎯 Search for Buy Stocks (1-4)"}
          </button>
        </div>

        {loading && (
          <div className="mt-4 space-y-3">
            <div className="h-2 rounded-full bg-slate/40 overflow-hidden">
              <div
                className="h-full bg-rose-500/80 transition-all duration-500"
                style={{ width: `${progress?.pct ?? 5}%` }}
              />
            </div>
            <div className="flex flex-wrap gap-4 font-mono text-[11px] text-mist">
              <span>
                {progress?.processed ?? 0}/{progress?.total ?? 100}
              </span>
              <span>Elapsed {fmtSec(progress?.elapsed)}</span>
              <span>Remaining ~{fmtSec(progress?.remaining)}</span>
              <span>{jobMsg}</span>
            </div>
            <Pipeline running={true} />
          </div>
        )}

        {error && <div className="hot-error mt-3">{error}</div>}
      </div>

      {data && (
        <>
          <div className="hot-meta-bar mono text-[10px] text-mist/50">
            Universe {data.universe_size ?? "—"}
            {" · "}scan seeds {data.scan_seed_count ?? "—"}
            {" · "}
            {data.generated_at
              ? `Updated ${new Date(data.generated_at).toLocaleString("en-IN", {
                  timeZone: "Asia/Kolkata",
                })}`
              : ""}
            {data.quality_note ? ` · ${data.quality_note}` : ""}
          </div>
          <Section
            title="Bulk / insider"
            subtitle="Highest weight — bulk/block deals & insider activity"
            items={data.bulk_insider_driven || []}
            liveQuotes={liveQuotes as any}
            onAnalyze={onAnalyze}
          />
          <Section
            title="Results / earnings"
            subtitle="Results & earnings surprise catalysts"
            items={data.results_driven || []}
            liveQuotes={liveQuotes as any}
            onAnalyze={onAnalyze}
          />
          <Section
            title="News-driven"
            subtitle="News catalysts (strict quality filter)"
            items={data.news_driven || []}
            liveQuotes={liveQuotes as any}
            onAnalyze={onAnalyze}
          />
        </>
      )}

      {!data && !loading && (
        <div className="rounded-xl border border-slate/40 bg-graphite/50 p-6 text-center text-mist/60 font-mono text-xs">
          Click <strong className="text-paper">Search Hot Picks Stocks</strong> to run the catalyst
          pipeline. Nothing is auto-loaded (free-tier friendly).
        </div>
      )}

      <BuySniperModal
        isOpen={sniperOpen}
        onClose={() => setSniperOpen(false)}
        suggestions={suggestions}
        loading={sniperLoading}
        error={sniperError}
        onSelectSymbol={onAnalyze}
      />
    </div>
  );
}
