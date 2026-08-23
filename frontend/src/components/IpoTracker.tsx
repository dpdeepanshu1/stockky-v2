// frontend/src/components/IpoTracker.tsx
//
// IPO Tracker — standalone left-nav tab (moved OUT of SurpriseStocks.tsx,
// where it used to live as an <IpoSection /> subsection).
//
// Everything IPO-related now lives here: its own state, its own polling, its
// own Scan / Stop controls, the 30-day ⇄ 1-year display window toggle
// (backend: GET /surprise/ipo/list?display_days=), and the shared
// <DataHealthAudit /> panel so feed problems can be diagnosed from the same
// screen instead of hopping to the Data Feed tab.
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
import DataHealthAudit from "./DataHealthAudit";

type IpoProgress = {
  status?: string;
  processed?: number;
  total?: number;
  message?: string;
  results_count?: number;
};

type IpoForm = {
  symbol: string;
  issue_price: string;
  listing_date: string;
  subscription_times: string;
  gmp: string;
};

// Display window presets. The backend scan itself always walks the full
// IPO_LOOKBACK_DAYS_HARD_CAP (~365d) universe; display_days only narrows what
// the list endpoint returns, so switching these never triggers a re-scan.
const WINDOW_RECENT = 30;
const WINDOW_WIDE = 365;

function decisionBadgeClass(decision?: string): string {
  const d = (decision || "").toUpperCase();
  if (d === "BUY NOW") return "bg-signal-buy/20 text-signal-buy border-signal-buy/40";
  if (d === "PREPARE TO BUY") return "bg-signal-prepare/20 text-signal-prepare border-signal-prepare/40";
  if (d === "SELL") return "bg-rose-500/20 text-rose-300 border-rose-500/40";
  if (d === "DO NOT BUY") return "bg-mist/10 text-mist/70 border-slate/50";
  return "bg-amber-500/10 text-amber-300 border-amber-500/30"; // HOLD
}

function stageLabel(ipo: IpoAnalysis): { text: string; tone: string } {
  if (ipo.stage === "upcoming") return { text: `Lists ${ipo.listing_date}`, tone: "text-mist/60" };
  if (ipo.stage === "pre_listing") return { text: "Lists today · pre-open", tone: "text-amber-300" };
  if (ipo.stage === "listing_day") return { text: "Listing day · live", tone: "text-emerald-300" };
  if (ipo.stage === "listed") return { text: `Day ${ipo.days_since_listing}`, tone: "text-mist/60" };
  return { text: ipo.stage || "—", tone: "text-mist/50" };
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
    symbol: "",
    issue_price: "",
    listing_date: "",
    subscription_times: "",
    gmp: "",
  });
  const [ipoAddBusy, setIpoAddBusy] = useState(false);
  const [stopBusy, setStopBusy] = useState(false);
  const [displayDays, setDisplayDays] = useState<number>(WINDOW_RECENT);
  const [totalScanned, setTotalScanned] = useState<number | null>(null);
  const ipoPollRef = useRef<number | null>(null);

  const [sniperOpen, setSniperOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<BuySuggestion[]>([]);

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

  const pollIpoStatus = useCallback(async () => {
    try {
      const st = await api.ipoScanStatus();
      setIpoProgress(st);
      const running = st?.status === "running";
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
  // below stays as a fallback for when the socket drops.
  const onRealtimeMessage = useCallback(
    (msg: RealtimeMessage) => {
      if (msg.type !== "jobs_snapshot") return;
      const ipoJob = (msg as any).ipo_scan;
      if (!ipoJob) return;
      setIpoScanning(ipoJob.status === "running");
      setIpoProgress(ipoJob);
      if (ipoJob.status && ipoJob.status !== "running") {
        stopIpoPoll();
        setStopBusy(false);
        void fetchIpoList();
      }
    },
    [fetchIpoList]
  );
  const { connected: wsConnected } = useStockkyRealtime(onRealtimeMessage);

  const startIpoScan = useCallback(async () => {
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

  const openIpoSuggestion = (ipo: IpoAnalysis) => {
    if (!ipo?.buy_suggestion) return;
    setSuggestions([ipo.buy_suggestion as unknown as BuySuggestion]);
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
        gmp: ipoForm.gmp ? Number(ipoForm.gmp) : undefined,
      });
      setIpoForm({ symbol: "", issue_price: "", listing_date: "", subscription_times: "", gmp: "" });
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
      <div className="rounded-xl border border-slate bg-graphite p-4 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div>
            <h2 className="font-display text-xl text-violet-300/95">🆕 IPO Tracker</h2>
            <p className="font-mono text-[11px] text-mist/60 mt-1 max-w-xl">
              Short-term buy/sell read on recently-listed, listing-today and upcoming NSE IPOs.
              Entry / target / stop numbers are computed server-side, so a listing-day row is
              actionable the moment it prints.
            </p>
            <p className="font-mono text-[10px] text-mist/45 mt-2">
              {ipoList.length} shown
              {totalScanned != null && totalScanned !== ipoList.length
                ? ` of ${totalScanned} scanned`
                : ""}
              {ipoGeneratedAt && (
                <span> · updated {new Date(ipoGeneratedAt).toLocaleTimeString("en-IN")}</span>
              )}
              {wsConnected && <span className="text-emerald-400/80 ml-1">● live</span>}
            </p>
          </div>

          <div className="flex flex-wrap gap-2">
            {/* Display window — 30d default, widen to 1y without re-scanning */}
            <div className="inline-flex rounded-lg border border-slate/60 overflow-hidden">
              <button
                type="button"
                onClick={() => setDisplayDays(WINDOW_RECENT)}
                className={`font-mono text-[11px] px-3 py-1.5 transition ${
                  displayDays === WINDOW_RECENT
                    ? "bg-violet-500/25 text-violet-100"
                    : "bg-transparent text-mist/60 hover:bg-slate/30"
                }`}
              >
                Last 30d
              </button>
              <button
                type="button"
                onClick={() => setDisplayDays(WINDOW_WIDE)}
                className={`font-mono text-[11px] px-3 py-1.5 border-l border-slate/60 transition ${
                  displayDays === WINDOW_WIDE
                    ? "bg-violet-500/25 text-violet-100"
                    : "bg-transparent text-mist/60 hover:bg-slate/30"
                }`}
              >
                Last 1y
              </button>
            </div>

            <button
              type="button"
              onClick={() => setIpoAddOpen(!ipoAddOpen)}
              className="font-mono text-[11px] px-3 py-1.5 rounded-lg bg-slate-500/20 border border-slate-400/40 text-paper hover:bg-slate-500/30"
            >
              + Add IPO
            </button>
            <button
              type="button"
              onClick={() => void startIpoScan()}
              disabled={ipoScanning}
              className="font-mono text-[11px] px-3 py-1.5 rounded-lg bg-violet-500/20 border border-violet-400/40 text-violet-100 hover:bg-violet-500/30 disabled:opacity-40"
            >
              {ipoScanning ? "Scanning…" : "Scan IPOs"}
            </button>
            <button
              type="button"
              onClick={() => void stopIpoScan()}
              disabled={!ipoScanning || stopBusy}
              className="font-mono text-[11px] px-3 py-1.5 rounded-lg bg-rose-500/20 border border-rose-400/40 text-rose-100 hover:bg-rose-500/30 disabled:opacity-40"
            >
              {stopBusy ? "Stopping…" : "■ Stop"}
            </button>
          </div>
        </div>

        {ipoAddOpen && (
          <div className="mb-4 rounded-lg border border-slate/50 bg-ink/60 p-3 grid grid-cols-2 gap-2">
            <input
              placeholder="Symbol (e.g. XYZLTD)"
              value={ipoForm.symbol}
              onChange={(e) => setIpoForm({ ...ipoForm, symbol: e.target.value })}
              className="col-span-2 bg-ink/60 border border-slate rounded-lg px-2 py-1.5 font-mono text-xs text-paper placeholder:text-mist/30 outline-none"
            />
            <input
              placeholder="Issue price (₹)"
              value={ipoForm.issue_price}
              onChange={(e) => setIpoForm({ ...ipoForm, issue_price: e.target.value })}
              className="bg-ink/60 border border-slate rounded-lg px-2 py-1.5 font-mono text-xs text-paper placeholder:text-mist/30 outline-none"
            />
            <input
              type="date"
              value={ipoForm.listing_date}
              onChange={(e) => setIpoForm({ ...ipoForm, listing_date: e.target.value })}
              className="bg-ink/60 border border-slate rounded-lg px-2 py-1.5 font-mono text-xs text-paper outline-none"
            />
            <input
              placeholder="Subscription (x, optional)"
              value={ipoForm.subscription_times}
              onChange={(e) => setIpoForm({ ...ipoForm, subscription_times: e.target.value })}
              className="bg-ink/60 border border-slate rounded-lg px-2 py-1.5 font-mono text-xs text-paper placeholder:text-mist/30 outline-none"
            />
            <input
              placeholder="GMP ₹ (optional)"
              value={ipoForm.gmp}
              onChange={(e) => setIpoForm({ ...ipoForm, gmp: e.target.value })}
              className="bg-ink/60 border border-slate rounded-lg px-2 py-1.5 font-mono text-xs text-paper placeholder:text-mist/30 outline-none"
            />
            <button
              type="button"
              onClick={() => void submitIpoAdd()}
              disabled={ipoAddBusy || !ipoForm.symbol || !ipoForm.issue_price || !ipoForm.listing_date}
              className="col-span-2 font-mono text-xs px-3 py-2 rounded-lg bg-emerald-500/20 border border-emerald-400/40 text-emerald-100 hover:bg-emerald-500/30 disabled:opacity-40"
            >
              {ipoAddBusy ? "Adding…" : "Add & Scan"}
            </button>
          </div>
        )}

        {(ipoScanning || (ipoProgress?.status && ipoProgress.status === "running")) && (
          <div className="mb-4 rounded-xl border border-violet-500/30 bg-violet-500/5 px-4 py-3">
            <div className="flex flex-wrap justify-between gap-2 mb-2">
              <p className="font-mono text-[11px] text-violet-200">IPO scan · {ipoProgress?.status || "running"}</p>
              <p className="font-mono text-[10px] text-mist/60">
                {ipoProgress?.processed ?? 0}/{ipoProgress?.total ?? "—"}
              </p>
            </div>
            <div className="h-2 rounded-full bg-ink/60 overflow-hidden border border-slate/50">
              <div
                className="h-full bg-gradient-to-r from-violet-500/80 to-emerald-500/80 transition-all duration-500"
                style={{ width: `${pct}%` }}
              />
            </div>
            <p className="font-mono text-[10px] text-mist/50 mt-1.5">
              {pct}% · {ipoProgress?.message || "…"}
            </p>
          </div>
        )}

        {ipoProgress?.status === "stopped" && !ipoScanning && (
          <div className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 font-mono text-[11px] text-amber-200">
            {ipoProgress.message || "Scan stopped — partial results kept."}
          </div>
        )}
        {ipoProgress?.status === "skipped_fresh" && !ipoScanning && (
          <div className="mb-4 rounded-lg border border-sky-500/40 bg-sky-500/10 px-3 py-2 font-mono text-[11px] text-sky-200">
            {ipoProgress.message}
          </div>
        )}
        {ipoError && (
          <div className="mb-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 font-mono text-[11px] text-rose-200">
            {ipoError}
          </div>
        )}

        {ipoList.length === 0 && !ipoScanning ? (
          <p className="font-mono text-[11px] text-mist/45 py-6 text-center">
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
                    <p
                      className={`font-mono text-xs min-w-[70px] ${
                        gainPct >= 0 ? "text-emerald-400" : "text-rose-400"
                      }`}
                    >
                      {gainPct >= 0 ? "+" : ""}
                      {gainPct.toFixed(1)}%
                    </p>
                  )}

                  {ipo.ipo_score != null && (
                    <p className="font-mono text-[10px] text-mist/50 min-w-[70px]">
                      Score {Math.round(ipo.ipo_score)}/100
                    </p>
                  )}

                  {ipo.pre_listing_advisory && (
                    <p className="font-mono text-[10px] text-amber-300/80 max-w-[200px]">
                      {ipo.pre_listing_advisory}
                    </p>
                  )}

                  <span
                    className={`font-mono text-[10px] px-2 py-1 rounded-md border ${decisionBadgeClass(
                      ipo.decision
                    )}`}
                  >
                    {ipo.decision || stage.text}
                  </span>

                  {ipo.buy_suggestion && (
                    <button
                      type="button"
                      onClick={() => openIpoSuggestion(ipo)}
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
      </div>

      {/* Generic feed audit — same component the Data Feed tab uses, so a
          missing-price problem behind an IPO row can be diagnosed here. */}
      <DataHealthAudit />

      <BuySniperModal
        isOpen={sniperOpen}
        onClose={() => setSniperOpen(false)}
        suggestions={suggestions}
        loading={false}
        error={null}
        onSelectSymbol={onSelect}
      />
    </div>
  );
}
