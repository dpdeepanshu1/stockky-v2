import { useCallback, useEffect, useRef, useState } from "react";
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
  /** Set when the row came back from hotpicks_static_feed rather than a live scan. */
  stored_at?: string | null;
  stored_generated_at?: string | null;
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
  /** Present when the payload was rebuilt from the stored 24h table. */
  source?: string;
  backend?: string;
  age_hours?: number | null;
  fresh?: boolean;
  hours?: number;
  count?: number;
  /** Present when a scan was stopped part-way through. */
  partial?: boolean;
  stopped_early?: boolean;
  processed_symbols?: number;
};

type HotAudit = {
  ok?: boolean;
  table?: string | null;
  backend?: string | null;
  configured?: boolean;
  table_exists?: boolean;
  rows_total?: number;
  rows_24h?: number;
  by_section?: Record<string, number>;
  age_hours?: number | null;
  fresh?: boolean;
  fresh_threshold_hours?: number;
  retention_hours?: number;
  missing_decision?: number;
  missing_score?: number;
  issues?: string[];
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
  const v = Math.max(0, Math.round(s));
  if (v < 60) return `${v}s`;
  return `${Math.floor(v / 60)}m ${v % 60}s`;
}

function fmtAge(hours?: number | null) {
  if (hours == null || Number.isNaN(hours)) return "—";
  if (hours < 1) return `${Math.max(0, Math.round(hours * 60))}m ago`;
  if (hours < 24) return `${hours.toFixed(1)}h ago`;
  return `${(hours / 24).toFixed(1)}d ago`;
}

function payloadHasPicks(p: any): boolean {
  if (!p || typeof p !== "object") return false;
  return (
    ((p.news_driven || []) as any[]).length +
      ((p.results_driven || []) as any[]).length +
      ((p.bulk_insider_driven || []) as any[]).length >
    0
  );
}

/**
 * Hot Picks feed health — the same "can I trust this feed?" panel the premarket
 * and IPO tabs have. Reads /stockky-hot/audit, which reports which backend is
 * actually in use (Neon on Render, Oracle ADB on the Oracle VM), whether
 * hotpicks_static_feed exists, and how stale the stored rows are. That is what
 * distinguishes "no catalysts today" from "the write path is broken".
 */
function HotFeedHealth({ refreshKey }: { refreshKey: number }) {
  const [audit, setAudit] = useState<HotAudit | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const res = await api.getStockkyHotAudit();
      setAudit((res || null) as HotAudit | null);
    } catch {
      /* audit is advisory only — never block the tab on it */
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  if (!audit) return null;

  const issues = audit.issues || [];
  const healthy = Boolean(audit.ok && audit.table_exists && (audit.rows_24h ?? 0) > 0 && !issues.length);
  const dot = healthy ? "bg-emerald-400" : issues.length ? "bg-amber-400" : "bg-slate-400";
  const backendLabel =
    audit.backend === "oracle"
      ? "Oracle Autonomous DB"
      : audit.backend === "postgresql"
      ? "Neon / Postgres"
      : audit.backend || "not configured";

  return (
    <div className="rounded-2xl border border-slate/40 bg-graphite/60 p-4">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between gap-3 text-left"
      >
        <span className="flex items-center gap-2">
          <span className={`inline-block h-2 w-2 rounded-full ${dot}`} />
          <span className="font-mono text-[11px] text-paper">Hot Picks Feed Health</span>
          <span className="font-mono text-[10px] text-mist/60">
            {audit.rows_24h ?? 0} rows / 24h · {fmtAge(audit.age_hours)} · {backendLabel}
          </span>
        </span>
        <span className="font-mono text-[10px] text-mist/60">{open ? "hide ▲" : "details ▼"}</span>
      </button>

      {open && (
        <div className="mt-3 space-y-2 font-mono text-[10px] text-mist/80">
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-4">
            <span>
              table <span className="text-paper">{audit.table || "—"}</span>
            </span>
            <span>
              exists <span className="text-paper">{audit.table_exists ? "yes" : "no"}</span>
            </span>
            <span>
              rows total <span className="text-paper">{audit.rows_total ?? 0}</span>
            </span>
            <span>
              fresh (&le;{audit.fresh_threshold_hours ?? 24}h){" "}
              <span className={audit.fresh ? "text-emerald-300" : "text-amber-300"}>
                {audit.fresh ? "yes" : "no"}
              </span>
            </span>
            <span>
              retention <span className="text-paper">{audit.retention_hours ?? "—"}h</span>
            </span>
            <span>
              missing decision <span className="text-paper">{audit.missing_decision ?? 0}</span>
            </span>
            <span>
              missing score <span className="text-paper">{audit.missing_score ?? 0}</span>
            </span>
            <span>
              configured <span className="text-paper">{audit.configured ? "yes" : "no"}</span>
            </span>
          </div>

          {audit.by_section && Object.keys(audit.by_section).length > 0 && (
            <div className="flex flex-wrap gap-3 pt-1">
              {Object.entries(audit.by_section).map(([k, v]) => (
                <span key={k}>
                  {k.replace(/_/g, " ")} <span className="text-paper">{v}</span>
                </span>
              ))}
            </div>
          )}

          {issues.length > 0 && (
            <ul className="space-y-1 pt-1">
              {issues.map((msg, i) => (
                <li key={i} className="text-amber-300/90">
                  • {msg}
                </li>
              ))}
            </ul>
          )}

          <button
            type="button"
            onClick={load}
            disabled={busy}
            className="mt-1 rounded-md border border-slate/50 px-2 py-1 text-[10px] text-mist hover:bg-slate/30 disabled:opacity-50"
          >
            {busy ? "Checking…" : "Re-check"}
          </button>
        </div>
      )}
    </div>
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
  const [stopBusy, setStopBusy] = useState(false);
  const [healthKey, setHealthKey] = useState(0);

  const { quotes: liveQuotes, subscribeQuotes } = useStockkyRealtime();

  useEffect(() => {
    const symbols = (data?.news_driven || [])
      .concat(data?.results_driven || [])
      .concat(data?.bulk_insider_driven || [])
      .map((x) => x.symbol)
      .filter(Boolean) as string[];
    if (symbols.length) subscribeQuotes(symbols);
  }, [data, subscribeQuotes]);

  /**
   * Instant paint. Two sources, cheapest-first:
   *   1. /stockky-hot/table — the last 24h of hotpicks_static_feed rows. Survives
   *      redeploys, kv TTL expiry and stopped scans, so the tab is never blank
   *      just because the in-memory cache went away.
   *   2. /stockky-hot/result — the kv_cache blob from the most recent full run.
   * Whichever yields picks first wins; the table is preferred because it carries
   * freshness metadata (age_hours / fresh) the blob does not.
   */
  const loadCached = useCallback(async () => {
    try {
      const stored = await api.getStockkyHotTable(24);
      if (payloadHasPicks(stored)) {
        setData(stored as unknown as HotPayload);
        return;
      }
    } catch {
      /* table not created yet, or no DB configured — fall through */
    }
    try {
      const res = await api.getStockkyHotResult();
      if (res?.ok !== false && (res?.news_driven || res?.bulk_insider_driven)) {
        setData(res as HotPayload);
      }
    } catch {
      /* no cached result yet */
    }
  }, []);

  const pollJob = useCallback(async () => {
    try {
      const st = await api.getStockkyHotStatus();
      const processed = st.processed ?? 0;
      // No synthetic fallback: /stockky-hot/run now reports the real universe
      // size, so a 0 total means "not started yet" and must not pretend to be 100.
      const total = st.total ?? 0;
      setProgress({
        processed,
        total,
        elapsed: st.elapsed_sec ?? 0,
        remaining: st.estimated_remaining_sec,
        pct: total ? Math.min(100, Math.round((processed / total) * 100)) : 0,
      });
      setJobMsg(st.message || null);
      if (st.status === "done" || st.status === "stopped") {
        setLoading(false);
        setStopBusy(false);
        const res = await api.getStockkyHotResult();
        if (res) setData(res as HotPayload);
        // A stopped run still persisted its partial rows — refresh the health panel.
        setHealthKey((k) => k + 1);
        return false;
      }
      if (st.status === "error") {
        setLoading(false);
        setStopBusy(false);
        setError(st.message || "Hot Picks failed");
        return false;
      }
      return st.status === "running";
    } catch (e: any) {
      setError(e?.message || "Status poll failed");
      setLoading(false);
      setStopBusy(false);
      return false;
    }
  }, []);

  // Keep a stable reference for the mount effect so it does not re-run on every
  // pollJob identity change.
  const pollJobRef = useRef(pollJob);
  useEffect(() => {
    pollJobRef.current = pollJob;
  }, [pollJob]);

  /**
   * Mount: paint stored picks, then RESUME an in-flight scan. Previously the
   * polling effect early-returned unless `loading` was already true, so a reload
   * (or switching tabs and back) mid-scan left the progress bar dead until the
   * user pressed Search again — and pressing Search restarted the whole scan.
   */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      await loadCached();
      if (cancelled) return;
      try {
        const st = await api.getStockkyHotStatus();
        if (!cancelled && st?.status === "running") {
          setLoading(true);
          await pollJobRef.current();
        }
      } catch {
        /* no job in flight */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadCached]);

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
    setStopBusy(false);
    setJobMsg("Starting Hot Picks search…");
    // total 0 until the backend reports the real universe size — a fake 100 is
    // what made the ETA read ~0s for a multi-minute scan.
    setProgress({ processed: 0, total: 0, elapsed: 0, remaining: null, pct: 0 });
    try {
      await api.runStockkyHot(true);
      await pollJob();
    } catch (e: any) {
      setError(e?.message || "Failed to start Hot Picks");
      setLoading(false);
    }
  };

  /**
   * Stop asks the backend to break out of the symbol loop after the symbol it is
   * currently on (network calls in flight are allowed to finish). Whatever was
   * already scored is still cached and still written to hotpicks_static_feed, so
   * stopping early degrades the result instead of discarding it.
   */
  const stopSearch = async () => {
    setStopBusy(true);
    try {
      await api.stopStockkyHot();
      setJobMsg("Stop requested — finishing current symbol…");
      await pollJob();
    } catch (e: any) {
      setError(e?.message || "Failed to stop Hot Picks");
      setStopBusy(false);
    }
  };

  const storedFromTable = data?.source === "hotpicks_static_feed";

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
          {loading && (
            <button
              type="button"
              onClick={stopSearch}
              disabled={stopBusy}
              className="font-mono text-xs px-4 py-2 rounded-lg border border-amber-500/50 bg-amber-500/15 text-amber-200 hover:bg-amber-500/25 disabled:opacity-50"
            >
              {stopBusy ? "Stopping…" : "■ Stop"}
            </button>
          )}
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
                {progress?.processed ?? 0}/{progress?.total ? progress.total : "…"}
              </span>
              <span>Elapsed {fmtSec(progress?.elapsed)}</span>
              <span>
                Remaining{" "}
                {progress?.remaining == null
                  ? "estimating…"
                  : `~${fmtSec(progress?.remaining)}`}
              </span>
              <span>{jobMsg}</span>
            </div>
            <Pipeline running={true} />
          </div>
        )}

        {error && <div className="hot-error mt-3">{error}</div>}
      </div>

      <HotFeedHealth refreshKey={healthKey} />

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
            {storedFromTable
              ? ` · stored feed (last ${data.hours ?? 24}h, ${data.count ?? 0} rows, ${fmtAge(
                  data.age_hours,
                )})`
              : ""}
            {storedFromTable && data.fresh === false ? " · STALE — run a fresh scan" : ""}
            {data.stopped_early
              ? ` · PARTIAL — scan stopped after ${data.processed_symbols ?? 0} symbols`
              : ""}
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
