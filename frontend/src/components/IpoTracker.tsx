// frontend/src/components/IpoTracker.tsx
//
// IPO Tracker — standalone left-nav tab (moved OUT of SurpriseStocks.tsx,
// where it used to live as an <IpoSection /> subsection).
//
// Everything IPO-related now lives here: its own state, its own polling, its
// own Scan / Stop controls, the 30-day ⇄ 1-year display window toggle
// (backend: GET /surprise/ipo/list?display_days=), and its OWN
// <IpoFeedHealth /> panel (backend: GET /surprise/ipo/audit) reading
// ipo_static_feed — NOT the shared <DataHealthAudit /> (which audits the
// general stock scan universe and was showing unrelated stock symbols here).
//
// Backend contract (all of this already exists, nothing new was needed):
//   POST /surprise/ipo/scan?background=true   start a scan
//   GET  /surprise/ipo/status                 progress
//   POST /surprise/ipo/stop                   halt after the current symbol
//   GET  /surprise/ipo/list?display_days=N    scored list, N-day window
//   POST /surprise/ipo/add                    manual entry (NSE blocks cloud IPs)
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type IpoAnalysis } from "../api"; // getApiUrl() based — never relative /api
import { BuySniperModal, type BuySuggestion } from "./BuySniperModal";
import { useStockkyRealtime, type RealtimeMessage } from "../useRealtime";
import IpoFeedHealth from "./IpoFeedHealth";

type IpoProgress = {
  status?: string;
  processed?: number;
  total?: number;
  message?: string;
  results_count?: number;
};

type IpoForm = {
  company_name: string;
};

// Display window presets. The backend scan itself always walks the full
// IPO_LOOKBACK_DAYS_HARD_CAP (~365d) universe; display_days only narrows what
// the list endpoint returns, so switching these never triggers a re-scan.
const WINDOW_RECENT = 30;
const WINDOW_3M = 90;
const WINDOW_6M = 180;
const WINDOW_WIDE = 365;

function decisionBadgeClass(decision?: string): string {
  const d = (decision || "").toUpperCase();
  if (d === "BUY NOW") return "bg-signal-buy/20 text-signal-buy border-signal-buy/40";
  if (d === "PREPARE TO BUY") return "bg-signal-prepare/20 text-signal-prepare border-signal-prepare/40";
  if (d === "SELL") return "bg-signal-sell/20 text-signal-sell border-signal-sell/40";
  if (d === "DO NOT BUY") return "bg-mist/10 text-mist/70 border-slate/50";
  return "bg-signal-hold/10 text-signal-hold border-signal-hold/30"; // HOLD
}

function stageLabel(ipo: IpoAnalysis): { text: string; tone: string } {
  if (ipo.stage === "upcoming") return { text: `Lists ${ipo.listing_date}`, tone: "text-mist/60" };
  if (ipo.stage === "pre_listing") return { text: "Lists today · pre-open", tone: "text-signal-hold" };
  if (ipo.stage === "listing_day") return { text: "Listing day · live", tone: "text-signal-buy" };
  if (ipo.stage === "listed") return { text: `Day ${ipo.days_since_listing}`, tone: "text-mist/60" };
  // no_data_yet: Yahoo hasn't indexed this ticker yet — common for fresh NSE SME listings.
  // Show a human-readable label instead of the raw internal stage name.
  if (ipo.stage === "no_data_yet") return { text: "⏳ Awaiting Yahoo data", tone: "text-signal-hold/70" };
  if (ipo.stage === "error") return { text: "⚠ Data error", tone: "text-signal-sell/70" };
  return { text: ipo.stage || "—", tone: "text-mist/50" };
}

// Same small spinner pattern used everywhere else in the app (ScanPanel,
// DecisionCard, BuySniperModal, StockChart) — IpoTracker's busy buttons
// previously only changed their label text with no spinner at all, the
// one visible inconsistency reported against this page.
function BusySpinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block w-3 h-3 rounded-full border-2 border-t-transparent animate-spin ${className}`}
    />
  );
}

export default function IpoTracker({
  onSelect,
}: {
  onSelect?: (symbol: string) => void;
}) {
  const [ipoList, setIpoList] = useState<IpoAnalysis[]>([]);
  const [ipoGeneratedAt, setIpoGeneratedAt] = useState<string | null>(null);
  const [ipoScanning, setIpoScanning] = useState(false);
  const [ipoProgress, setIpoProgress] = useState<IpoProgress | null>(null);
  const [ipoError, setIpoError] = useState<string | null>(null);
  const [ipoAddOpen, setIpoAddOpen] = useState(false);
  const [ipoForm, setIpoForm] = useState<IpoForm>({
    company_name: "",
  });
  const [ipoAddBusy, setIpoAddBusy] = useState(false);
  const [ipoAddNotice, setIpoAddNotice] = useState<{ message: string; suggestions: string[] } | null>(null);
  const [stopBusy, setStopBusy] = useState(false);
  const [displayDays, setDisplayDays] = useState<number>(WINDOW_RECENT);
  const [totalScanned, setTotalScanned] = useState<number | null>(null);
  const ipoPollRef = useRef<number | null>(null);
  const [dbLoadBusy, setDbLoadBusy] = useState(false);
  const [notifyBusy, setNotifyBusy] = useState(false);
  const [notifyMsg, setNotifyMsg] = useState<string | null>(null);

  const [sniperOpen, setSniperOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<BuySuggestion[]>([]);
  const [sniperLoading, setSniperLoading] = useState(false);
  const [sniperError, setSniperError] = useState<string | null>(null);
  const [sniperBusySymbol, setSniperBusySymbol] = useState<string | null>(null);
  const [expandedSymbol, setExpandedSymbol] = useState<string | null>(null);

  // Keep the current window in a ref so the poll/socket callbacks can refetch
  // with the right display_days without being re-created on every toggle.
  const displayDaysRef = useRef(displayDays);
  useEffect(() => {
    displayDaysRef.current = displayDays;
  }, [displayDays]);

  const stopIpoPoll = () => {
    if (ipoPollRef.current != null) {
      window.clearInterval(ipoPollRef.current);
      ipoPollRef.current = null;
    }
  };

  const fetchIpoList = useCallback(async () => {
    try {
      const res = await api.ipoList(displayDaysRef.current);
      setIpoList((res?.results || []) as IpoAnalysis[]);
      setIpoGeneratedAt(res?.generated_at || null);
      setTotalScanned(
        typeof res?.total_scanned === "number" ? res.total_scanned : (res?.results || []).length
      );
    } catch (e: any) {
      console.warn("ipo list", e);
    }
  }, []);

  // 2026-08-26 fix: "click Scan, it flashes then reverts to idle with no
  // visible progress" — this was a real race, not a rendering bug. The
  // shared /ws "jobs_snapshot" channel broadcasts periodically regardless
  // of what this component just did; if a snapshot reflecting the JOB'S
  // PRE-CLICK state (idle/stopped/done from the last run) was already
  // in flight the instant the user clicked Scan, it would arrive right
  // after setIpoScanning(true) and immediately flip it back to false —
  // before the backend's own background thread had even had a chance to
  // set status="running" and broadcast a snapshot that reflects it. The
  // POST itself usually still succeeded; the UI just silently snapped
  // back to idle and made it look like nothing happened. Guarded with a
  // short grace window: any "not running" snapshot arriving within
  // IPO_SCAN_START_GRACE_MS of our own start click is treated as stale
  // and ignored — a multi-hundred-symbol IPO scan cannot legitimately
  // finish that fast, so there's no real completion this could be
  // mistaken for.
  const IPO_SCAN_START_GRACE_MS = 4000;
  const ipoScanStartedAtRef = useRef<number>(0);

  const pollIpoStatus = useCallback(async () => {
    try {
      const st = await api.ipoScanStatus();
      const withinGrace = Date.now() - ipoScanStartedAtRef.current < IPO_SCAN_START_GRACE_MS;
      const running = st?.status === "running" || withinGrace;
      setIpoProgress(st);
      setIpoScanning(running);
      if (!running) {
        stopIpoPoll();
        setStopBusy(false);
        await fetchIpoList();
      }
    } catch (e: any) {
      console.warn("ipo status", e);
    }
  }, [fetchIpoList]);

  // Live job updates over the shared /ws hub (channel "jobs"); the 2s poll
  // above stays as a fallback for when the socket drops.
  const onRealtimeMessage = useCallback(
    (msg: RealtimeMessage) => {
      if (msg.type !== "jobs_snapshot") return;
      const ipoJob = (msg as any).ipo_scan;
      if (!ipoJob) return;
      const withinGrace = Date.now() - ipoScanStartedAtRef.current < IPO_SCAN_START_GRACE_MS;
      const running = ipoJob.status === "running" || withinGrace;
      setIpoScanning(running);
      setIpoProgress(ipoJob);
      if (!running && ipoJob.status && ipoJob.status !== "running") {
        stopIpoPoll();
        setStopBusy(false);
        void fetchIpoList();
      }
    },
    [fetchIpoList]
  );
  const { connected: wsConnected } = useStockkyRealtime(onRealtimeMessage);

  const startIpoScan = useCallback(async (force = false, wipe = false) => {
    setIpoError(null);
    ipoScanStartedAtRef.current = Date.now();
    setIpoScanning(true);
    setIpoProgress({
      message: wipe ? "Wiping stale data, feeding fresh…" : force ? "Starting full re-scan…" : "Starting IPO scan…",
    });
    try {
      if (wipe) {
        await api.wipeAndFeedIpoScan();
      } else if (force) {
        await api.forceIpoScan();
      } else {
        await api.ipoScan();
      }
      stopIpoPoll();
      ipoPollRef.current = window.setInterval(pollIpoStatus, 2000);
      await pollIpoStatus();
    } catch (e: any) {
      setIpoScanning(false);
      setIpoError(e?.message || "Failed to start IPO scan");
      stopIpoPoll();
    }
  }, [pollIpoStatus]);

  // Stop halts the scan after the current symbol; already-analyzed IPOs are
  // persisted, not discarded (same behaviour as the Data Feed / premarket jobs).
  const stopIpoScan = useCallback(async () => {
    setStopBusy(true);
    try {
      await api.ipoStop();
      setIpoProgress((p) => ({ ...(p || {}), message: "Stop requested — finishing current symbol…" }));
      await pollIpoStatus();
    } catch (e: any) {
      setIpoError(e?.message || "Failed to stop IPO scan");
      setStopBusy(false);
    }
  }, [pollIpoStatus]);

  // "Scan IPOs" — a pure DB/cache read (GET /ipo/list), never an upstream
  // call. This used to POST /ipo/scan?force=false, which silently fell
  // through to a real NSE/yfinance scan whenever the results cache had
  // expired even though ipo_static_feed itself was still fresh (fixed
  // separately by widening the cache TTL) — but a "Scan" button should
  // never be able to trigger upstream traffic at all, by construction, not
  // just "rarely now." Use Premarket Feed / Force Rescan for that.
  const loadFromDb = useCallback(async () => {
    setDbLoadBusy(true);
    setIpoError(null);
    try {
      await fetchIpoList();
    } catch (e: any) {
      setIpoError(e?.message || "Failed to load IPO Tracker data");
    } finally {
      setDbLoadBusy(false);
    }
  }, [fetchIpoList]);

  const notifyTop5 = useCallback(async (topN = 5) => {
    setNotifyBusy(true);
    setNotifyMsg(null);
    try {
      const res = await api.ipoNotifyTopPicks(topN);
      if (res.sent) {
        setNotifyMsg(`Sent top ${res.count ?? topN} to Telegram.`);
      } else {
        setNotifyMsg(res.message || res.error || "Telegram didn't confirm delivery — check notification settings.");
      }
    } catch (e: any) {
      setNotifyMsg(e?.message || "Failed to send to Telegram");
    } finally {
      setNotifyBusy(false);
    }
  }, []);

  // Prefer the precomputed buy_suggestion (already scored by the IPO
  // pipeline) when the row has one. Otherwise fall back to an on-demand
  // scan through the same Buy Sniper the Hot Picks / Surprise tabs use,
  // built from this row's own fields — so a row with a real price but no
  // stored suggestion (e.g. HOLD-tier or not yet re-scored) still gets an
  // actionable answer instead of a dead button.
  const openIpoSuggestion = async (ipo: IpoAnalysis) => {
    setSniperOpen(true);
    setSniperError(null);

    if (ipo?.buy_suggestion) {
      setSuggestions([ipo.buy_suggestion as unknown as BuySuggestion]);
      return;
    }

    const price = Number(ipo?.current_price) || 0;
    if (!ipo?.symbol || price <= 0) {
      setSuggestions([]);
      setSniperError("No live price for this IPO yet — can't evaluate a buy setup.");
      return;
    }

    setSuggestions([]);
    setSniperLoading(true);
    setSniperBusySymbol(ipo.symbol);
    try {
      const data = await api.findBuys({
        stocks: [
          {
            symbol: ipo.symbol,
            decision: ipo.decision || "HOLD",
            combined_score: ipo.ipo_score || ipo.pre_listing_advisory_score || 60,
            conviction: ipo.ipo_score || ipo.pre_listing_advisory_score || 60,
            price,
            cmp: price,
            change_pct: ipo.momentum_5d_pct,
            atr: ipo.atr_pct ? (ipo.atr_pct / 100) * price : undefined,
            technical_score: ipo.ipo_score || 60,
            fundamental_score: ipo.pre_listing_advisory_score || 60,
            sector: "IPO",
          },
        ],
        target_count: 1,
        min_conviction: 50, // relaxed — this is one specific row the user asked about directly
      });
      const found = (data?.suggestions || []) as BuySuggestion[];
      setSuggestions(found);
      if (!found.length) {
        setSniperError(
          data?.error || "No actionable buy setup for this IPO right now — score/momentum too weak."
        );
      }
    } catch (err: any) {
      setSniperError(err?.message || "Failed to evaluate buy setup");
    } finally {
      setSniperLoading(false);
      setSniperBusySymbol(null);
    }
  };

  // Top-level buy sniper: rank EVERY tracked IPO that has a live price and pick
  // the best 1-4 buy setups among them (the same "choose the best stock among
  // them" flow as Hot Picks, applied to the IPO universe) instead of only being
  // able to evaluate one row at a time.
  const handleSearchBuysFromIpo = async () => {
    setSniperOpen(true);
    setSniperError(null);
    setSuggestions([]);
    const mapped = ipoList
      .map((ipo) => {
        const price = Number(ipo?.current_price) || 0;
        if (!ipo?.symbol || price <= 0) return null;
        const conv = ipo.ipo_score || ipo.pre_listing_advisory_score || 60;
        return {
          symbol: ipo.symbol,
          decision: ipo.decision || "HOLD",
          combined_score: conv,
          conviction: conv,
          price,
          cmp: price,
          change_pct: ipo.momentum_5d_pct,
          atr: ipo.atr_pct ? (ipo.atr_pct / 100) * price : undefined,
          technical_score: ipo.ipo_score || 60,
          fundamental_score: ipo.pre_listing_advisory_score || 60,
          sector: "IPO",
        };
      })
      .filter((x): x is NonNullable<typeof x> => !!x);
    if (!mapped.length) {
      setSniperError(
        "No IPOs with a live price to evaluate yet — run a scan / premarket feed first."
      );
      return;
    }
    setSniperLoading(true);
    try {
      const data = await api.findBuys({
        stocks: mapped,
        target_count: 4,
        min_conviction: 55,
      });
      const found = (data?.suggestions || []) as BuySuggestion[];
      setSuggestions(found);
      if (!found.length) {
        setSniperError(
          data?.error || "No actionable IPO buy setups right now — scores/momentum too weak."
        );
      }
    } catch (err: any) {
      setSniperError(err?.message || "Failed to find IPO buy setups");
    } finally {
      setSniperLoading(false);
    }
  };

  const submitIpoAdd = async () => {
    if (!ipoForm.company_name.trim()) return;
    setIpoAddBusy(true);
    setIpoAddNotice(null);
    try {
      const res = await api.ipoAdd({ company_name: ipoForm.company_name.trim() });
      if (!res?.accepted) {
        setIpoAddNotice({
          message: res?.message || "Could not resolve that company name.",
          suggestions: res?.suggestions || [],
        });
        return;
      }
      setIpoForm({ company_name: "" });
      setIpoAddOpen(false);
      await startIpoScan();
    } catch (e: any) {
      setIpoError(e?.message || "Failed to add IPO");
    } finally {
      setIpoAddBusy(false);
    }
  };

  // Instant paint from the last stored scan, then resume polling if a scan is
  // still running server-side (e.g. the tab was opened mid-scan).
  useEffect(() => {
    void (async () => {
      await fetchIpoList();
      try {
        const st = await api.ipoScanStatus();
        if (st?.status === "running") {
          setIpoScanning(true);
          setIpoProgress(st);
          stopIpoPoll();
          ipoPollRef.current = window.setInterval(pollIpoStatus, 2000);
        }
      } catch {
        /* status is best-effort on mount */
      }
    })();
    return () => stopIpoPoll();
  }, [fetchIpoList, pollIpoStatus]);

  // Window toggle re-reads the already-scanned list (no re-scan, no NSE hit).
  useEffect(() => {
    void fetchIpoList();
  }, [displayDays, fetchIpoList]);

  const pct =
    ipoProgress?.total && ipoProgress.total > 0
      ? Math.min(100, Math.round((100 * (ipoProgress.processed || 0)) / ipoProgress.total))
      : 0;

  return (
    <div className="space-y-4">
      <div className="rounded-2xl border border-slate bg-graphite p-4 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <h2 className="font-display text-xl text-signal-prepare/95">🆕 IPO Tracker</h2>
            <p className="font-display tabular-nums text-[11px] text-mist/60 mt-1 max-w-xl">
              Short-term buy/sell read on recently-listed, listing-today and upcoming NSE IPOs.
              Entry / target / stop numbers are computed server-side, so a listing-day row is
              actionable the moment it prints.
            </p>
            <p className="font-display tabular-nums text-[10px] text-mist/45 mt-2">
              {ipoList.length} shown
              {totalScanned != null && totalScanned !== ipoList.length
                ? ` of ${totalScanned} scanned`
                : ""}
              {ipoGeneratedAt && (
                <span> · updated {new Date(ipoGeneratedAt).toLocaleTimeString("en-IN")}</span>
              )}
              {wsConnected && <span className="text-signal-buy/80 ml-1">● live</span>}
            </p>
          </div>

          <div className="flex flex-wrap gap-2">
            {/* Display window — 30d default, widen up to 1y without re-scanning.
                The backend scan itself always walks the full ~1y universe
                (see WINDOW_RECENT/WINDOW_3M/WINDOW_6M/WINDOW_WIDE above); these
                buttons only change which already-scanned rows are shown. */}
            <div className="inline-flex rounded-xl border border-slate/60 overflow-hidden">
              <button
                type="button"
                onClick={() => setDisplayDays(WINDOW_RECENT)}
                className={`font-display tabular-nums text-[11px] px-3 py-1.5 transition ${
                  displayDays === WINDOW_RECENT
                    ? "bg-signal-prepare/25 text-white"
                    : "bg-transparent text-mist/60 hover:bg-slate/30"
                }`}
              >
                Last 30d
              </button>
              <button
                type="button"
                onClick={() => setDisplayDays(WINDOW_3M)}
                className={`font-display tabular-nums text-[11px] px-3 py-1.5 border-l border-slate/60 transition ${
                  displayDays === WINDOW_3M
                    ? "bg-signal-prepare/25 text-white"
                    : "bg-transparent text-mist/60 hover:bg-slate/30"
                }`}
              >
                Last 3m
              </button>
              <button
                type="button"
                onClick={() => setDisplayDays(WINDOW_6M)}
                className={`font-display tabular-nums text-[11px] px-3 py-1.5 border-l border-slate/60 transition ${
                  displayDays === WINDOW_6M
                    ? "bg-signal-prepare/25 text-white"
                    : "bg-transparent text-mist/60 hover:bg-slate/30"
                }`}
              >
                Last 6m
              </button>
              <button
                type="button"
                onClick={() => setDisplayDays(WINDOW_WIDE)}
                className={`font-display tabular-nums text-[11px] px-3 py-1.5 border-l border-slate/60 transition ${
                  displayDays === WINDOW_WIDE
                    ? "bg-signal-prepare/25 text-white"
                    : "bg-transparent text-mist/60 hover:bg-slate/30"
                }`}
              >
                Last 1y
              </button>
            </div>

            <button
              type="button"
              onClick={() => setIpoAddOpen(!ipoAddOpen)}
              className="font-display tabular-nums text-[11px] px-3 py-1.5 rounded-xl bg-slate-500/20 border border-slate-400/40 text-paper hover:bg-slate-500/30"
            >
              + Add IPO
            </button>
            <button
              type="button"
              onClick={() => void startIpoScan(true, true)}
              disabled={ipoScanning}
              title="Wipes ipo_static_feed and the cached list, then bulk-seeds fresh from upstream (NSE + ipoalerts + yfinance) — same job the morning 'IPO Premarket Refresh' GitHub Action runs on a schedule (without the wipe), triggered here on demand. Only wipes once the fresh fetch is confirmed healthy; downgrades to a safe merge instead if NSE looks partially blocked, so a bad fetch can't erase good data."
              className="font-display tabular-nums text-[11px] px-3 py-1.5 rounded-xl bg-signal-hold/20 border border-signal-hold/40 text-white hover:bg-signal-hold/30 disabled:opacity-40"
            >
              {ipoScanning ? (
                <span className="inline-flex items-center gap-1.5">
                  <BusySpinner className="border-signal-hold" /> Feeding…
                </span>
              ) : (
                "📥 Premarket Feed"
              )}
            </button>
            <button
              type="button"
              onClick={() => void loadFromDb()}
              disabled={dbLoadBusy}
              title="Reads ipo_static_feed / the cached list only — never calls NSE/yfinance. Use Force Rescan for that."
              className="font-display tabular-nums text-[11px] px-3 py-1.5 rounded-xl bg-signal-prepare/20 border border-signal-prepare/40 text-white hover:bg-signal-prepare/30 disabled:opacity-40"
            >
              {dbLoadBusy ? (
                <span className="inline-flex items-center gap-1.5">
                  <BusySpinner className="border-signal-prepare" /> Loading…
                </span>
              ) : (
                "↻ Scan IPOs (DB)"
              )}
            </button>
            <button
              type="button"
              onClick={() => void startIpoScan(true, false)}
              disabled={ipoScanning}
              title="Re-scans every IPO from scratch, straight from upstream (NSE + yfinance), merging fresh results into the existing cache/DB (new data wins per symbol, nothing already-known disappears) — a one-off refresh mid-session without the full wipe Premarket Feed does."
              className="font-display tabular-nums text-[11px] px-3 py-1.5 rounded-xl bg-signal-prepare/10 border border-signal-prepare/30 text-signal-prepare/80 hover:bg-signal-prepare/20 disabled:opacity-40"
            >
              {ipoScanning ? (
                <span className="inline-flex items-center gap-1.5">
                  <BusySpinner className="border-signal-prepare" /> Scanning…
                </span>
              ) : (
                "Force Scan (upstream)"
              )}
            </button>
            <button
              type="button"
              onClick={() => void stopIpoScan()}
              disabled={!ipoScanning || stopBusy}
              className="font-display tabular-nums text-[11px] px-3 py-1.5 rounded-xl bg-signal-sell/20 border border-signal-sell/40 text-white hover:bg-signal-sell/30 disabled:opacity-40"
            >
              {stopBusy ? "Stopping…" : "■ Stop"}
            </button>
            <button
              type="button"
              onClick={() => void handleSearchBuysFromIpo()}
              disabled={sniperLoading || ipoList.length === 0}
              title="Rank every tracked IPO with a live price and pick the 1-4 best buy setups among them"
              className="font-display tabular-nums text-[11px] px-3 py-1.5 rounded-xl border border-signal-buy/50 bg-signal-buy/20 text-white hover:bg-signal-buy/35 disabled:opacity-40 shadow-lg shadow-emerald-900/20"
            >
              {sniperLoading && !sniperBusySymbol ? "Sniping…" : "🎯 Search for Buy Stocks (1-4)"}
            </button>
            <button
              type="button"
              onClick={() => void notifyTop5(5)}
              disabled={notifyBusy || ipoList.length === 0}
              title="Send the top 5 IPO Tracker picks to Telegram (uses the same notification channel configured in Settings)"
              className="font-display tabular-nums text-[11px] px-3 py-1.5 rounded-xl bg-signal-prepare/20 border border-signal-prepare/40 text-white hover:bg-signal-prepare/30 disabled:opacity-40"
            >
              {notifyBusy ? "Sending…" : "📨 Send Top 5 to Telegram"}
            </button>
          </div>
          {notifyMsg && (
            <p className="font-display tabular-nums text-[10px] text-mist mt-1">{notifyMsg}</p>
          )}
        </div>

        {ipoAddOpen && (
          <div className="mb-4 rounded-xl border border-slate/50 bg-ink/60 p-3 flex flex-col gap-2">
            <input
              placeholder="Exact company name (e.g. Tempsens Instruments (India) Limited)"
              value={ipoForm.company_name}
              onChange={(e) => setIpoForm({ company_name: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && void submitIpoAdd()}
              className="bg-ink/60 border border-slate rounded-xl px-2 py-1.5 font-display tabular-nums text-xs text-paper placeholder:text-mist/30 outline-none"
            />
            <p className="font-display tabular-nums text-[10px] text-mist/50">
              Just the name — symbol, issue price, and listing date are looked up automatically.
            </p>
            {ipoAddNotice && (
              <div className="rounded-xl border border-signal-hold/30 bg-signal-hold/5 px-2 py-1.5 font-display tabular-nums text-[11px] text-signal-hold/90">
                {ipoAddNotice.message}
                {ipoAddNotice.suggestions.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {ipoAddNotice.suggestions.map((s) => (
                      <button
                        key={s}
                        type="button"
                        onClick={() => {
                          setIpoForm({ company_name: s });
                          setIpoAddNotice(null);
                        }}
                        className="rounded border border-signal-hold/40 px-1.5 py-0.5 text-white hover:bg-signal-hold/20"
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
            <button
              type="button"
              onClick={() => void submitIpoAdd()}
              disabled={ipoAddBusy || !ipoForm.company_name.trim()}
              className="font-display tabular-nums text-xs px-3 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 text-white hover:bg-signal-buy/30 disabled:opacity-40"
            >
              {ipoAddBusy ? "Looking it up…" : "Add & Scan"}
            </button>
          </div>
        )}

        {(ipoScanning || (ipoProgress?.status && ipoProgress.status === "running")) && (
          <div className="mb-4 rounded-2xl border border-signal-prepare/30 bg-signal-prepare/5 px-4 py-3">
            <div className="flex flex-wrap justify-between gap-2 mb-2">
              <p className="font-display tabular-nums text-[11px] text-signal-prepare">IPO scan · {ipoProgress?.status || "running"}</p>
              <p className="font-display tabular-nums text-[10px] text-mist/60">
                {ipoProgress?.processed ?? 0}/{ipoProgress?.total ?? "—"}
              </p>
            </div>
            <div className="h-2 rounded-full bg-ink/60 overflow-hidden border border-slate/50">
              <div
                className="h-full bg-gradient-to-r from-violet-500/80 to-emerald-500/80 transition-all duration-500"
                style={{ width: `${pct}%` }}
              />
            </div>
            <p className="font-display tabular-nums text-[10px] text-mist/50 mt-1.5">
              {pct}% · {ipoProgress?.message || "…"}
            </p>
          </div>
        )}

        {ipoProgress?.status === "stopped" && !ipoScanning && (
          <div className="mb-4 rounded-xl border border-signal-hold/40 bg-signal-hold/10 px-3 py-2 font-display tabular-nums text-[11px] text-signal-hold">
            {ipoProgress.message || "Scan stopped — partial results kept."}
          </div>
        )}
        {ipoProgress?.status === "skipped_fresh" && !ipoScanning && (
          <div className="mb-4 rounded-xl border border-signal-prepare/40 bg-signal-prepare/10 px-3 py-2 font-display tabular-nums text-[11px] text-signal-prepare">
            {ipoProgress.message}
          </div>
        )}
        {ipoError && (
          <div className="mb-4 rounded-xl border border-signal-sell/40 bg-signal-sell/10 px-3 py-2 font-display tabular-nums text-[11px] text-white">
            {ipoError}
          </div>
        )}

        {ipoList.length === 0 && !ipoScanning ? (
          <p className="font-display tabular-nums text-[11px] text-mist/45 py-6 text-center">
            No IPOs in the last {displayDays === WINDOW_WIDE ? "year" : `${displayDays} days`} — tap
            “Scan IPOs” for auto-discovery, switch to “Last 1y” to widen the window, or “+ Add IPO”
            if NSE’s feed is blocked.
          </p>
        ) : (
          <div className="space-y-2">
            {ipoList.map((ipo) => {
              const stage = stageLabel(ipo);
              const gainPct = ipo.current_vs_issue_pct;
              return (
                <div
                  key={ipo.symbol}
                  className="rounded-xl border border-slate/50 bg-ink/50 px-3 py-2.5"
                >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-[140px]">
                    <button
                      type="button"
                      onClick={() => onSelect?.(ipo.symbol)}
                      className="font-display tabular-nums text-xs text-paper hover:text-signal-buy text-left"
                    >
                      {ipo.symbol}
                    </button>
                    <p className={`font-display tabular-nums text-[10px] ${stage.tone}`}>{stage.text}</p>
                  </div>

                  <div className="font-display tabular-nums text-[10px] text-mist/60 min-w-[110px]">
                    <p>Issue ₹{ipo.issue_price}</p>
                    {ipo.current_price != null && <p>CMP ₹{ipo.current_price}</p>}
                  </div>

                  {gainPct != null && (
                    <p
                      className={`font-display tabular-nums text-xs min-w-[70px] ${
                        gainPct >= 0 ? "text-signal-buy" : "text-signal-sell"
                      }`}
                    >
                      {gainPct >= 0 ? "+" : ""}
                      {gainPct.toFixed(1)}%
                    </p>
                  )}

                  {ipo.ipo_score != null && (
                    <p className="font-display tabular-nums text-[10px] text-mist/50 min-w-[70px]">
                      Score {Math.round(ipo.ipo_score)}/100
                    </p>
                  )}

                  {/* GMP shown on card face — most relevant for pre/upcoming IPOs.
                      For already-listed stocks (days_since_listing > 1) the stock
                      is already trading and GMP is stale/irrelevant; skip it there. */}
                  {ipo.gmp != null && (ipo.stage === "upcoming" || ipo.stage === "pre_listing" || ipo.stage === "listing_day" || ipo.stage === "no_data_yet") && (
                    <p className={`font-display tabular-nums text-[10px] min-w-[90px] font-semibold ${
                      ipo.gmp > 0 ? "text-signal-buy" : ipo.gmp < 0 ? "text-signal-sell" : "text-mist/60"
                    }`}>
                      GMP ₹{ipo.gmp}{ipo.gmp_pct_of_issue != null ? ` (${ipo.gmp_pct_of_issue > 0 ? "+" : ""}${ipo.gmp_pct_of_issue.toFixed(0)}%)` : ""}
                    </p>
                  )}

                  {ipo.pre_listing_advisory && (
                    <p className="font-display tabular-nums text-[10px] text-signal-hold/80 max-w-[200px]">
                      {ipo.pre_listing_advisory}
                    </p>
                  )}

                  <span
                    className={`font-display tabular-nums text-[10px] px-2 py-1 rounded-xl border ${decisionBadgeClass(
                      ipo.decision
                    )}`}
                  >
                    {ipo.decision || stage.text}
                  </span>

                  {(ipo.buy_suggestion || Number(ipo.current_price) > 0) && (
                    <button
                      type="button"
                      onClick={() => void openIpoSuggestion(ipo)}
                      disabled={sniperLoading && sniperBusySymbol === ipo.symbol}
                      title={
                        ipo.buy_suggestion
                          ? "Show the stored buy setup"
                          : "No stored setup yet — scan live for one"
                      }
                      className="font-display tabular-nums text-[11px] px-3 py-1.5 rounded-xl bg-signal-buy/20 border border-signal-buy/40 text-signal-buy hover:bg-signal-buy/30 disabled:opacity-50"
                    >
                      {sniperLoading && sniperBusySymbol === ipo.symbol ? (
                        <span className="inline-flex items-center gap-1.5">
                          <BusySpinner className="border-signal-buy" /> Scanning…
                        </span>
                      ) : ipo.buy_suggestion ? (
                        ipo.decision === "BUY NOW" ? (
                          "Buy Now →"
                        ) : (
                          "Prepare to Buy →"
                        )
                      ) : (
                        "🎯 Scan for Buy →"
                      )}
                    </button>
                  )}

                  <button
                    type="button"
                    onClick={() => setExpandedSymbol((cur) => (cur === ipo.symbol ? null : ipo.symbol))}
                    className="font-display tabular-nums text-[10px] text-mist/50 hover:text-paper transition uppercase tracking-wide"
                  >
                    {expandedSymbol === ipo.symbol ? "Hide detail ▴" : "Detail ▾"}
                  </button>
                </div>

                {expandedSymbol === ipo.symbol && (
                  <div className="mt-2.5 pt-2.5 border-t border-slate/40 grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-2">
                    <div>
                      <p className="font-display tabular-nums text-[9px] text-mist/40 uppercase tracking-wide mb-1">Listing</p>
                      <div className="font-display tabular-nums text-[10px] text-mist/70 space-y-0.5">
                        {ipo.listing_date && <p>Listed {ipo.listing_date}</p>}
                        {ipo.days_since_listing != null && <p>{ipo.days_since_listing}d since listing</p>}
                        {ipo.listing_day_close != null && <p>Day-1 close ₹{ipo.listing_day_close}</p>}
                        {ipo.listing_pop_pct != null && <p>Listing pop {ipo.listing_pop_pct.toFixed(1)}%</p>}
                        {ipo.post_listing_high != null && <p>Post-listing high ₹{ipo.post_listing_high}</p>}
                        {ipo.current_vs_high_pct != null && <p>vs high {ipo.current_vs_high_pct.toFixed(1)}%</p>}
                        {ipo.subscription_times != null && <p>Subscribed {ipo.subscription_times}x</p>}
                        {ipo.source && <p>Source: {ipo.source}</p>}
                      </div>
                    </div>

                    <div>
                      <p className="font-display tabular-nums text-[9px] text-mist/40 uppercase tracking-wide mb-1">Momentum</p>
                      <div className="font-display tabular-nums text-[10px] text-mist/70 space-y-0.5">
                        {ipo.momentum_5d_pct != null && <p>5d momentum {ipo.momentum_5d_pct.toFixed(1)}%</p>}
                        {ipo.volume_trend_ratio != null && <p>Volume trend {ipo.volume_trend_ratio.toFixed(2)}x</p>}
                        {ipo.atr_pct != null && <p>ATR {ipo.atr_pct.toFixed(1)}%</p>}
                        {ipo.gmp != null && (
                          <p className={ipo.gmp >= 0 ? "text-signal-buy" : "text-signal-sell"}>
                            GMP ₹{ipo.gmp} ({ipo.gmp_pct_of_issue != null ? `${ipo.gmp_pct_of_issue > 0 ? "+" : ""}${ipo.gmp_pct_of_issue.toFixed(1)}%` : ""} of issue)
                          </p>
                        )}
                        {ipo.gmp_implied_listing != null && (
                          <p className="text-mist/60">
                            Implied listing ₹{ipo.gmp_implied_listing}
                          </p>
                        )}
                        {ipo.gmp == null && (
                          <p className="text-mist/40">GMP: not available</p>
                        )}
                        {ipo.momentum_5d_pct == null &&
                          ipo.volume_trend_ratio == null &&
                          ipo.atr_pct == null &&
                          ipo.gmp == null && <p className="text-mist/40">No momentum data yet</p>}
                      </div>
                    </div>

                    {ipo.score_breakdown && Object.keys(ipo.score_breakdown).length > 0 && (
                      <div>
                        <p className="font-display tabular-nums text-[9px] text-mist/40 uppercase tracking-wide mb-1">Score Breakdown</p>
                        <div className="font-display tabular-nums text-[10px] text-mist/70 space-y-0.5">
                          {Object.entries(ipo.score_breakdown).map(([k, v]) => (
                            <p key={k} className="flex justify-between gap-2">
                              <span className="text-mist/50">{k.replace(/_/g, " ")}</span>
                              <span>{typeof v === "number" ? v.toFixed(1) : String(v)}</span>
                            </p>
                          ))}
                        </div>
                      </div>
                    )}

                    {ipo.fundamentals_snapshot && Object.keys(ipo.fundamentals_snapshot).length > 0 && (
                      <div>
                        <p className="font-display tabular-nums text-[9px] text-mist/40 uppercase tracking-wide mb-1">Fundamentals</p>
                        <div className="font-display tabular-nums text-[10px] text-mist/70 space-y-0.5">
                          {Object.entries(ipo.fundamentals_snapshot).map(([k, v]) => (
                            <p key={k} className="flex justify-between gap-2">
                              <span className="text-mist/50">{k.replace(/_/g, " ")}</span>
                              <span>{v == null ? "—" : typeof v === "number" ? v.toFixed(2) : String(v)}</span>
                            </p>
                          ))}
                        </div>
                      </div>
                    )}

                    {ipo.message && (
                      <div className="col-span-full">
                        <p className="font-display tabular-nums text-[10px] text-mist/50">{ipo.message}</p>
                      </div>
                    )}
                  </div>
                )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Generic feed audit — same component the Data Feed tab uses, so a
          missing-price problem behind an IPO row can be diagnosed here. */}
      <IpoFeedHealth onRepairComplete={fetchIpoList} />

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
