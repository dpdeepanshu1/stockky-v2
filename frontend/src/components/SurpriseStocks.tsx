// frontend/src/components/SurpriseStocks.tsx
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api"; // all calls use getApiUrl() — never relative /api on separate Render hosts
import { formatInrPrice, getSafePrice, resolveDisplayPrice } from "../priceDisplay";
import { PriceRoll } from "./ui/motion";   // group 462 (phase 4)
import { BuySniperModal, type BuySuggestion } from "./BuySniperModal";
import { useStockkyRealtime, type RealtimeMessage } from "../useRealtime";
import { KpiStrip } from "./ui/KpiStrip";
import { MarketsFilterBar, NoMatch } from "./MarketsFilterBar";   // E541
import { MarketCard } from "./MarketCard";   // E543
import { Button, Chip, DataTable, Empty, Tabs, type Column } from "./kit";   // E543, E538 (F3 part 2)
import { RowSpark, useSparklines } from "./RowSpark";   // E557 (B3)
import { SURPRISE_VIEWS, surpriseCard, type SurpriseView } from "../marketCardView";   // E543
import { NO_FILTER, applyMarketFilter, isFilteredEmpty, sortsFor, type MarketAccess, type MarketFilter } from "../marketsFilter";   // E541
import { surpriseKpis, surpriseRowView, type SurpriseRowView } from "./surpriseView";

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

// E538 (F3 part 2): the two tables are kit DataTables. Columns live here so the page body stays about the page.
type SurpriseTableRow = { s: SurpriseStock; v: SurpriseRowView };
const dirClass = (d: "up" | "down" | "flat" | null) => (d === "down" ? "text-signal-sell" : d === "up" ? "text-signal-buy" : "text-paper");
function surpriseColumns(sparkOf: (symbol: string) => number[] | null): Column<SurpriseTableRow>[] {
  return [
    { key: "symbol", header: "Symbol", render: ({ s }) => <b className="font-display tabular-nums text-paper">{s.symbol}</b> },
    { key: "tier", header: "Tier", render: ({ v }) => <span className={`mk-tier ${v.tier.tone}`}><i aria-hidden="true">{v.tier.glyph}</i>{v.tier.label}</span> },
    { key: "score", header: "Score", render: ({ v }) => (v.score ? (
      <span className={`mk-sc ${v.score.tone}`} aria-label={`Score ${v.score.text}, ${v.score.word}`}><b className="num">{v.score.text}</b><small>{v.score.word}</small></span>
    ) : "—") },
    { key: "price", header: "Price", align: "right", render: ({ s }) => <PriceRoll value={resolveDisplayPrice(s as any, null)} text={formatInrPrice(s as any, null, "—")} /> },
    { key: "change", header: "Change %", align: "right", render: ({ v }) => (
      <span className={`font-semibold ${dirClass(v.change.dir)}`}>
        {v.change.dir === "up" && <i className="mk-arrow" aria-hidden="true">▲</i>}
        {v.change.dir === "down" && <i className="mk-arrow" aria-hidden="true">▼</i>}
        {v.change.text}
      </span>
    ) },
    { key: "trend", header: "30d", render: ({ s }) => <RowSpark symbol={s.symbol} closes={sparkOf(s.symbol)} /> },   // E557 (B3): empty when there is no history
    { key: "rvol", header: "RVOL", align: "right", render: ({ v }) => (
      <span className="font-bold text-signal-hold">{v.rvol}{v.slope && <span className="text-signal-buy ml-1">{v.slope}</span>}</span>
    ) },
    { key: "trigger", header: "Trigger", render: ({ s }) => <Chip as="span">{s.trigger_type || "—"}</Chip> },
    { key: "target", header: "Target +5%", align: "right", render: ({ v }) => <span className="text-signal-buy/90">{v.target}</span> },
    { key: "stop", header: "Trail (VWAP)", align: "right", render: ({ v }) => <span className="text-signal-sell/90">{v.stop}</span> },
  ];
}
type HealthRow = { symbol: string; missing_fields?: string[]; current_price?: number };
function healthColumns(patching: string | null, batchBusy: boolean, repair: (symbol: string) => void): Column<HealthRow>[] {
  return [
    { key: "symbol", header: "Symbol", render: (r) => <b className="font-display tabular-nums text-paper">{r.symbol}</b> },
    { key: "price", header: "Price", align: "right", render: (r) => <span className="font-display tabular-nums text-signal-sell">{r.current_price && r.current_price > 0 ? `₹${r.current_price.toFixed(2)}` : "—"}</span> },
    { key: "missing", header: "Missing", render: (r) => (
      <span className="inline-flex flex-wrap gap-1">{(r.missing_fields || ["price"]).map((m) => <Chip key={m} as="span" tone="bad" symbol="✗">{m}</Chip>)}</span>
    ) },
    { key: "action", header: "Action", align: "right", render: (r) => (
      <Button variant="ghost" size="sm" onClick={() => repair(r.symbol)} disabled={batchBusy} loading={patching === r.symbol}>
        {patching === r.symbol ? "Patching…" : "Repair"}
      </Button>
    ) },
  ];
}

const SURPRISE_ACCESS: MarketAccess<SurpriseStock> = { symbol: (r) => r.symbol, score: (r) => r.score, change: (r) => (r as { change_pct?: unknown }).change_pct };   // E541

export default function SurpriseStocks({
  onSelect,
}: {
  onSelect?: (symbol: string) => void;
}) {
  const [stocks, setStocks] = useState<SurpriseStock[]>([]);
  const [view, setView] = useState<SurpriseView>("table");   // E543: Cards is a second way to read the same rows; Table stays the default
  const [mfilter, setMfilter] = useState<MarketFilter>(NO_FILTER);   // E541: what is shown only; the scan list itself is untouched
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

  const [auditError, setAuditError] = useState<string | null>(null); // group302
  const [closedNote, setClosedNote] = useState<string | null>(null); // group302: market-closed banner
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
    setClosedNote(null);
    setScanProg({ processed: 0, total: 0, hits: 0, quotes_ok: 0, elapsed: 0, percent: 0 });

    // group302: while the market is closed, opening the tab (or any non-forced refresh) shows the last saved scan
    // instead of starting a ~970-symbol sweep that cannot find anything new (it used to sit at 0/0 on a Saturday
    // next to a 31-hour-old result). "Refresh Scan" passes force=true and still runs a real scan.
    if (!force) {
      try {
        const sess = await api.getMarketSession();
        if (sess && (sess.phase === "closed" || sess.phase === "holiday")) {
          const data = await api.surpriseScan(false);
          if (ac.signal.aborted) return;
          setStocks(Array.isArray(data?.stocks) ? data.stocks : []);
          setMeta({
            static_loaded: data?.static_loaded,
            quotes_ok: data?.quotes_ok,
            universe_scanned: data?.universe_scanned,
            elapsed_sec: data?.elapsed_sec,
          });
          if (data?.error) setError(String(data.error));
          if (data?.market_closed) {
            const mins = Math.round(Number(data.cache_age_sec || 0) / 60);
            const age = mins >= 120 ? `${Math.round(mins / 60)} h old` : `${mins} min old`;
            setClosedNote(`Market closed - showing the last saved scan (${age}). Press Refresh Scan to run a fresh one.`);
          }
          setScanProg(null);
          setLastAt(new Date().toLocaleTimeString("en-IN", { hour12: true }));
          if (scanAbort.current === ac) {
            setLoading(false);
            scanAbort.current = null;
          }
          return;
        }
      } catch {
        /* session check or saved-result read failed: fall through to the normal scan below */
      }
    }

    setStocks([]);
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
      // BUG FIX: when a new scan supersedes this one (fetchSurpriseStocks
      // called again while this call is still in flight), this call's
      // AbortController is aborted and its stream throws AbortError —
      // caught above with an early `return`, which still runs this
      // `finally`. That happens *asynchronously*, after the new call has
      // already installed its own AbortController on scanAbort.current
      // and started loading. Unconditionally doing `setLoading(false)` /
      // `scanAbort.current = null` here would then stomp on the NEW call's
      // in-flight state — killing its loading spinner and nulling the ref
      // it needs to be abortable. Only clear shared state if this call's
      // controller is still the current one (i.e. nothing newer started).
      if (scanAbort.current === ac) {
        setLoading(false);
        scanAbort.current = null;
      }
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
    setAuditError(null);
    try {
      const data = await api.surpriseAudit();
      setHealthData(data);
    } catch (e: any) {
      // group302: keep the last good numbers (a failed audit is not "health 0") and show the reason with a Retry.
      setAuditError(e?.message || "Audit failed");
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
          setRepairMsg({ ok: true, text: `Repaired ${symbol} — price updated.` });
        } else {
          setRepairMsg({ ok: true, text: res?.message || `${symbol}: nothing missing.` });
        }
        // Refresh both the health audit AND the stocks table so the repaired
        // price appears in the row immediately without a manual re-scan.
        await fetchSurpriseHealth();
        await fetchSurpriseStocks(false);
      } catch (e: any) {
        setRepairMsg({ ok: false, text: e?.message || `Repair failed for ${symbol}` });
      } finally {
        setPatchingSymbol(null);
      }
    },
    [fetchSurpriseHealth, fetchSurpriseStocks]
  );

  const handleRepairBatchMissing = useCallback(async () => {
    setBatchRepairBusy(true);
    setRepairMsg(null);
    try {
      // Repair ALL missing — use the full incomplete_stocks count.
      // Backend accepts up to 100 per call (le=100) — clamp to it, or an
      // incomplete count over 100 gets rejected with a 422 instead of repaired.
      const totalMissing = healthData?.incomplete_stocks?.length ?? healthData?.missing_data ?? 15;
      const limit = Math.min(100, Math.max(15, totalMissing));
      const res = await api.surpriseRepairBatch(limit);
      const repaired: string[] = res?.repaired || [];
      if (res?.status === "error") {
        setRepairMsg({ ok: false, text: res.error || "Batch repair failed" });
      } else if (repaired.length > 0) {
        setRepairMsg({ ok: true, text: `Repaired ${repaired.length} symbol(s): ${repaired.join(", ")}` });
      } else {
        setRepairMsg({ ok: true, text: res?.message || "Nothing needed repair." });
      }
      // Refresh both audit and stocks table.
      await fetchSurpriseHealth();
      await fetchSurpriseStocks(false);
    } catch (e: any) {
      setRepairMsg({ ok: false, text: e?.message || "Batch repair failed" });
    } finally {
      setBatchRepairBusy(false);
    }
  }, [fetchSurpriseHealth, fetchSurpriseStocks, healthData]);

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

  const shownStocks = applyMarketFilter(stocks, SURPRISE_ACCESS, mfilter);   // E541
  const sparkOf = useSparklines(shownStocks.map((s) => s.symbol));   // E557 (B3)

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
          {closedNote && (
            <p className="font-display tabular-nums text-[11px] text-signal-hold mt-2" role="status">
              {closedNote}
            </p>
          )}
          {meta && (
            <p className="font-display tabular-nums text-[10px] text-mist/45 mt-2">
              baselines {meta.static_loaded ?? "—"} · quotes {meta.quotes_ok ?? "—"}/
              {meta.universe_scanned ?? "—"} · {meta.elapsed_sec ?? "—"}s
              {lastAt ? ` · ${lastAt}` : ""}
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="soft" size="sm" onClick={startPremarket} loading={pmRunning}>
            {pmRunning ? "Premarket running…" : "🛠 Run Premarket Feed"}
          </Button>
          <Button variant="soft" size="sm" onClick={() => void runMarketAwareQuoteFeed(false)} disabled={quoteFeedBusy || pmRunning} loading={quoteFeedBusy}>
            {quoteFeedBusy ? "Bulk quotes…" : "📡 Bulk Quote Feed (50×)"}
          </Button>
          <Button variant="primary" size="sm" onClick={() => fetchSurpriseStocks(true)} loading={loading}>
            {loading ? "Scanning…" : "Refresh Scan"}
          </Button>
          <Button
            variant="danger"
            size="sm"
            onClick={() => void stopSurprise()}
            disabled={!busy}
            loading={stopBusy}
            title={
              busy
                ? "Halt after the current symbol — results already collected are kept"
                : "Nothing is running"
            }
          >
            {stopBusy ? "Stopping…" : "⏹ Stop"}
          </Button>
          <Button variant="soft" size="sm" onClick={handleSearchBuysFromSurprise} disabled={stocks.length === 0} loading={sniperLoading}>
            {sniperLoading ? "Sniping…" : "🎯 Search for Buy Stocks (1-4)"}
          </Button>
          <Button variant="soft" size="sm" onClick={() => void notifyTopPicks()} loading={notifyBusy} title="Send the current top 5 Surprise Momentum picks to Telegram">
            {notifyBusy ? "Sending…" : "📨 Send Top 5 to Telegram"}
          </Button>

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
          <Button variant="ghost" size="sm" onClick={() => void fetchSurpriseHealth()} loading={healthLoading}>
            {healthLoading ? "Auditing…" : auditError ? "🔄 Retry Audit" : "🔄 Refresh Audit"}
          </Button>
        </div>
        {auditError && !healthLoading && (
          <p className="font-display tabular-nums text-[11px] text-signal-sell mb-3" role="alert">
            {auditError}
          </p>
        )}

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
          <Button
            variant="soft"
            size="sm"
            onClick={() => void handleRepairBatchMissing()}
            loading={batchRepairBusy}
            disabled={
              healthLoading ||
              // See FeedHealthPanel: enable on the full repairable set
              // (incomplete_stocks), not the price-only missing_data count.
              ((healthData?.incomplete_stocks?.length ?? healthData?.missing_data ?? 0) === 0)
            }
          >
            {batchRepairBusy ? "Repairing…" : `⚡ Repair All Missing (${Math.min(100, healthData?.incomplete_stocks?.length ?? healthData?.missing_data ?? 0)})`}
          </Button>
        </div>
        {repairMsg && (
          <p className={`font-display tabular-nums text-[11px] mt-2 ${repairMsg.ok ? "text-signal-buy/80" : "text-signal-sell/80"}`}>
            {repairMsg.text}
          </p>
        )}

        {(healthData?.incomplete_stocks?.length ?? 0) > 0 && (
          <div className="mt-4">
            <DataTable
              dense
              columns={healthColumns(patchingSymbol, batchRepairBusy, (sym) => void handleRepairSingle(sym))}
              rows={healthData?.incomplete_stocks || []}
              rowKey={(stock) => stock.symbol}
            />
          </div>
        )}
      </div>

      <KpiStrip items={surpriseKpis(stocks, meta)} label="Surprise Momentum summary" />
      <MarketsFilterBar label="Surprise Momentum" query={mfilter.query} onQuery={(query) => setMfilter((f) => ({ ...f, query }))} sort={mfilter.sort}
        onSort={(sort) => setMfilter((f) => ({ ...f, sort }))} sorts={sortsFor(SURPRISE_ACCESS)} shown={shownStocks.length} total={stocks.length}
        extra={<Tabs label="Surprise Momentum view" items={SURPRISE_VIEWS} value={view} onChange={setView} />} />

      {view === "cards" && (
        <>
          {isFilteredEmpty(shownStocks.length, stocks.length) && <NoMatch query={mfilter.query} onClear={() => setMfilter(NO_FILTER)} />}
          {stocks.length === 0 && (
            <p className="mk-nomatch" role="status">{loading ? `Scanning universe… ${scanProg ? `${scanProg.processed}/${scanProg.total}` : ""}` : "No surprise breakouts or early-building setups right now."}</p>
          )}
          <div className="mk-grid">
            {shownStocks.map((s) => (
              <MarketCard key={s.symbol} card={surpriseCard(s, surpriseRowView(s))} onSelect={onSelect}
                price={<PriceRoll value={resolveDisplayPrice(s as any, null)} text={formatInrPrice(s as any, null, "—")} />} />
            ))}
          </div>
        </>
      )}

      {view === "table" && (
        <DataTable
          columns={surpriseColumns(sparkOf)}
          rows={shownStocks.map((s) => ({ s, v: surpriseRowView(s) }))}   // E161: formatted text, dashes for missing values
          rowKey={(r) => r.s.symbol}
          rowClass={(r) => `mk-srow ${r.v.change.dir ?? "flat"}`}
          onRowClick={onSelect ? (r) => onSelect(r.s.symbol) : undefined}
          empty={
            isFilteredEmpty(shownStocks.length, stocks.length)
              ? <NoMatch query={mfilter.query} onClear={() => setMfilter(NO_FILTER)} />
              : loading
                ? <Empty title={`Scanning universe… ${scanProg ? `${scanProg.processed}/${scanProg.total}` : ""}`} />
                : <Empty title="No surprise breakouts or early-building setups right now." />
          }
        />
      )}

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
