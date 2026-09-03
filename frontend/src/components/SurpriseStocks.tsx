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
  const [repairMsg, setRepairMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [stopBusy, setStopBusy] = useState(false);
  const [notifyBusy, setNotifyBusy] = useState(false);
  const [notifyMsg, setNotifyMsg] = useState<string | null>(null);
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
        // Surprise's own tiering already gates what reaches this tab: the
        // "building" tier (early, pre-breakout setups — the whole point of
        // this tab) can score as low as SURPRISE_BUILDING_MIN_SCORE=30 on
        // the momentum scale, well under buy_sniper's default 55+
        // conviction floor (which is calibrated for Hot Picks' blended
        // technical+fundamental score, a different scale). Re-applying
        // that flat 55 here silently rejected almost every building-tier
        // row — the sniper always came back empty even on a tab full of
        // real picks. Match Surprise's own floor instead of a borrowed one.
        min_conviction: 30,
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

  // Stop for the Surprise tab. /surprise/stop has existed on the backend but
  // nothing here ever called it, so a long premarket/waterfall run could only be
  // waited out. The same flag halts the premarket baseline job and the live
  // scan's waterfall-fill loop, and anything already computed is kept — so this
  // is "stop early with partial results", not "cancel and lose the work".
  const stopSurprise = useCallback(async () => {
    setStopBusy(true);
    try {
      const res = await api.surpriseStop();
      if (res?.ok === false) setError(res?.error || "Stop request failed");
      // Abort the SSE stream too: the flag stops the server-side loop, but the
      // open stream would otherwise sit there until the backend closes it.
      try {
        scanAbort.current?.abort();
      } catch {}
      await pollPremarket();
    } catch (err: any) {
      setError(err?.message || "Stop request failed");
    } finally {
      setStopBusy(false);
    }
  }, [pollPremarket]);

  // Manual "send top 5 picks to Telegram" — re-scans server-side (reusing
  // cached static/quote data, not a full re-fetch) and forwards the top N
  // by score to notification-scheduler-service's /notify (channel=telegram).
  const notifyTopPicks = useCallback(async () => {
    setNotifyBusy(true);
    setNotifyMsg(null);
    try {
      const res = await api.surpriseNotifyTopPicks(5);
      if (res?.sent) {
        setNotifyMsg(`Sent top ${res.count} picks to Telegram ✓`);
      } else if (res?.count === 0) {
        setNotifyMsg(res?.message || "No qualifying picks right now.");
      } else {
        setNotifyMsg(
          res?.notification_result?.note ||
          res?.error ||
          "Could not deliver — check Telegram is configured under Settings → Notifications."
        );
      }
    } catch (err: any) {
      setNotifyMsg(err?.message || "Failed to send top picks");
    } finally {
      setNotifyBusy(false);
    }
  }, []);

  const busy = loading || pmRunning || quoteFeedBusy;

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
      setRepairMsg(null);
      try {
        const res = await api.surpriseRepairBatch(1, symbol);
        if (res?.status === "error" || res?.status === "not_found") {
          setRepairMsg({ ok: false, text: res.error || res.message || `Could not repair ${symbol}.` });
        } else if ((res?.repaired || []).length > 0) {
          setRepairMsg({ ok: true, text: `Repaired ${symbol}.` });
        } else {
          setRepairMsg({ ok: true, text: res?.message || `${symbol} already has a price — nothing to repair.` });
        }
        await fetchSurpriseHealth();
      } catch (e: any) {
        setRepairMsg({ ok: false, text: e?.message || `Repair failed for ${symbol}` });
      } finally {
        setPatchingSymbol(null);
      }
    },
    [fetchSurpriseHealth]
  );

  const handleRepairBatchMissing = useCallback(async () => {
    setBatchRepairBusy(true);
    setRepairMsg(null);
    try {
      const res = await api.surpriseRepairBatch(15);
      const repaired: string[] = res?.repaired || [];
      if (res?.status === "error") {
        setRepairMsg({ ok: false, text: res.error || "Batch repair failed" });
      } else if (repaired.length > 0) {
        setRepairMsg({ ok: true, text: `Repaired ${repaired.length} symbol(s): ${repaired.join(", ")}` });
      } else {
        setRepairMsg({ ok: true, text: res?.message || "Nothing needed repair." });
      }
      await fetchSurpriseHealth();
    } catch (e: any) {
      setRepairMsg({ ok: false, text: e?.message || "Batch repair failed" });
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
    <div className="rounded-2xl border border-slate bg-graphite p-4 sm:p-6">
      <div className="flex flex-wrap justify-between items-start gap-4 mb-5">
        <div>
          <h2 className="font-display text-xl text-signal-buy/95">
            ⚡ Surprise Momentum Stocks
          </h2>
          <p className="font-display tabular-nums text-[11px] text-mist/60 mt-1 max-w-xl">
            High RVOL / ORB vs Neon baselines. Live quotes only for ticks; static from premarket.
          </p>
          {meta && (
            <p className="font-display tabular-nums text-[10px] text-mist/45 mt-2">
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
            className="font-display tabular-nums text-xs px-4 py-2 rounded-xl bg-signal-hold/25 text-signal-hold border border-signal-hold/40 hover:bg-signal-hold/40 transition disabled:opacity-50"
          >
            {pmRunning ? "Premarket running…" : "🛠 Run Premarket Feed"}
          </button>
          <button
            type="button"
            onClick={() => void runMarketAwareQuoteFeed(false)}
            disabled={quoteFeedBusy || pmRunning}
            className="font-display tabular-nums text-xs px-4 py-2 rounded-xl bg-signal-prepare/25 text-signal-prepare border border-signal-prepare/40 hover:bg-signal-prepare/40 transition disabled:opacity-50"
          >
            {quoteFeedBusy ? "Bulk quotes…" : "📡 Bulk Quote Feed (50×)"}
          </button>
          <button
            type="button"
            onClick={() => fetchSurpriseStocks(true)}
            disabled={loading}
            className="font-display tabular-nums text-xs px-4 py-2 rounded-xl bg-signal-buy/30 text-signal-buy border border-signal-buy/40 hover:bg-signal-buy/45 transition disabled:opacity-50"
          >
            {loading ? "Scanning…" : "Refresh Scan"}
          </button>
          <button
            type="button"
            onClick={() => void stopSurprise()}
            disabled={!busy || stopBusy}
            title={
              busy
                ? "Halt after the current symbol — results already collected are kept"
                : "Nothing is running"
            }
            className="font-display tabular-nums text-xs px-4 py-2 rounded-xl bg-signal-sell/25 text-white border border-signal-sell/40 hover:bg-signal-sell/40 transition disabled:opacity-40"
          >
            {stopBusy ? "Stopping…" : "⏹ Stop"}
          </button>
          <button
            type="button"
            onClick={handleSearchBuysFromSurprise}
            disabled={stocks.length === 0 || sniperLoading}
            className="font-display tabular-nums text-xs bg-signal-buy/25 border border-signal-buy/50 text-white rounded-xl px-3 py-2 transition hover:bg-signal-buy/35 disabled:opacity-50 shadow-lg shadow-emerald-900/20"
          >
            {sniperLoading ? "Sniping…" : "🎯 Search for Buy Stocks (1-4)"}
          </button>
          <button
            type="button"
            onClick={() => void notifyTopPicks()}
            disabled={notifyBusy}
            title="Send the current top 5 Surprise Momentum picks to Telegram"
            className="font-display tabular-nums text-xs bg-signal-prepare/25 border border-signal-prepare/50 text-signal-prepare rounded-xl px-3 py-2 transition hover:bg-signal-prepare/35 disabled:opacity-50 shadow-lg shadow-sky-900/20"
          >
            {notifyBusy ? "Sending…" : "📨 Send Top 5 to Telegram"}
          </button>

        </div>
        {notifyMsg && (
          <p className="font-display tabular-nums text-[11px] text-white/60 mt-2">{notifyMsg}</p>
        )}
      </div>

      {(pmRunning || (pmProgress && pmProgress.stage && pmProgress.stage !== "idle")) && (
        <div className="mb-4 rounded-2xl border border-signal-hold/30 bg-signal-hold/5 px-4 py-3">
          <div className="flex flex-wrap justify-between gap-2 mb-2">
            <p className="font-display tabular-nums text-[11px] text-signal-hold">
              Premarket · {pmProgress?.stage || "—"}
            </p>
            <p className="font-display tabular-nums text-[10px] text-mist/60">
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
          <p className="font-display tabular-nums text-[10px] text-mist/50 mt-1.5">
            {pmPct}% · {pmProgress?.message || "…"}
            {wsConnected && <span className="text-signal-buy/80 ml-2">● live</span>}
            {!!yfRateLimit?.waiters && (
              <span className="text-signal-hold/80 ml-2">
                · queued behind shared rate limit ({yfRateLimit.waiters} waiting)
              </span>
            )}
          </p>
        </div>
      )}

      {/* Live surprise scan pipeline (same idea as Market Scan) */}
      {(loading || (scanProg && scanProg.total > 0 && scanProg.percent < 100)) && (
        <div className="mb-4 rounded-2xl border border-signal-buy/30 bg-signal-buy/5 px-4 py-3">
          <div className="flex flex-wrap justify-between gap-2 mb-2">
            <p className="font-display tabular-nums text-[11px] text-signal-buy">
              Surprise scan · {scanProg?.hits ?? 0} hits
            </p>
            <p className="font-display tabular-nums text-[10px] text-mist/60">
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
          <p className="font-display tabular-nums text-[10px] text-mist/50 mt-1.5">{scPct}% complete</p>
        </div>
      )}

      {pmError && (
        <div className="mb-4 rounded-xl border border-signal-sell/40 bg-signal-sell/10 px-3 py-2 font-display tabular-nums text-[11px] text-white">
          Premarket: {pmError}
        </div>
      )}
      {error && (
        <div className="mb-4 rounded-xl border border-signal-hold/40 bg-signal-hold/10 px-3 py-2 font-display tabular-nums text-[11px] text-signal-hold">
          {error}
        </div>
      )}


      {/* Premarket / Surprise Feed Health */}
      <div className="mt-2 mb-6 border border-slate bg-ink/40 rounded-2xl p-4 sm:p-5">
        <div className="flex flex-wrap justify-between items-center gap-3 mb-4">
          <div>
            <h3 className="font-display tabular-nums text-sm font-bold text-paper">Premarket Feed Health</h3>
            <p className="font-display tabular-nums text-[10px] text-mist/50 mt-0.5">
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
            className="font-display tabular-nums text-xs px-3 py-1.5 bg-graphite text-mist rounded-xl border border-slate hover:bg-slate/40 transition disabled:opacity-50"
          >
            {healthLoading ? "Auditing…" : "🔄 Refresh Audit"}
          </button>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="p-3 rounded-xl bg-graphite/80 border border-slate/60">
            <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider">Health score</div>
            <div
              className={`font-display tabular-nums text-xl font-bold mt-1 ${
                (healthData?.health_score ?? 0) >= 90
                  ? "text-signal-buy"
                  : (healthData?.health_score ?? 0) >= 70
                    ? "text-signal-hold"
                    : "text-signal-sell"
              }`}
            >
              {healthData?.health_score ?? "—"}%
            </div>
          </div>
          <div className="p-3 rounded-xl bg-graphite/80 border border-slate/60">
            <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider">Total tracked</div>
            <div className="font-display tabular-nums text-xl font-bold text-paper mt-1">
              {healthData?.total_tracked ?? "—"}
            </div>
          </div>
          <div className="p-3 rounded-xl bg-graphite/80 border border-slate/60">
            <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider">Fully populated</div>
            <div className="font-display tabular-nums text-xl font-bold text-signal-buy mt-1">
              {healthData?.fully_populated ?? "—"}
            </div>
          </div>
          <div className="p-3 rounded-xl bg-graphite/80 border border-slate/60">
            <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider">Missing data</div>
            <div className="font-display tabular-nums text-xl font-bold text-signal-sell mt-1">
              {healthData?.missing_data ?? "—"}
            </div>
          </div>
        </div>
        {healthData?.message && (
          <p className="font-display tabular-nums text-[10px] text-mist/50 mt-3">{healthData.message}</p>
        )}

        <div className="flex flex-wrap gap-2 mt-4">
          <button
            type="button"
            onClick={() => void handleRepairBatchMissing()}
            disabled={
              batchRepairBusy ||
              healthLoading ||
              // See FeedHealthPanel: enable on the full repairable set
              // (incomplete_stocks), not the price-only missing_data count.
              ((healthData?.incomplete_stocks?.length ?? healthData?.missing_data ?? 0) === 0)
            }
            className="font-display tabular-nums text-xs px-3 py-1.5 rounded-xl bg-signal-sell/20 text-white border border-signal-sell/40 hover:bg-signal-sell/35 transition disabled:opacity-50"
          >
            {batchRepairBusy ? "Repairing…" : "⚡ Auto-Repair Missing (15)"}
          </button>
        </div>
        {repairMsg && (
          <p className={`font-display tabular-nums text-[11px] mt-2 ${repairMsg.ok ? "text-signal-buy/80" : "text-signal-sell/80"}`}>
            {repairMsg.text}
          </p>
        )}

        {(healthData?.incomplete_stocks?.length ?? 0) > 0 && (
          <div className="mt-4 overflow-x-auto rounded-xl border border-slate/60">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="text-mist/50 border-b border-slate/60 font-display tabular-nums text-[10px] uppercase tracking-wider">
                  <th className="py-2 px-3">Symbol</th>
                  <th className="py-2 px-3">Price</th>
                  <th className="py-2 px-3">Missing</th>
                  <th className="py-2 px-3 text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {(healthData?.incomplete_stocks || []).map((stock) => (
                  <tr key={stock.symbol} className="border-b border-slate/40 hover:bg-ink/40">
                    <td className="py-2 px-3 font-display tabular-nums font-semibold text-paper">{stock.symbol}</td>
                    <td className="py-2 px-3 font-display tabular-nums text-signal-sell">0 (missing)</td>
                    <td className="py-2 px-3">
                      {(stock.missing_fields || ["price"]).map((m) => (
                        <span
                          key={m}
                          className="inline-block bg-signal-sell/40 text-signal-sell px-1.5 py-0.5 rounded mr-1 text-[10px] uppercase"
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
                        className="font-display tabular-nums text-[11px] px-2 py-1 bg-graphite text-mist rounded border border-slate hover:bg-slate/40 disabled:opacity-50"
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
            <tr className="border-b border-slate/80 text-mist/50 font-display tabular-nums text-[10px] uppercase tracking-wider">
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
                    className="font-display tabular-nums font-semibold text-paper hover:text-signal-buy"
                  >
                    {s.symbol}
                  </button>
                </td>
                <td className="py-2.5 px-3">
                  <span
                    className={
                      "font-display tabular-nums text-[10px] px-2 py-0.5 rounded border " +
                      (isBuilding
                        ? "bg-signal-hold/60 text-signal-hold border-signal-hold/40"
                        : "bg-signal-buy/60 text-signal-buy border-signal-buy/40")
                    }
                  >
                    {isBuilding ? "Building" : "Breakout"}
                  </span>
                </td>
                <td className="py-2.5 px-3">
                  <span className="font-display tabular-nums text-[11px] px-2 py-0.5 rounded bg-signal-buy/80 text-signal-buy border border-signal-buy/50">
                    {s.score}/100
                  </span>
                </td>
                <td className="py-2.5 px-3 font-display tabular-nums text-xs text-paper">
                  {formatInrPrice(s as any, null, "—")}
                </td>
                <td className="py-2.5 px-3 font-display tabular-nums text-xs font-semibold text-signal-buy">
                  {Number(s.change_pct) >= 0 ? "+" : ""}
                  {Number(s.change_pct).toFixed(2)}%
                </td>
                <td className="py-2.5 px-3 font-display tabular-nums text-xs text-signal-hold font-bold">
                  {Number(s.rvol).toFixed(2)}x
                  {typeof s.rvol_slope === "number" && s.rvol_slope > 0 && (
                    <span className="text-signal-buy ml-1">↑{s.rvol_slope.toFixed(2)}</span>
                  )}
                </td>
                <td className="py-2.5 px-3">
                  <span className="font-display tabular-nums text-[10px] px-2 py-0.5 rounded bg-signal-prepare/60 text-signal-prepare border border-signal-prepare/40">
                    {s.trigger_type}
                  </span>
                </td>
                <td className="py-2.5 px-3 font-display tabular-nums text-xs text-signal-buy/90">
                  ₹{Number(s.target_1).toFixed(2)}
                </td>
                <td className="py-2.5 px-3 font-display tabular-nums text-xs text-signal-sell/90">
                  ₹{Number(s.trailing_stop).toFixed(2)}
                </td>
              </tr>
              );
            })}
            {stocks.length === 0 && !loading && (
              <tr>
                <td colSpan={9} className="py-10 text-center font-display tabular-nums text-xs text-mist/45">
                  No surprise breakouts or early-building setups right now.
                </td>
              </tr>
            )}
            {loading && stocks.length === 0 && (
              <tr>
                <td colSpan={9} className="py-10 text-center font-display tabular-nums text-xs text-mist/50">
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
