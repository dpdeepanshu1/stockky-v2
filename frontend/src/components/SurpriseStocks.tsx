// frontend/src/components/SurpriseStocks.tsx
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api"; // all calls use getApiUrl() — never relative /api on separate Render hosts
import { formatInrPrice, getSafePrice } from "../priceDisplay";
import { BuySniperModal, type BuySuggestion } from "./BuySniperModal";

export interface SurpriseStock {
  symbol: string;
  score: number;
  price: number;
  change_pct: number;
  rvol: number;
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
      await api.surprisePremarket();
      stopPmPoll();
      pollRef.current = window.setInterval(pollPremarket, 2000);
      await pollPremarket();
    } catch (e: any) {
      setPmRunning(false);
      setPmError(e?.message || "Failed to start premarket");
      stopPmPoll();
    }
  };


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
            {quoteFeedBusy ? "Quote feed…" : "📡 Live Quote Cache"}
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
          </p>
        </div>
      )}

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
            {stocks.map((s) => (
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
            ))}
            {stocks.length === 0 && !loading && (
              <tr>
                <td colSpan={8} className="py-10 text-center font-mono text-xs text-mist/45">
                  No surprise breakouts meeting score ≥60 / volume thresholds right now.
                </td>
              </tr>
            )}
            {loading && stocks.length === 0 && (
              <tr>
                <td colSpan={8} className="py-10 text-center font-mono text-xs text-mist/50">
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
