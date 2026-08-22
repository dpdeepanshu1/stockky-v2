// frontend/src/components/SurpriseStocks.tsx
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api"; // all calls use getApiUrl() — never relative /api on separate Render hosts
import { formatInrPrice, getSafePrice } from "../priceDisplay";
import { BuySniperModal, type BuySuggestion } from "./BuySniperModal";
import { useStockkyRealtime, type RealtimeMessage } from "../useRealtime";

export interface SurpriseStock {
  symbol: string;
  score: number;
  tier?: "breakout" | "building";
  price: number;
  change_pct: number;
  rvol: number;
  rvol_slope?: number;
  buy_pct?: number;
  trigger_type: string;
  trailing_stop: number;
  target_1: number;
  prev_close?: number;
  sector?: string | null;
  dist_52w_pct?: number;
}

type PremarketProgress = {
  stage?: string;
  percent?: number;
  processed?: number;
  total?: number;
  computed?: number;
  errors?: number;
  elapsed_sec?: number;
  eta_sec?: number | null;
  is_running?: boolean;
  current_symbol?: string | null;
  message?: string;
  error?: string;
};

type ScanProgress = {
  processed: number;
  total: number;
  hits: number;
  quotes_ok: number;
  elapsed: number;
  eta_sec?: number | null;
  percent: number;
};

function formatEta(sec?: number | null) {
  if (sec == null || !Number.isFinite(sec)) return "—";
  const s = Math.max(0, Math.round(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h > 0) return `${h}h ${m}m ${r}s`;
  if (m > 0) return `${m}m ${r}s`;
  return `${r}s`;
}

export default function SurpriseStocks({
  onSelect,
}: {
  onSelect?: (symbol: string) => void;
}) {
  const [stocks, setStocks] = useState<SurpriseStock[]>([]);
  const [sniperOpen, setSniperOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<BuySuggestion[]>([]);
  const [sniperLoading, setSniperLoading] = useState(false);
  const [sniperError, setSniperError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [meta, setMeta] = useState<{
    static_loaded?: number;
    quotes_ok?: number;
    universe_scanned?: number;
    elapsed_sec?: number;
  } | null>(null);
  const [scanProg, setScanProg] = useState<ScanProgress | null>(null);
  const [lastAt, setLastAt] = useState<string | null>(null);

  const [pmRunning, setPmRunning] = useState(false);
  const [pmProgress, setPmProgress] = useState<PremarketProgress | null>(null);
  const [pmError, setPmError] = useState<string | null>(null);

  // Recent IPO scanner (Surprise tab subsection)
  const [ipoList, setIpoList] = useState<any[]>([]);
  const [ipoGeneratedAt, setIpoGeneratedAt] = useState<string | null>(null);
  const [ipoScanning, setIpoScanning] = useState(false);
  const [ipoProgress, setIpoProgress] = useState<{ processed?: number; total?: number; message?: string } | null>(null);
  const [ipoError, setIpoError] = useState<string | null>(null);
  const [ipoAddOpen, setIpoAddOpen] = useState(false);
  const [ipoForm, setIpoForm] = useState({ symbol: "", issue_price: "", listing_date: "", subscription_times: "" });
  const [ipoAddBusy, setIpoAddBusy] = useState(false);
  const ipoPollRef = useRef<number | null>(null);

  const [healthData, setHealthData] = useState<{
    health_score?: number;
    total_tracked?: number;
    missing_data?: number;
    fully_populated?: number;
    market_open?: boolean;
    source?: string;
    cache_age_sec?: number | null;
    message?: string;
    incomplete_stocks?: Array<{
      symbol: string;
      missing_fields?: string[];
      current_price?: number;
    }>;
  } | null>(null);
  const [healthLoading, setHealthLoading] = useState(false);
  const [quoteFeedBusy, setQuoteFeedBusy] = useState(false);
  const [patchingSymbol, setPatchingSymbol] = useState<string | null>(null);
  const [batchRepairBusy, setBatchRepairBusy] = useState(false);
  const pollRef = useRef<number | null>(null);
  const scanAbort = useRef<AbortController | null>(null);

  // Point 1 fix: real-time job updates over the existing /ws hub (channel
  // "jobs") instead of pure polling — the gateway now broadcasts a
  // surprise_premarket progress snapshot every ~2s while the job is
  // running (and includes rate_limits so a slow run is visibly "queued
  // behind the shared yfinance limiter", not just stuck). Polling below is
  // kept as a fallback in case the socket drops.
  const onRealtimeMessage = useCallback((msg: RealtimeMessage) => {
    if (msg.type !== "jobs_snapshot") return;
    const sp = (msg as any).surprise_premarket;
    if (sp) {
      setPmProgress(sp);
      setPmRunning(!!sp.is_running || sp.status === "running" || sp.stage === "computing");
      if (sp.stage === "error") {
        setPmError(sp.error || sp.message || "Premarket failed");
        setPmRunning(false);
        stopPmPoll();
      } else if (!sp.is_running && sp.stage === "done") {
        stopPmPoll();
      }
    }
    const rl = (msg as any).rate_limits?.yfinance;
    if (rl) setYfRateLimit(rl);
    const ipoJob = (msg as any).ipo_scan;
    if (ipoJob) {
      setIpoScanning(ipoJob.status === "running");
      setIpoProgress(ipoJob);
      if (ipoJob.status === "done") {
        stopIpoPoll();
        void fetchIpoList();
      }
    }
  }, []);
  const { connected: wsConnected } = useStockkyRealtime(onRealtimeMessage);
  const [yfRateLimit, setYfRateLimit] = useState<{ waiters?: number; throttle_events?: number } | null>(null);

  const stopPmPoll = () => {
    if (pollRef.current != null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const pollPremarket = useCallback(async () => {
    try {
      const st = await api.surprisePremarketStatus();
      setPmProgress(st);
      setPmRunning(!!st?.is_running);
      if (!st?.is_running && st?.stage === "done") {
        stopPmPoll();
      }
      if (st?.stage === "error") {
        setPmError(st.error || st.message || "Premarket failed");
        setPmRunning(false);
        stopPmPoll();
      }
    } catch (e: any) {
      console.warn("premarket status", e);
    }
  }, []);

  const startPremarket = async () => {
    setPmError(null);
    setPmRunning(true);
    setPmProgress({
      stage: "starting",
      percent: 1,
      processed: 0,
      total: 0,
      is_running: true,
      message: "Starting premarket job…",
    });
    try {
      // 1) Static baselines (Neon surprise_static_feed)
      await api.surprisePremarket();
      // 2) Chunked bulk Yahoo live quotes (50/chunk) into system:surprise_feed
      try {
        setPmProgress((p) => ({
          ...(p || {}),
          message: "Bulk Yahoo quotes (50/chunk)…",
          percent: Math.max(Number(p?.percent) || 1, 15),
        }));
        await api.runSurprisePremarketFeed(true);
        await fetchSurpriseHealth();
      } catch (bulkErr: any) {
        console.warn("surprise bulk quote feed", bulkErr);
        // Baselines may still succeed; quote cache can be retried via Live Quote Cache
      }
      stopPmPoll();
      pollRef.current = window.setInterval(pollPremarket, 2000);
      await pollPremarket();
    } catch (e: any) {
      setPmRunning(false);
      setPmError(e?.message || "Failed to start premarket");
      stopPmPoll();
    }
  };

  // ── Recent IPO scanner ──────────────────────────────────────────────
  const stopIpoPoll = () => {
    if (ipoPollRef.current != null) {
      window.clearInterval(ipoPollRef.current);
      ipoPollRef.current = null;
    }
  };

  const fetchIpoList = useCallback(async () => {
    try {
      const res = await api.ipoList();
      setIpoList(res?.results || []);
      setIpoGeneratedAt(res?.generated_at || null);
    } catch (e: any) {
      console.warn("ipo list", e);
    }
  }, []);

  const pollIpoStatus = useCallback(async () => {
    try {
      const st = await api.ipoScanStatus();
      setIpoProgress(st);
      const running = st?.status === "running";
      setIpoScanning(running);
      if (!running) {
        stopIpoPoll();
        await fetchIpoList();
      }
    } catch (e: any) {
      console.warn("ipo status", e);
    }
  }, [fetchIpoList]);

  const startIpoScan = async () => {
    setIpoError(null);
    setIpoScanning(true);
    setIpoProgress({ message: "Starting IPO scan…" });
    try {
      await api.ipoScan();
      stopIpoPoll();
      ipoPollRef.current = window.setInterval(pollIpoStatus, 2000);
      await pollIpoStatus();
    } catch (e: any) {
      setIpoScanning(false);
      setIpoError(e?.message || "Failed to start IPO scan");
      stopIpoPoll();
    }
  };

  const openIpoSuggestion = (ipo: any) => {
    if (!ipo?.buy_suggestion) return;
    setSuggestions([ipo.buy_suggestion]);
    setSniperError(null);
    setSniperLoading(false);
    setSniperOpen(true);
  };

  const submitIpoAdd = async () => {
    if (!ipoForm.symbol.trim() || !ipoForm.issue_price || !ipoForm.listing_date) return;
    setIpoAddBusy(true);
    try {
      await api.ipoAdd({
        symbol: ipoForm.symbol.trim().toUpperCase(),
        issue_price: Number(ipoForm.issue_price),
        listing_date: ipoForm.listing_date,
        subscription_times: ipoForm.subscription_times ? Number(ipoForm.subscription_times) : undefined,
      });
      setIpoForm({ symbol: "", issue_price: "", listing_date: "", subscription_times: "" });
      setIpoAddOpen(false);
      await startIpoScan();
    } catch (e: any) {
      setIpoError(e?.message || "Failed to add IPO");
    } finally {
      setIpoAddBusy(false);
    }
  };

  useEffect(() => {
    void fetchIpoList();
    return () => stopIpoPoll();
  }, [fetchIpoList]);


  const handleSearchBuysFromSurprise = async () => {
    setSniperOpen(true);
    setSniperLoading(true);
    setSniperError(null);
    setSuggestions([]);
    try {
      const mapped = stocks.map((s) => {
        const safePrice = getSafePrice(s as any);
        return {
          symbol: s.symbol,
          decision: s.score >= 75 ? "BUY NOW" : "PREPARE TO BUY",
          combined_score: s.score || 70,
          conviction: s.score || 70,
          price: safePrice,
          cmp: safePrice,
          ltp: safePrice,
          close: safePrice,
          change_pct: s.change_pct,
          technical_score: s.score || 70,
          fundamental_score: 70,
          target: s.target_1,
          stop_loss: s.trailing_stop,
          prev_close: s.prev_close,
          sector: s.sector,
        };
      }).filter((x) => x.symbol && Number(x.price) > 0);
      const data = await api.findBuys({
        stocks: mapped,
        target_count: 4,
        min_conviction: 55,
      });
      setSuggestions((data?.suggestions || []) as BuySuggestion[]);
      if (data?.error) setSniperError(String(data.error));
    } catch (err: any) {
      console.error("Sniper error:", err);
      setSniperError(err?.message || "Failed to find buy setups");
    } finally {
      setSniperLoading(false);
    }
  };

  const fetchSurpriseStocks = useCallback(async (force = false) => {
    if (scanAbort.current) {
      try {
        scanAbort.current.abort();
      } catch {}
    }
    const ac = new AbortController();
    scanAbort.current = ac;
    setLoading(true);
    setError(null);
    setStocks([]);
    setScanProg({ processed: 0, total: 0, hits: 0, quotes_ok: 0, elapsed: 0, percent: 0 });

    try {
      const live: SurpriseStock[] = [];
      let usedStream = false;
      try {
        for await (const row of api.surpriseScanStream({
          forceReload: force,
          signal: ac.signal,
        })) {
          usedStream = true;
          if (row?._meta) {
            if (row.event === "static_loaded" || row.event === "scan_start") {
              setMeta((m) => ({
                ...m,
                static_loaded: row.static_loaded ?? row.total ?? m?.static_loaded,
              }));
              if (row.total) {
                setScanProg((p) => ({
                  processed: p?.processed ?? 0,
                  total: Number(row.total) || 0,
                  hits: p?.hits ?? 0,
                  quotes_ok: p?.quotes_ok ?? 0,
                  elapsed: p?.elapsed ?? 0,
                  percent: 0,
                  eta_sec: null,
                }));
              }
            }
            if (row.event === "progress") {
              setScanProg({
                processed: Number(row.processed) || 0,
                total: Number(row.total) || 0,
                hits: Number(row.hits) || 0,
                quotes_ok: Number(row.quotes_ok) || 0,
                elapsed: Number(row.elapsed) || 0,
                eta_sec: row.eta_sec ?? null,
                percent: Number(row.percent) || 0,
              });
            }
            if (row.event === "error") {
              setError(String(row.error || "stream error"));
            }
            if (row.event === "done") {
              setMeta({
                static_loaded: row.universe ?? live.length,
                quotes_ok: row.quotes_ok,
                universe_scanned: row.universe,
                elapsed_sec: row.elapsed,
              });
              setScanProg((p) =>
                p
                  ? {
                      ...p,
                      processed: Number(row.universe) || p.total,
                      total: Number(row.universe) || p.total,
                      hits: Number(row.hits) || p.hits,
                      quotes_ok: Number(row.quotes_ok) || p.quotes_ok,
                      elapsed: Number(row.elapsed) || p.elapsed,
                      percent: 100,
                      eta_sec: 0,
                    }
                  : p
              );
            }
            continue;
          }
          if (row?.symbol) {
            // Step 6: upsert by symbol + normalize price from any alias
            const hit = { ...(row as SurpriseStock) };
            const safePx = getSafePrice(hit);
            if (safePx > 0) {
              hit.price = safePx;
            }
            const idx = live.findIndex((s) => s.symbol === hit.symbol);
            if (idx >= 0) live[idx] = hit;
            else live.push(hit);
            const sorted = [...live].sort((a, b) => b.score - a.score);
            setStocks(sorted);
            if (row._progress) {
              setScanProg({
                processed: Number(row._progress.processed) || live.length,
                total: Number(row._progress.total) || 0,
                hits: Number(row._progress.hits) || live.length,
                quotes_ok: Number(row._progress.quotes_ok) || 0,
                elapsed: Number(row._progress.elapsed) || 0,
                percent: Math.min(
                  99,
                  Math.round(
                    (100 * (Number(row._progress.processed) || 0)) /
                      Math.max(1, Number(row._progress.total) || 1)
                  )
                ),
              });
            }
          }
        }
      } catch (streamErr: any) {
        if (streamErr?.name === "AbortError") return;
        if (!usedStream) {
          const data = await api.surpriseScan(force);
          setStocks(Array.isArray(data?.stocks) ? data.stocks : []);
          setMeta({
            static_loaded: data?.static_loaded,
            quotes_ok: data?.quotes_ok,
            universe_scanned: data?.universe_scanned,
            elapsed_sec: data?.elapsed_sec,
          });
          if (data?.error) setError(String(data.error));
        } else {
          throw streamErr;
        }
      }
      setLastAt(new Date().toLocaleTimeString("en-IN", { hour12: true }));
    } catch (err: any) {
      setError(err?.message || "Scan failed");
      console.error("Surprise scan failed", err);
    } finally {
      setLoading(false);
      scanAbort.current = null;
    }
  }, []);

  useEffect(() => {
    fetchSurpriseStocks(false);
    pollPremarket();
    return () => {
      stopPmPoll();
      try {
        scanAbort.current?.abort();
      } catch {}
    };
  }, [fetchSurpriseStocks, pollPremarket]);

  const pmPct = Math.min(100, Math.max(0, Number(pmProgress?.percent ?? 0)));

  const fetchSurpriseHealth = useCallback(async () => {
    setHealthLoading(true);
    try {
      const data = await api.surpriseAudit();
      setHealthData(data);
    } catch (e: any) {
      setHealthData({
        health_score: 0,
        total_tracked: 0,
        missing_data: 0,
        incomplete_stocks: [],
        message: e?.message || "Audit failed",
      });
    } finally {
      setHealthLoading(false);
    }
  }, []);

  const handleRepairSingle = useCallback(
    async (symbol: string) => {
      setPatchingSymbol(symbol);
      try {
        await api.surpriseRepairBatch(1, symbol);
        await fetchSurpriseHealth();
      } catch (e: any) {
        setPmError(e?.message || `Repair failed for ${symbol}`);
      } finally {
        setPatchingSymbol(null);
      }
    },
    [fetchSurpriseHealth]
  );

  const handleRepairBatchMissing = useCallback(async () => {
    setBatchRepairBusy(true);
    try {
      await api.surpriseRepairBatch(15);
      await fetchSurpriseHealth();
    } catch (e: any) {
      setPmError(e?.message || "Batch repair failed");
    } finally {
      setBatchRepairBusy(false);
    }
  }, [fetchSurpriseHealth]);

  const runMarketAwareQuoteFeed = useCallback(async (force = false) => {
    setQuoteFeedBusy(true);
    setPmError(null);
    try {
      const res = await api.runSurprisePremarketFeed(force);
      if (res?.message) {
        setPmError(null);
      }
      await fetchSurpriseHealth();
    } catch (e: any) {
      setPmError(e?.message || "Quote feed failed");
    } finally {
      setQuoteFeedBusy(false);
    }
  }, [fetchSurpriseHealth]);

  useEffect(() => {
    void fetchSurpriseHealth();
  }, [fetchSurpriseHealth]);

  const scPct = Math.min(100, Math.max(0, Number(scanProg?.percent ?? 0)));

  return (
    <div className="rounded-xl border border-slate bg-graphite p-4 sm:p-6">
      <div className="flex flex-wrap justify-between items-start gap-4 mb-5">
        <div>
          <h2 className="font-display text-xl text-emerald-400/95">
            ⚡ Surprise Momentum Stocks
          </h2>
          <p className="font-mono text-[11px] text-mist/60 mt-1 max-w-xl">
            High RVOL / ORB vs Neon baselines. Live quotes only for ticks; static from premarket.
          </p>
          {meta && (
            <p className="font-mono text-[10px] text-mist/45 mt-2">
              baselines {meta.static_loaded ?? "—"} · quotes {meta.quotes_ok ?? "—"}/
              {meta.universe_scanned ?? "—"} · {meta.elapsed_sec ?? "—"}s
              {lastAt ? ` · ${lastAt}` : ""}
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={startPremarket}
            disabled={pmRunning}
            className="font-mono text-xs px-4 py-2 rounded-lg bg-amber-600/25 text-amber-200 border border-amber-500/40 hover:bg-amber-600/40 transition disabled:opacity-50"
          >
            {pmRunning ? "Premarket running…" : "🛠 Run Premarket Feed"}
          </button>
          <button
            type="button"
            onClick={() => void runMarketAwareQuoteFeed(false)}
            disabled={quoteFeedBusy || pmRunning}
            className="font-mono text-xs px-4 py-2 rounded-lg bg-sky-600/25 text-sky-200 border border-sky-500/40 hover:bg-sky-600/40 transition disabled:opacity-50"
          >
            {quoteFeedBusy ? "Bulk quotes…" : "📡 Bulk Quote Feed (50×)"}
          </button>
          <button
            type="button"
            onClick={() => fetchSurpriseStocks(true)}
            disabled={loading}
            className="font-mono text-xs px-4 py-2 rounded-lg bg-emerald-600/30 text-emerald-300 border border-emerald-500/40 hover:bg-emerald-600/45 transition disabled:opacity-50"
          >
            {loading ? "Scanning…" : "Refresh Scan"}
          </button>
          <button
            type="button"
            onClick={handleSearchBuysFromSurprise}
            disabled={stocks.length === 0 || sniperLoading}
            className="font-mono text-xs bg-emerald-600/25 border border-emerald-500/50 text-emerald-200 rounded-lg px-3 py-2 transition hover:bg-emerald-600/35 disabled:opacity-50 shadow-lg shadow-emerald-900/20"
          >
            {sniperLoading ? "Sniping…" : "🎯 Search for Buy Stocks (1-4)"}
          </button>

        </div>
      </div>

      {(pmRunning || (pmProgress && pmProgress.stage && pmProgress.stage !== "idle")) && (
        <div className="mb-4 rounded-xl border border-amber-500/30 bg-amber-500/5 px-4 py-3">
          <div className="flex flex-wrap justify-between gap-2 mb-2">
            <p className="font-mono text-[11px] text-amber-200">
              Premarket · {pmProgress?.stage || "—"}
            </p>
            <p className="font-mono text-[10px] text-mist/60">
              {pmProgress?.processed ?? 0}/{pmProgress?.total ?? "—"} · {formatEta(pmProgress?.elapsed_sec)} · ETA{" "}
              {formatEta(pmProgress?.eta_sec)}
            </p>
          </div>
          <div className="h-2 rounded-full bg-ink/60 overflow-hidden border border-slate/50">
            <div
              className="h-full bg-gradient-to-r from-amber-500/80 to-emerald-500/80 transition-all duration-500"
              style={{ width: `${pmPct}%` }}
            />
          </div>
          <p className="font-mono text-[10px] text-mist/50 mt-1.5">
            {pmPct}% · {pmProgress?.message || "…"}
            {wsConnected && <span className="text-emerald-400/80 ml-2">● live</span>}
            {!!yfRateLimit?.waiters && (
              <span className="text-amber-400/80 ml-2">
                · queued behind shared rate limit ({yfRateLimit.waiters} waiting)
              </span>
            )}
          </p>
        </div>
      )}

      {/* ── Recent IPO Listings ── */}
      <IpoSection
        ipoList={ipoList}
        generatedAt={ipoGeneratedAt}
        scanning={ipoScanning}
        progress={ipoProgress}
        error={ipoError}
        wsConnected={wsConnected}
        onScan={startIpoScan}
        onOpenSuggestion={openIpoSuggestion}
        onSelect={onSelect}
        addOpen={ipoAddOpen}
        setAddOpen={setIpoAddOpen}
        form={ipoForm}
        setForm={setIpoForm}
        addBusy={ipoAddBusy}
        onSubmitAdd={submitIpoAdd}
      />

      {/* Live surprise scan pipeline (same idea as Market Scan) */}
      {(loading || (scanProg && scanProg.total > 0 && scanProg.percent < 100)) && (
        <div className="mb-4 rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-4 py-3">
          <div className="flex flex-wrap justify-between gap-2 mb-2">
            <p className="font-mono text-[11px] text-emerald-300">
              Surprise scan · {scanProg?.hits ?? 0} hits
            </p>
            <p className="font-mono text-[10px] text-mist/60">
              {scanProg?.processed ?? 0}/{scanProg?.total ?? "—"} · quotes {scanProg?.quotes_ok ?? 0} ·{" "}
              {formatEta(scanProg?.elapsed)} · ETA {formatEta(scanProg?.eta_sec)}
            </p>
          </div>
          <div className="h-2 rounded-full bg-ink/60 overflow-hidden border border-slate/50">
            <div
              className="h-full bg-gradient-to-r from-sky-500/80 to-emerald-500/80 transition-all duration-300"
              style={{ width: `${scPct}%` }}
            />
          </div>
          <p className="font-mono text-[10px] text-mist/50 mt-1.5">{scPct}% complete</p>
        </div>
      )}

      {pmError && (
        <div className="mb-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 font-mono text-[11px] text-rose-200">
          Premarket: {pmError}
        </div>
      )}
      {error && (
        <div className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 font-mono text-[11px] text-amber-200">
          {error}
        </div>
      )}


      {/* Premarket / Surprise Feed Health */}
      <div className="mt-2 mb-6 border border-slate bg-ink/40 rounded-xl p-4 sm:p-5">
        <div className="flex flex-wrap justify-between items-center gap-3 mb-4">
          <div>
            <h3 className="font-mono text-sm font-bold text-paper">Premarket Feed Health</h3>
            <p className="font-mono text-[10px] text-mist/50 mt-0.5">
              Audit live quote cache and refresh safely (2h open / durable closed).
              {healthData?.market_open != null
                ? healthData.market_open
                  ? " · Market OPEN"
                  : " · Market CLOSED"
                : ""}
              {healthData?.source ? ` · source: ${healthData.source}` : ""}
            </p>
          </div>
          <button
            type="button"
            onClick={() => void fetchSurpriseHealth()}
            disabled={healthLoading}
            className="font-mono text-xs px-3 py-1.5 bg-graphite text-mist rounded-lg border border-slate hover:bg-slate/40 transition disabled:opacity-50"
          >
            {healthLoading ? "Auditing…" : "🔄 Refresh Audit"}
          </button>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="p-3 rounded-lg bg-graphite/80 border border-slate/60">
            <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">Health score</div>
            <div
              className={`font-mono text-xl font-bold mt-1 ${
                (healthData?.health_score ?? 0) >= 90
                  ? "text-emerald-400"
                  : (healthData?.health_score ?? 0) >= 70
                    ? "text-amber-300"
                    : "text-rose-400"
              }`}
            >
              {healthData?.health_score ?? "—"}%
            </div>
          </div>
          <div className="p-3 rounded-lg bg-graphite/80 border border-slate/60">
            <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">Total tracked</div>
            <div className="font-mono text-xl font-bold text-paper mt-1">
              {healthData?.total_tracked ?? "—"}
            </div>
          </div>
          <div className="p-3 rounded-lg bg-graphite/80 border border-slate/60">
            <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">Fully populated</div>
            <div className="font-mono text-xl font-bold text-emerald-400 mt-1">
              {healthData?.fully_populated ?? "—"}
            </div>
          </div>
          <div className="p-3 rounded-lg bg-graphite/80 border border-slate/60">
            <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">Missing data</div>
            <div className="font-mono text-xl font-bold text-rose-400 mt-1">
              {healthData?.missing_data ?? "—"}
            </div>
          </div>
        </div>
        {healthData?.message && (
          <p className="font-mono text-[10px] text-mist/50 mt-3">{healthData.message}</p>
        )}

        <div className="flex flex-wrap gap-2 mt-4">
          <button
            type="button"
            onClick={() => void handleRepairBatchMissing()}
            disabled={
              batchRepairBusy ||
              healthLoading ||
              (healthData?.missing_data ?? 0) === 0
            }
            className="font-mono text-xs px-3 py-1.5 rounded-lg bg-rose-600/20 text-rose-200 border border-rose-500/40 hover:bg-rose-600/35 transition disabled:opacity-50"
          >
            {batchRepairBusy ? "Repairing…" : "⚡ Auto-Repair Missing (15)"}
          </button>
        </div>

        {(healthData?.incomplete_stocks?.length ?? 0) > 0 && (
          <div className="mt-4 overflow-x-auto rounded-lg border border-slate/60">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="text-mist/50 border-b border-slate/60 font-mono text-[10px] uppercase tracking-wider">
                  <th className="py-2 px-3">Symbol</th>
                  <th className="py-2 px-3">Price</th>
                  <th className="py-2 px-3">Missing</th>
                  <th className="py-2 px-3 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {(healthData?.incomplete_stocks || []).map((stock) => (
                  <tr key={stock.symbol} className="border-b border-slate/40 hover:bg-ink/40">
                    <td className="py-2 px-3 font-mono font-semibold text-paper">{stock.symbol}</td>
                    <td className="py-2 px-3 font-mono text-rose-400">0 (missing)</td>
                    <td className="py-2 px-3">
                      {(stock.missing_fields || ["price"]).map((m) => (
                        <span
                          key={m}
                          className="inline-block bg-rose-900/40 text-rose-300 px-1.5 py-0.5 rounded mr-1 text-[10px] uppercase"
                        >
                          {m}
                        </span>
                      ))}
                    </td>
                    <td className="py-2 px-3 text-right">
                      <button
                        type="button"
                        onClick={() => void handleRepairSingle(stock.symbol)}
                        disabled={patchingSymbol === stock.symbol || batchRepairBusy}
                        className="font-mono text-[11px] px-2 py-1 bg-graphite text-mist rounded border border-slate hover:bg-slate/40 disabled:opacity-50"
                      >
                        {patchingSymbol === stock.symbol ? "Patching…" : "Repair"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left border-collapse">
          <thead>
            <tr className="border-b border-slate/80 text-mist/50 font-mono text-[10px] uppercase tracking-wider">
              <th className="py-2.5 px-3">Symbol</th>
              <th className="py-2.5 px-3">Tier</th>
              <th className="py-2.5 px-3">Score</th>
              <th className="py-2.5 px-3">Price</th>
              <th className="py-2.5 px-3">Change %</th>
              <th className="py-2.5 px-3">RVOL</th>
              <th className="py-2.5 px-3">Trigger</th>
              <th className="py-2.5 px-3">Target +5%</th>
              <th className="py-2.5 px-3">Trail (VWAP)</th>
            </tr>
          </thead>
          <tbody>
            {stocks.map((s) => {
              const isBuilding = s.tier === "building";
              return (
              <tr key={s.symbol} className="border-b border-slate/40 hover:bg-ink/40 text-sm transition">
                <td className="py-2.5 px-3">
                  <button
                    type="button"
                    onClick={() => onSelect?.(s.symbol)}
                    className="font-mono font-semibold text-paper hover:text-emerald-300"
                  >
                    {s.symbol}
                  </button>
                </td>
                <td className="py-2.5 px-3">
                  <span
                    className={
                      "font-mono text-[10px] px-2 py-0.5 rounded border " +
                      (isBuilding
                        ? "bg-amber-950/60 text-amber-300 border-amber-700/40"
                        : "bg-emerald-950/60 text-emerald-300 border-emerald-700/40")
                    }
                  >
                    {isBuilding ? "Building" : "Breakout"}
                  </span>
                </td>
                <td className="py-2.5 px-3">
                  <span className="font-mono text-[11px] px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-300 border border-emerald-700/50">
                    {s.score}/100
                  </span>
                </td>
                <td className="py-2.5 px-3 font-mono text-xs text-paper">
                  {formatInrPrice(s as any, null, "—")}
                </td>
                <td className="py-2.5 px-3 font-mono text-xs font-semibold text-emerald-400">
                  {Number(s.change_pct) >= 0 ? "+" : ""}
                  {Number(s.change_pct).toFixed(2)}%
                </td>
                <td className="py-2.5 px-3 font-mono text-xs text-amber-300 font-bold">
                  {Number(s.rvol).toFixed(2)}x
                  {typeof s.rvol_slope === "number" && s.rvol_slope > 0 && (
                    <span className="text-emerald-400 ml-1">↑{s.rvol_slope.toFixed(2)}</span>
                  )}
                </td>
                <td className="py-2.5 px-3">
                  <span className="font-mono text-[10px] px-2 py-0.5 rounded bg-indigo-950/60 text-indigo-300 border border-indigo-700/40">
                    {s.trigger_type}
                  </span>
                </td>
                <td className="py-2.5 px-3 font-mono text-xs text-emerald-300/90">
                  ₹{Number(s.target_1).toFixed(2)}
                </td>
                <td className="py-2.5 px-3 font-mono text-xs text-rose-300/90">
                  ₹{Number(s.trailing_stop).toFixed(2)}
                </td>
              </tr>
              );
            })}
            {stocks.length === 0 && !loading && (
              <tr>
                <td colSpan={9} className="py-10 text-center font-mono text-xs text-mist/45">
                  No surprise breakouts or early-building setups right now.
                </td>
              </tr>
            )}
            {loading && stocks.length === 0 && (
              <tr>
                <td colSpan={9} className="py-10 text-center font-mono text-xs text-mist/50">
                  Scanning universe… {scanProg ? `${scanProg.processed}/${scanProg.total}` : ""}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <BuySniperModal
        isOpen={sniperOpen}
        onClose={() => setSniperOpen(false)}
        suggestions={suggestions}
        loading={sniperLoading}
        error={sniperError}
        onSelectSymbol={onSelect}
      />
    </div>
  );
}

// ── Recent IPO Listings subsection ──────────────────────────────────────
// Scores recently-listed (and listing-today) NSE IPOs for a short-term
// buy/sell decision. "Prepare to Buy" / "Buy Now" rows open the same
// BuySniperModal used everywhere else in this tab — the entry/target/stop
// numbers come straight from ipo_scanner.py's buy_suggestion, computed
// server-side so this stays fast even the moment a stock lists.
function decisionBadgeClass(decision?: string): string {
  const d = (decision || "").toUpperCase();
  if (d === "BUY NOW") return "bg-signal-buy/20 text-signal-buy border-signal-buy/40";
  if (d === "PREPARE TO BUY") return "bg-signal-prepare/20 text-signal-prepare border-signal-prepare/40";
  if (d === "SELL") return "bg-rose-500/20 text-rose-300 border-rose-500/40";
  if (d === "DO NOT BUY") return "bg-mist/10 text-mist/70 border-slate/50";
  return "bg-amber-500/10 text-amber-300 border-amber-500/30"; // HOLD
}

function stageLabel(ipo: any): { text: string; tone: string } {
  if (ipo.stage === "upcoming") return { text: `Lists ${ipo.listing_date}`, tone: "text-mist/60" };
  if (ipo.stage === "pre_listing") return { text: "Lists today · pre-open", tone: "text-amber-300" };
  if (ipo.stage === "listing_day") return { text: "Listing day · live", tone: "text-emerald-300" };
  if (ipo.stage === "listed") return { text: `Day ${ipo.days_since_listing}`, tone: "text-mist/60" };
  return { text: ipo.stage || "—", tone: "text-mist/50" };
}

function IpoSection({
  ipoList,
  generatedAt,
  scanning,
  progress,
  error,
  wsConnected,
  onScan,
  onOpenSuggestion,
  onSelect,
  addOpen,
  setAddOpen,
  form,
  setForm,
  addBusy,
  onSubmitAdd,
}: {
  ipoList: any[];
  generatedAt: string | null;
  scanning: boolean;
  progress: { processed?: number; total?: number; message?: string } | null;
  error: string | null;
  wsConnected: boolean;
  onScan: () => void;
  onOpenSuggestion: (ipo: any) => void;
  onSelect?: (symbol: string) => void;
  addOpen: boolean;
  setAddOpen: (v: boolean) => void;
  form: { symbol: string; issue_price: string; listing_date: string; subscription_times: string };
  setForm: (v: any) => void;
  addBusy: boolean;
  onSubmitAdd: () => void;
}) {
  return (
    <section className="mb-6 rounded-xl border border-slate/50 bg-ink/40 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
        <div>
          <p className="font-mono text-sm text-paper">🆕 Recent IPO Listings</p>
          <p className="font-mono text-[10px] text-mist/50">
            Short-term buy/sell read on recently-listed &amp; listing-today NSE IPOs
            {generatedAt && <span> · updated {new Date(generatedAt).toLocaleTimeString("en-IN")}</span>}
            {wsConnected && <span className="text-emerald-400/80 ml-1">● live</span>}
          </p>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => setAddOpen(!addOpen)}
            className="font-mono text-[11px] px-3 py-1.5 rounded-lg bg-slate-500/20 border border-slate-400/40 text-paper hover:bg-slate-500/30"
          >
            + Add IPO
          </button>
          <button
            type="button"
            onClick={onScan}
            disabled={scanning}
            className="font-mono text-[11px] px-3 py-1.5 rounded-lg bg-violet-500/20 border border-violet-400/40 text-violet-100 hover:bg-violet-500/30 disabled:opacity-40"
          >
            {scanning ? "Scanning…" : "Scan IPOs"}
          </button>
        </div>
      </div>

      {addOpen && (
        <div className="mb-3 rounded-lg border border-slate/50 bg-ink/60 p-3 grid grid-cols-2 gap-2">
          <input
            placeholder="Symbol (e.g. XYZLTD)"
            value={form.symbol}
            onChange={(e) => setForm({ ...form, symbol: e.target.value })}
            className="col-span-2 bg-ink/60 border border-slate rounded-lg px-2 py-1.5 font-mono text-xs text-paper placeholder:text-mist/30 outline-none"
          />
          <input
            placeholder="Issue price (₹)"
            value={form.issue_price}
            onChange={(e) => setForm({ ...form, issue_price: e.target.value })}
            className="bg-ink/60 border border-slate rounded-lg px-2 py-1.5 font-mono text-xs text-paper placeholder:text-mist/30 outline-none"
          />
          <input
            type="date"
            value={form.listing_date}
            onChange={(e) => setForm({ ...form, listing_date: e.target.value })}
            className="bg-ink/60 border border-slate rounded-lg px-2 py-1.5 font-mono text-xs text-paper outline-none"
          />
          <input
            placeholder="Subscription (x, optional)"
            value={form.subscription_times}
            onChange={(e) => setForm({ ...form, subscription_times: e.target.value })}
            className="col-span-2 bg-ink/60 border border-slate rounded-lg px-2 py-1.5 font-mono text-xs text-paper placeholder:text-mist/30 outline-none"
          />
          <button
            type="button"
            onClick={onSubmitAdd}
            disabled={addBusy || !form.symbol || !form.issue_price || !form.listing_date}
            className="col-span-2 font-mono text-xs px-3 py-2 rounded-lg bg-emerald-500/20 border border-emerald-400/40 text-emerald-100 hover:bg-emerald-500/30 disabled:opacity-40"
          >
            {addBusy ? "Adding…" : "Add & Scan"}
          </button>
        </div>
      )}

      {scanning && (
        <p className="font-mono text-[10px] text-amber-300/80 mb-2">
          {progress?.processed ?? 0}/{progress?.total ?? "—"} · {progress?.message || "…"}
        </p>
      )}
      {error && <p className="font-mono text-[10px] text-rose-400 mb-2">{error}</p>}

      {ipoList.length === 0 && !scanning ? (
        <p className="font-mono text-[11px] text-mist/45 py-4 text-center">
          No IPOs tracked yet — tap "Scan IPOs" for auto-discovery, or "+ Add IPO" if NSE's feed is blocked.
        </p>
      ) : (
        <div className="space-y-2">
          {ipoList.map((ipo) => {
            const stage = stageLabel(ipo);
            const gainPct = ipo.current_vs_issue_pct;
            return (
              <div
                key={ipo.symbol}
                className="rounded-lg border border-slate/50 bg-ink/50 px-3 py-2.5 flex flex-wrap items-center justify-between gap-2"
              >
                <div className="min-w-[140px]">
                  <button
                    type="button"
                    onClick={() => onSelect?.(ipo.symbol)}
                    className="font-mono text-xs text-paper hover:text-emerald-300 text-left"
                  >
                    {ipo.symbol}
                  </button>
                  <p className={`font-mono text-[10px] ${stage.tone}`}>{stage.text}</p>
                </div>

                <div className="font-mono text-[10px] text-mist/60 min-w-[110px]">
                  <p>Issue ₹{ipo.issue_price}</p>
                  {ipo.current_price != null && <p>CMP ₹{ipo.current_price}</p>}
                </div>

                {gainPct != null && (
                  <p className={`font-mono text-xs min-w-[70px] ${gainPct >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {gainPct >= 0 ? "+" : ""}
                    {gainPct.toFixed(1)}%
                  </p>
                )}

                {ipo.ipo_score != null && (
                  <p className="font-mono text-[10px] text-mist/50 min-w-[70px]">Score {Math.round(ipo.ipo_score)}/100</p>
                )}

                {ipo.pre_listing_advisory && (
                  <p className="font-mono text-[10px] text-amber-300/80 max-w-[200px]">{ipo.pre_listing_advisory}</p>
                )}

                <span className={`font-mono text-[10px] px-2 py-1 rounded-md border ${decisionBadgeClass(ipo.decision)}`}>
                  {ipo.decision || stage.text}
                </span>

                {ipo.buy_suggestion && (
                  <button
                    type="button"
                    onClick={() => onOpenSuggestion(ipo)}
                    className="font-mono text-[11px] px-3 py-1.5 rounded-lg bg-signal-buy/20 border border-signal-buy/40 text-signal-buy hover:bg-signal-buy/30"
                  >
                    {ipo.decision === "BUY NOW" ? "Buy Now →" : "Prepare to Buy →"}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
