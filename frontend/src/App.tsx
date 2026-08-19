// frontend/src/App.tsx

import { useState, useEffect, useRef, useCallback } from "react";
import { api, getApiUrl, setApiUrl, Decision, ScanResult, wakeService, startSessionKeepAlive, stopSessionKeepAlive } from "./api";
import Pipeline from "./components/Pipeline";
import DecisionCard from "./components/DecisionCard";
import SignalStream, { BreadthStrip } from "./components/SignalStream";
import ScanPanel, { MultiHorizonScanLists } from "./components/ScanPanel";
import WatchlistManager from "./components/WatchlistManager";
import NotificationsPanel from "./components/NotificationsPanel";
import SystemCheck from "./components/SystemCheck";
import MarketMovers from "./components/MarketMovers";
import ServiceManager from "./components/ServiceManager";
import RateLimitDashboard from "./components/RateLimitDashboard";
import Training from "./components/Training";
import HotStocks from "./components/HotStocks";
import SurpriseStocks from "./components/SurpriseStocks";
import DataFeed from "./components/DataFeed";
import DataHealthAudit from "./components/DataHealthAudit";
import Trades from "./components/Trades";
// ── NEW: import Market Sentiment Header ──
import MarketSentimentHeader from "./components/MarketSentimentHeader";
import CommandPalette, { CommandAction } from "./components/CommandPalette";
import { useStockkyRealtime, RealtimeMessage } from "./useRealtime";

type ViewState =
  | { mode: "idle" }
  | { mode: "loading"; label: string; progress?: { processed: number; total: number; elapsed: number; estimatedRemaining?: number } }
  | { mode: "stock"; data: Decision }
  | { mode: "scan"; data: ScanResult }
  | { mode: "error"; message: string };

type Tab = "dashboard" | "notifications" | "training" | "trades" | "hot" | "surprise" | "datafeed" | "settings" | "watchlist";


async function powerOffAll() {
  try {
    window.alert(
      "Switching OFF all processes…\n" +
        "Committing in-progress work, then force-stopping scan / data-feed / hot-picks / training.\n" +
        "Only health checks will remain."
    );
    const base = import.meta.env.VITE_API_URL || "";
    // Stop client-side keep-alive and any local intervals before backend
    try {
      const { stopSessionKeepAlive } = await import("./api");
      stopSessionKeepAlive();
    } catch {}
    try {
      sessionStorage.removeItem("stockky_scan_task");
      localStorage.removeItem("stockky_scan_task");
      sessionStorage.removeItem("stockky_ws_quotes");
    } catch {}
    // Parallel force-stops so backend workers die even if power-off is slow
    const stops = [
      fetch(`${base}/ops/power-off`, { method: "POST" }),
      fetch(`${base}/scan/stop-all`, { method: "POST" }).catch(() => null),
      fetch(`${base}/data-feed/stop?force=true`, { method: "POST" }).catch(() => null),
    ];
    const results = await Promise.allSettled(stops);
    let j: any = {};
    if (results[0].status === "fulfilled") {
      try {
        j = await (results[0] as PromiseFulfilledResult<Response>).value.json();
      } catch {
        j = {};
      }
    }
    window.alert(
      (j?.message || "Processes stopped.") +
        "\n\n" +
        (Array.isArray(j?.phases)
          ? j.phases.map((p: any) => `${p.phase}: ${p.detail}`).join("\n")
          : "") +
        "\n\nReady for fresh start. Refreshing…"
    );
    window.location.reload();
  } catch (e: any) {
    window.alert("Power-off failed: " + (e?.message || e));
  }
}

export default function App() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    try {
      const t = localStorage.getItem("stockky_theme");
      if (t === "light" || t === "dark") return t;
    } catch {}
    return "dark";
  });
  useEffect(() => {
    document.documentElement.classList.toggle("light", theme === "light");
    try { localStorage.setItem("stockky_theme", theme); } catch {}
  }, [theme]);

  // Free-tier: soft keep-alive only while the UI tab is open/visible (every ~4.5 min).
  // Does not run deep service fan-out — avoids overload during scans/schedulers.
  useEffect(() => {
    startSessionKeepAlive();
    return () => stopSessionKeepAlive();
  }, []);

  const [systemReady, setSystemReady] = useState(false);
  const [liteScan, setLiteScan] = useState(true);
  const [tab, setTab] = useState<Tab>("dashboard");
  const [query, setQuery] = useState("");
  const [view, setView] = useState<ViewState>({ mode: "idle" });
  const [signalFilter, setSignalFilter] = useState<"ALL" | "BUY" | "PREPARE" | "HOLD">("ALL");
  const [watchlist, setWatchlist] = useState<string[]>([]);
  const [showWatchlist, setShowWatchlist] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showServiceManager, setShowServiceManager] = useState(false);
  const [cmdOpen, setCmdOpen] = useState(false);
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false);

  // Watchlist overlay must not stick when navigating Dashboard / other tabs
  useEffect(() => {
    if (tab !== "watchlist") setShowWatchlist(false);
    setMobileMoreOpen(false);
  }, [tab]);

  // Lock body scroll while mobile more sheet is open
  useEffect(() => {
    if (!mobileMoreOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, [mobileMoreOpen]);

  // Cmd/Ctrl+K command palette
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCmdOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const goTrades = (ev?: Event) => {
      setTab("trades");
      // Optional: ask Trades tab to open deposit panel
      try {
        const detail = (ev as CustomEvent)?.detail;
        if (detail?.openDeposit) {
          window.setTimeout(() => {
            window.dispatchEvent(new CustomEvent("stockky:open-deposit", { detail }));
          }, 150);
        }
      } catch {}
    };
    window.addEventListener("stockky:goto-trades", goTrades as EventListener);
    return () => window.removeEventListener("stockky:goto-trades", goTrades as EventListener);
  }, []);

  // Idle-aware free-tier load control:
  // - Track user activity
  // - After 5 min idle during market hours → one light /ops/idle-tick
  // - When user is active, prefer caches (no extra background work)
  useEffect(() => {
    let lastActive = Date.now();
    let idleSent = false;
    const mark = () => {
      lastActive = Date.now();
      idleSent = false;
    };
    const events = ["pointerdown", "keydown", "scroll", "touchstart", "visibilitychange"] as const;
    events.forEach((e) => window.addEventListener(e, mark, { passive: true }));

    const timer = window.setInterval(async () => {
      try {
        if (document.visibilityState === "hidden") return;
        const idleMs = Date.now() - lastActive;
        if (idleMs < 5 * 60 * 1000 || idleSent) return;
        // Only during local IST market-ish window (browser local may differ; server still gates)
        const res = await fetch(`${getApiUrl().replace(/\/$/, "")}/ops/idle-tick`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        });
        idleSent = true;
        // If off-market server returns ran:false — stay quiet
        if (!res.ok) return;
      } catch {
        /* ignore */
      }
    }, 30000);

    return () => {
      events.forEach((e) => window.removeEventListener(e, mark));
      window.clearInterval(timer);
    };
  }, []);


  const [backendUp, setBackendUp] = useState<"checking" | "up" | "down">("checking");
  const [isWaking, setIsWaking] = useState(false);
  const [scanTaskId, setScanTaskId] = useState<string | null>(null);
  const [pollInterval, setPollInterval] = useState<number | null>(null);
  const [wsLive, setWsLive] = useState(false);
  const [liveScanRows, setLiveScanRows] = useState<Decision[]>([]);
  const [scanTransport, setScanTransport] = useState<"stream" | "poll" | null>(null);
  const pollIntervalRefWs = useRef<number | null>(null);
  const scanCancelledRef = useRef(false);
  const scanAbortRef = useRef<AbortController | null>(null);

  /** Stop all scan polling / session tracking on the client. */
  function clearScanActivity() {
    if (scanAbortRef.current) {
      try { scanAbortRef.current.abort(); } catch {}
      scanAbortRef.current = null;
    }
    if (pollIntervalRefWs.current) {
      window.clearInterval(pollIntervalRefWs.current);
      pollIntervalRefWs.current = null;
    }
    setPollInterval((prev) => {
      if (prev) window.clearInterval(prev);
      return null;
    });
    try { sessionStorage.removeItem("stockky_scan_task_id"); } catch {}
  }

  function buildScanResultFromRows(rows: Decision[], total: number, partial: boolean): ScanResult {
    const actionable = new Set(["BUY NOW", "PREPARE TO BUY", "SELL"]);
    const recommendations = rows.filter((r) => actionable.has(String(r.decision || "")));
    const buy = rows.filter((r) => r.decision === "BUY NOW" || r.decision === "PREPARE TO BUY").length;
    const sell = rows.filter((r) => r.decision === "SELL").length;
    const hold = rows.filter((r) => r.decision === "HOLD").length;
    const cautious = rows.filter((r) => r.decision === "DO NOT BUY" || r.decision === "WAIT").length;
    return {
      scanned: rows.length,
      universe_size: total || rows.length,
      watchlist_size: 0,
      recommendations,
      watchlist_candidates: [],
      verdict: partial
        ? `Partial stream: ${rows.length}/${total || rows.length}`
        : `Stream complete: ${rows.length} symbols`,
      market_mood: buy > sell ? "bullish" : sell > buy ? "bearish" : "mixed",
      market_stats: {
        buy_signals: buy,
        sell_signals: sell,
        hold_signals: hold,
        cautious,
      },
      all_results: rows,
      errors: rows
        .filter((r) => (r as any).error)
        .map((r) => ({ symbol: r.symbol, error: (r as any).error || "ERROR" })),
      ...(partial ? { partial: true, stopped_early: true } as any : {}),
    };
  }


  const handleRealtime = useCallback((msg: RealtimeMessage) => {
    if (msg.type !== "scan_status" || !msg.task_id) return;
    if (scanCancelledRef.current) return;
    if ((msg.status === "done" || msg.status === "cancelled") && msg.result) {
      clearScanActivity();
      setView({ mode: "scan", data: msg.result as any });
      setScanTaskId(null);
      setStatusMessage(msg.status === "cancelled" ? "Scan stopped (live)" : "Scan complete (live)");
      setTimeout(() => setStatusMessage(null), 2500);
    } else if (msg.status === "done" && !msg.result) {
      clearScanActivity();
      setScanTaskId(null);
      setView({ mode: "idle" });
    } else if (msg.status === "running" || msg.processed != null) {
      setView({
        mode: "loading",
        label: `Running market scan... (${msg.processed || 0}/${msg.total || "?"})`,
        progress: {
          processed: Number(msg.processed || 0),
          total: Number(msg.total || 0),
          elapsed: Number(msg.elapsed || 0),
        },
      });
    }
  }, []);

  const { connected: wsConnected, subscribeScan } = useStockkyRealtime(handleRealtime);
  useEffect(() => { setWsLive(wsConnected); }, [wsConnected]);



  // Retry, Status and Last Request tracking
  const [isRetrying, setIsRetrying] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const lastRequestType = useRef<"stock" | "scan" | "watchlist_scan" | null>(null);
  const lastSymbol = useRef<string | null>(null);

  useEffect(() => {
    checkBackend();
    // Refresh should restore last MARKET SCAN, not last single-stock analysis.
    (async () => {
      if (sessionStorage.getItem("stockky_scan_task_id")) return;
      try {
        const raw = localStorage.getItem("stockky_last_scan");
        if (raw) {
          const parsed = JSON.parse(raw);
          if (parsed && (parsed.all_results || parsed.recommendations || parsed.scanned != null)) {
            setView({ mode: "scan", data: parsed });
            return;
          }
        }
      } catch {}
      try {
        const last = await api.getLastScan();
        if (last?.ok && last.result) {
          try {
            localStorage.setItem("stockky_last_scan", JSON.stringify(last.result));
          } catch {}
          setView({ mode: "scan", data: last.result });
        }
      } catch {}
    })();
  }, []);

  // Resumes a scan across a page refresh — without this, reloading mid-scan
  // (or right after it finished, before the tab had a chance to show the
  // result) just loses all state and drops back to idle, even though the
  // backend scan is either still running or already sitting there done.
  useEffect(() => {
    const savedTaskId = sessionStorage.getItem("stockky_scan_task_id");
    if (!savedTaskId) return;
    (async () => {
      try {
        const status = await api.scanStatus(savedTaskId);
        if (status.status === "done") {
          sessionStorage.removeItem("stockky_scan_task_id");
          try {
            if (status.result) localStorage.setItem("stockky_last_scan", JSON.stringify(status.result));
          } catch {}
          setView({ mode: "scan", data: status.result! });
        } else if (status.status === "error") {
          sessionStorage.removeItem("stockky_scan_task_id");
        } else if (status.status === "running") {
          setScanTaskId(savedTaskId);
          setView({
            mode: "loading",
            label: `Running market scan... (${status.processed}/${status.total})`,
            progress: {
              processed: status.processed,
              total: status.total,
              elapsed: status.elapsed,
              estimatedRemaining: status.estimated_remaining ?? undefined,
            },
          });
          const interval = window.setInterval(() => pollScanStatus(savedTaskId), 1000);
          pollIntervalRefWs.current = interval;
          setPollInterval(interval);
        }
      } catch {
        // Task not found (Redis TTL expired, ~1hr) or gateway unreachable
        // right now — either way, nothing to resume, so just drop it
        // rather than keep retrying against a task that's gone.
        sessionStorage.removeItem("stockky_scan_task_id");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [pollInterval]);

  function checkBackend() {
    setBackendUp("checking");
    api
      .ping()
      .then(() => {
        setBackendUp("up");
        api.getWatchlist().then((r) => setWatchlist(r.symbols)).catch(() => {});
      })
      .catch(() => setBackendUp("down"));
  }

  async function handleWakeBackend() {
    setIsWaking(true);
    try {
      await wakeService(getApiUrl());
      setTimeout(() => {
        checkBackend();
        setIsWaking(false);
      }, 3000);
    } catch {
      setIsWaking(false);
      checkBackend();
    }
  }

  async function handleSearch(symbol: string) {
    if (!symbol.trim()) return;
    const sym = symbol.trim().toUpperCase();
    try {
      window.alert(
        `Analysis started for ${sym}\n\nFetching quote, technicals, fundamentals, news & decision. This may take a few seconds.`
      );
    } catch {}
    setStatusMessage(`🟢 Analysis started for ${sym}`);
    setTimeout(() => setStatusMessage(null), 5000);

    setTab("dashboard");
    scanCancelledRef.current = true;
    clearScanActivity();
    lastRequestType.current = "stock";
    lastSymbol.current = symbol.trim();
    setView({ mode: "loading", label: `Analysing ${sym}...` });
    setScanTaskId(null);
    setCancelRequested(false);
    try {
      const data = await api.getStock(symbol.trim());
      try { localStorage.setItem("stockky_last_analysis", JSON.stringify(data)); } catch {}
      setView({ mode: "stock", data });
      setQuery("");
      setStatusMessage(`✅ Analysis ready for ${sym}`);
      setTimeout(() => setStatusMessage(null), 4000);
    } catch (e) {
      setView({ mode: "error", message: (e as Error).message });
      setStatusMessage(`❌ Analysis failed for ${sym}`);
      setTimeout(() => setStatusMessage(null), 5000);
    }
  }

  async function handleScan(liteOverride?: boolean) {
    const useLite = typeof liteOverride === "boolean" ? liteOverride : liteScan;
    if (typeof liteOverride === "boolean") setLiteScan(liteOverride);
    const modeLabel = useLite ? "Lite Market Run" : "Full Market Run";
    try {
      window.alert(
        `${modeLabel} started.\n\nStreaming results as they finish (NDJSON).\nYou can press Stop Scan anytime.`
      );
    } catch {}
    setStatusMessage(useLite ? "🟢 Lite Market Run (live stream)" : "🟢 Full Market Run (live stream)");
    setTimeout(() => setStatusMessage(null), 5000);

    scanCancelledRef.current = false;
    lastRequestType.current = "scan";
    setScanTaskId(null);
    setCancelRequested(false);
    setStoppingScan(false);
    setLiveScanRows([]);
    setScanTransport("stream");
    clearScanActivity();

    const ac = new AbortController();
    scanAbortRef.current = ac;

    setView({
      mode: "loading",
      label: `Streaming ${modeLabel.toLowerCase()}…`,
      progress: { processed: 0, total: 0, elapsed: 0 },
    });

    const rows: Decision[] = [];
    let total = 0;
    let processed = 0;
    const t0 = Date.now();

    try {
      for await (const row of api.scanStream({
        lite: useLite,
        forceRefresh: false,
        signal: ac.signal,
      })) {
        if (scanCancelledRef.current) break;

        if (row && row._meta) {
          if (row.event === "feed_bulk_loaded") {
            total = Number(row.total) || total;
            setView({
              mode: "loading",
              label: `Neon feed ${row.feed_hits ?? "?"}/${total} — streaming symbols…`,
              progress: {
                processed,
                total: total || processed,
                elapsed: (Date.now() - t0) / 1000,
              },
            });
          } else if (row.event === "done" || row.event === "cancelled") {
            processed = Number(row.processed) || processed;
            total = Number(row.total) || total;
          }
          continue;
        }

        // Symbol result line — Step 2: upsert by symbol (no duplicates on re-emit)
        const dec = row as Decision;
        if (dec && dec.symbol) {
          const idx = rows.findIndex((s) => s.symbol === dec.symbol);
          if (idx >= 0) rows[idx] = dec;
          else rows.push(dec);
          processed = Number(row._progress?.processed) || rows.length;
          total = Number(row._progress?.total) || total;
          const elapsed = Number(row._progress?.elapsed) || (Date.now() - t0) / 1000;
          // Progressive live list with upsert + soft cap for UI
          setLiveScanRows((prev) => {
            const i = prev.findIndex((s) => s.symbol === dec.symbol);
            let next: Decision[];
            if (i >= 0) {
              next = [...prev];
              next[i] = dec;
            } else {
              next = [...prev, dec];
            }
            return next.length > 80 ? next.slice(-80) : next;
          });
          setView({
            mode: "loading",
            label: `Live scan ${processed}/${total || "?"} — ${dec.symbol} → ${dec.decision || "?"}`,
            progress: {
              processed,
              total: total || processed,
              elapsed,
              estimatedRemaining:
                processed >= 8 && total > processed && elapsed > 0
                  ? Math.min(
                      6 * 3600, // cap display at 6h so early slow symbols don't show 80h
                      ((total - processed) * elapsed) / processed
                    )
                  : undefined,
            },
          });
        }
      }

      const partial = scanCancelledRef.current;
      const result = buildScanResultFromRows(rows, total || rows.length, partial);
      try {
        localStorage.setItem("stockky_last_scan", JSON.stringify(result));
      } catch {}
      setLiveScanRows([]);
      setScanTransport(null);
      scanAbortRef.current = null;
      setView({ mode: "scan", data: result });
      setStatusMessage(partial ? "⏹ Stream stopped — partial results kept" : "✅ Live stream complete");
      setTimeout(() => setStatusMessage(null), 3000);
    } catch (e: any) {
      if (e?.name === "AbortError" || scanCancelledRef.current) {
        const result = buildScanResultFromRows(rows, total || rows.length, true);
        try {
          localStorage.setItem("stockky_last_scan", JSON.stringify(result));
        } catch {}
        setLiveScanRows([]);
        setScanTransport(null);
        scanAbortRef.current = null;
        if (rows.length) {
          setView({ mode: "scan", data: result });
          setStatusMessage("⏹ Stream aborted — partial results kept");
        } else {
          setView({ mode: "idle" });
        }
        setTimeout(() => setStatusMessage(null), 3000);
        return;
      }

      // Fallback: classic async task + poll if stream endpoint unavailable
      console.warn("scanStream failed, falling back to /scan/start", e);
      setScanTransport("poll");
      setStatusMessage("Stream unavailable — falling back to background scan…");
      try {
        const { task_id } = await api.scanStart(false, useLite);
        if (scanCancelledRef.current) return;
        setScanTaskId(task_id);
        try { subscribeScan(task_id); } catch {}
        sessionStorage.setItem("stockky_scan_task_id", task_id);
        const interval = window.setInterval(() => {
          if (scanCancelledRef.current) {
            window.clearInterval(interval);
            return;
          }
          pollScanStatus(task_id);
        }, 1000);
        setPollInterval(interval);
        pollIntervalRefWs.current = interval;
        await pollScanStatus(task_id);
      } catch (e2) {
        clearScanActivity();
        setScanTransport(null);
        setView({ mode: "error", message: (e2 as Error).message || (e as Error).message });
      }
    }
  }


  async function handleScanWatchlist() {
    setView({ mode: "loading", label: "Scanning watchlist..." });
    lastRequestType.current = "watchlist_scan";
    setScanTaskId(null);
    if (pollInterval) clearInterval(pollInterval);
    try {
      const data = await api.scanWatchlist();
      setView({ mode: "scan", data });
    } catch (e) {
      setView({ mode: "error", message: (e as Error).message });
    }
  }

  async function pollScanStatus(taskId: string) {
    if (scanCancelledRef.current) return;
    try {
      const status = await api.scanStatus(taskId);
      if (scanCancelledRef.current) return;
      if (status.status === "running") {
        setView({
          mode: "loading",
          label: `Running market scan... (${status.processed}/${status.total})`,
          progress: {
            processed: status.processed,
            total: status.total,
            elapsed: status.elapsed,
            estimatedRemaining: status.estimated_remaining ?? undefined,
          },
        });
      } else if (status.status === "done") {
        if (pollInterval) clearInterval(pollInterval);
        setPollInterval(null);
        setScanTaskId(null);
        sessionStorage.removeItem("stockky_scan_task_id");
        setView({ mode: "scan", data: status.result! });
      } else if (status.status === "error") {
        if (pollInterval) clearInterval(pollInterval);
        setPollInterval(null);
        setScanTaskId(null);
        sessionStorage.removeItem("stockky_scan_task_id");
        setView({ mode: "error", message: status.error || "Scan failed" });
      }
    } catch (e) {
      console.warn("Polling error", e);
    }
  }

  // Requests cancellation and keeps polling — the backend picks up the
  // cancel flag on its next check (every 3rd completed symbol, so this
  // isn't instant) and finalizes the task as "done" with whatever was
  // scored so far. pollScanStatus already knows how to move to the scan
  // results view once status flips to "done", so this doesn't need its
  // own separate handling for that — just marks the UI as "stopping" and
  // lets the normal poll loop pick up the finalized partial result.
  const [stoppingScan, setStoppingScan] = useState(false);
  const [cancelRequested, setCancelRequested] = useState(false);
  async function handleStopScan() {
    // Stream mode: abort fetch; generator path finalizes partial rows
    if (scanTransport === "stream" || scanAbortRef.current) {
      setCancelRequested(true);
      setStoppingScan(true);
      scanCancelledRef.current = true;
      setStatusMessage("⏹ Stopping live stream — keeping symbols scored so far…");
      setView({
        mode: "loading",
        label: "Stopping stream — finalizing partial results…",
        progress: view.mode === "loading" ? view.progress : { processed: 0, total: 0, elapsed: 0 },
      });
      try { scanAbortRef.current?.abort(); } catch {}
      try { await api.stopAllScans(); } catch {}
      return;
    }

    if (!scanTaskId && !cancelRequested) return;
    const taskId = scanTaskId;
    setCancelRequested(true);
    setStoppingScan(true);
    setStatusMessage("⏹ Stopping — keeping symbols scored so far…");
    setView({
      mode: "loading",
      label: "Stopping scan — finalizing partial results…",
      progress: view.mode === "loading" ? view.progress : { processed: 0, total: 0, elapsed: 0 },
    });

    // Request cancel; keep task id so we can poll for partial result
    if (taskId) {

      try {
        await api.scanCancel(taskId);
        // Belt-and-suspenders: signal every running scan on gateway
        try { await api.stopAllScans(); } catch {}
      } catch (e) {
        console.warn("Cancel request failed", e);
      }
      // Poll briefly for done + result (backend builds partial rankings)
      for (let i = 0; i < 45; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        try {
          const status = await api.scanStatus(taskId);
          if (status.status === "done" && status.result) {
            try {
              localStorage.setItem("stockky_last_scan", JSON.stringify(status.result));
            } catch {}
            clearScanActivity();
            setScanTaskId(null);
            setView({ mode: "scan", data: status.result });
            setStatusMessage(
              status.cancelled || status.partial
                ? "⏹ Scan stopped — partial results shown"
                : "Scan complete"
            );
            setStoppingScan(false);
            setCancelRequested(false);
            setTimeout(() => setStatusMessage(null), 4000);
            return;
          }
          if (status.status === "error") break;
          if (status.status === "running") {
            setView({
              mode: "loading",
              label: `Stopping… (${status.processed}/${status.total})`,
              progress: {
                processed: status.processed,
                total: status.total,
                elapsed: status.elapsed,
              },
            });
          }
        } catch {
          /* keep trying */
        }
      }
    }

    // Fallback: try last cached scan from backend
    try {
      const last = await (api as any).getLastScan();
      if (last?.ok && last.result) {
        localStorage.setItem("stockky_last_scan", JSON.stringify(last.result));
        setView({ mode: "scan", data: last.result });
        setStatusMessage("⏹ Stopped — showing last available scan results");
      } else {
        setView({ mode: "idle" });
        setStatusMessage("⏹ Scan stopped (no partial results yet)");
      }
    } catch {
      setView({ mode: "idle" });
      setStatusMessage("⏹ Scan stopped");
    }
    clearScanActivity();
    setScanTaskId(null);
    setStoppingScan(false);
    setCancelRequested(false);
    setTimeout(() => setStatusMessage(null), 4000);
  }

  const handleRetry = async () => {
    if (isRetrying) return;
    setIsRetrying(true);
    setStatusMessage("⏳ Restarting services and retrying...");

    try {
      if (backendUp === "down") {
        await handleWakeBackend();
        await new Promise(r => setTimeout(r, 3000));
      }
      
      if (lastRequestType.current === 'stock' && lastSymbol.current) {
        await handleSearch(lastSymbol.current);
      } else if (lastRequestType.current === 'scan') {
        await handleScan();
      } else if (lastRequestType.current === 'watchlist_scan') {
        await handleScanWatchlist();
      } else {
        setView({ mode: "idle" });
      }
      setStatusMessage("✅ Retry successful");
    } catch (e) {
      setStatusMessage("❌ Retry failed: " + (e as Error).message);
    } finally {
      setIsRetrying(false);
      setTimeout(() => setStatusMessage(null), 4000);
    }
  };

  async function handleWatchlistUpdate(symbols: string[]) {
    await api.setWatchlist(symbols);
    setWatchlist(symbols);
  }

  async function handleAddToWatchlist(symbol: string) {
    try {
      await api.addToWatchlist(symbol);
      const wl = await api.getWatchlist();
      setWatchlist(wl.symbols);
      setStatusMessage(`✅ Added ${symbol} to watchlist`);
      setTimeout(() => setStatusMessage(null), 3000);
    } catch (e) {
      console.error("Failed to add to watchlist", e);
      setStatusMessage(`❌ Failed to add ${symbol} to watchlist`);
      setTimeout(() => setStatusMessage(null), 3000);
    }
  }

  async function handleAddManyToWatchlist(symbols: string[], label: string) {
    if (symbols.length === 0) {
      setStatusMessage("Nothing to add");
      setTimeout(() => setStatusMessage(null), 3000);
      return;
    }
    try {
      const before = new Set(watchlist);
      const newCount = symbols.filter((s) => !before.has(s.toUpperCase())).length;
      await api.addManyToWatchlist(symbols);
      const wl = await api.getWatchlist();
      setWatchlist(wl.symbols);
      setStatusMessage(
        newCount > 0
          ? `✅ Added ${newCount} new symbol(s) from ${label} (${symbols.length - newCount} already on watchlist)`
          : `All ${symbols.length} symbol(s) from ${label} were already on the watchlist`
      );
      setTimeout(() => setStatusMessage(null), 4000);
    } catch (e) {
      console.error(`Failed to add ${label} to watchlist`, e);
      setStatusMessage(`❌ Failed to add ${label} to watchlist`);
      setTimeout(() => setStatusMessage(null), 3000);
    }
  }

  // Telegram handlers
  async function handleSendTopPicks() {
    if (view.mode !== "scan") return;
    const recs = view.data.recommendations || [];
    if (recs.length === 0) {
      setStatusMessage("❌ No recommendations to send");
      setTimeout(() => setStatusMessage(null), 3000);
      return;
    }
    try {
      await api.sendPicksToTelegram({ type: "top5", recommendations: recs });
      setStatusMessage("✅ Top 5 picks sent to Telegram!");
      setTimeout(() => setStatusMessage(null), 3000);
    } catch (e) {
      setStatusMessage("❌ Failed to send: " + (e as Error).message);
      setTimeout(() => setStatusMessage(null), 5000);
    }
  }

  async function handleSendAllActionable() {
    if (view.mode !== "scan") return;
    const all = view.data.all_results || [];
    const actionable = all.filter(r => r.decision === "BUY NOW" || r.decision === "PREPARE TO BUY");
    if (actionable.length === 0) {
      setStatusMessage("❌ No actionable stocks found");
      setTimeout(() => setStatusMessage(null), 3000);
      return;
    }
    try {
      await api.sendPicksToTelegram({ type: "all_actionable", recommendations: actionable });
      setStatusMessage(`✅ ${actionable.length} picks sent to Telegram!`);
      setTimeout(() => setStatusMessage(null), 3000);
    } catch (e) {
      setStatusMessage("❌ Failed to send: " + (e as Error).message);
      setTimeout(() => setStatusMessage(null), 5000);
    }
  }

  if (!systemReady) {
    return <SystemCheck onReady={() => setSystemReady(true)} />;
  }

  function formatTime(seconds: number): string {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(s / 3600);
    const mins = Math.floor((s % 3600) / 60);
    const secs = s % 60;
    if (hours > 0) return `${hours}h ${mins}m ${secs}s`;
    if (mins > 0) return `${mins}m ${secs}s`;
    return `${secs}s`;
  }

  const cmdActions: CommandAction[] = [
    { id: "dash", label: "Dashboard", group: "Navigate", hint: "tab", run: () => setTab("dashboard") },
    { id: "datafeed", label: "Data Feed", group: "Navigate", hint: "tab", run: () => setTab("datafeed") },
    { id: "hot", label: "Stockky Hot Picks", group: "Navigate", hint: "tab", run: () => setTab("hot") },
    { id: "surprise", label: "Surprise Momentum", group: "Navigate", hint: "tab", run: () => setTab("surprise") },
    { id: "train", label: "Training Lab", group: "Navigate", hint: "tab", run: () => setTab("training") },

    { id: "trades", label: "Trades", group: "Navigate", hint: "tab", run: () => setTab("trades") },
    { id: "alerts", label: "Alerts", group: "Navigate", hint: "tab", run: () => setTab("notifications") },
    { id: "wl", label: "Watchlist", group: "Navigate", hint: "tab", run: () => setTab("watchlist") },
    { id: "settings", label: "Settings", group: "Navigate", hint: "tab", run: () => setTab("settings") },
    { id: "ratelimits", label: "Rate limit monitor", group: "Navigate", hint: "Settings", run: () => setTab("settings") },
    { id: "scan-wl", label: "Scan Watchlist", group: "Action", keywords: "scan", run: () => { setTab("dashboard"); handleScanWatchlist(); } },
    ...(watchlist || []).slice(0, 30).map((s) => ({
      id: `sym-${s}`,
      label: s,
      group: "Watchlist",
      keywords: "stock analyse",
      run: () => { setTab("dashboard"); handleSearch(s); },
    })),
  ];

  const navItems: { id: Tab; label: string; short: string; icon: string }[] = [
    { id: "dashboard", label: "Dashboard", short: "Home", icon: "▣" },
    { id: "datafeed", label: "Data Feed", short: "Feed", icon: "🗄️" },
    { id: "hot", label: "Hot Picks", short: "Picks", icon: "🔥" },
    { id: "surprise", label: "Surprise", short: "Surp", icon: "⚡" },

    { id: "training", label: "Training", short: "Train", icon: "◈" },
    { id: "trades", label: "Trades", short: "Trade", icon: "⇄" },
    { id: "notifications", label: "Alerts", short: "Alerts", icon: "◉" },
    { id: "watchlist", label: "Watchlist", short: "List", icon: "☆" },
    { id: "settings", label: "Settings", short: "Set", icon: "⚙" },
  ];

  return (
    <div className="min-h-screen bg-ink text-paper relative terminal-shell terminal-premium">
      <CommandPalette open={cmdOpen} onClose={() => setCmdOpen(false)} actions={cmdActions} />
      {statusMessage && (
        <div className="fixed top-4 right-4 z-50 bg-graphite border border-signal-buy/40 rounded-lg px-4 py-2.5 shadow-2xl animate-fadeIn flex items-center gap-2">
          <p className="font-mono text-sm text-paper">{statusMessage}</p>
        </div>
      )}

      {/* Desktop sidebar */}
      <aside className="terminal-sidebar hidden md:flex">
        <div className="sidebar-brand">
          <span className="font-display text-lg tracking-tight">Stockky</span>
          <span className="mono text-[9px] text-mist tracking-widest uppercase">Terminal · NSE</span>
          <span className={`mono text-[9px] ${wsLive ? "text-signal-buy" : "text-mist/50"}`}>
            {wsLive ? "● LIVE FEED" : "○ POLLING"}
          </span>
        </div>
        <nav className="sidebar-nav">
          {navItems.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`sidebar-link ${tab === item.id ? "active" : ""}`}
              onClick={() => {
                setTab(item.id as Tab);
                if (item.id === "watchlist") setShowWatchlist(true);
                else setShowWatchlist(false);
                if (item.id !== "settings") setShowSettings(false);
              }}
            >
              <span className="mono text-[11px] opacity-70 w-4">{item.icon}</span>
              {item.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <BackendStatusDot status={backendUp} onClick={() => setTab("settings")} />
          <button
            type="button"
            className="sidebar-link"
            onClick={() => setCmdOpen(true)}
            title="Command palette"
          >
            <span className="kbd">⌘K</span> Command
          </button>
          <button
            type="button"
            className="sidebar-link"
            onClick={() => setShowServiceManager(!showServiceManager)}
          >
            Services
          </button>
          <button
            type="button"
            className="sidebar-link theme-toggle"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            title="Toggle dark / light"
            aria-label="Toggle color theme"
          >
            {theme === "dark" ? "☀ Light mode" : "☾ Dark mode"}
          </button>
        </div>
      </aside>

      {/* Mobile top bar */}
      <header className="terminal-topbar md:hidden">
        <div className="flex items-center gap-2 min-w-0">
          <button
            type="button"
            className="mobile-menu-btn"
            aria-label="Open menu"
            aria-expanded={mobileMoreOpen}
            onClick={() => setMobileMoreOpen(true)}
          >
            <span className="mobile-menu-icon" aria-hidden>☰</span>
          </button>
          <div className="min-w-0">
            <span className="font-display text-base block leading-none">Stockky</span>
            <span className={`mono text-[9px] ${wsLive ? "text-signal-buy" : "text-mist/50"}`}>
              {wsLive ? "● LIVE" : "○ POLL"}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <BackendStatusDot status={backendUp} onClick={() => setTab("settings")} />
          <button type="button" className="btn-terminal text-[10px]" onClick={() => setCmdOpen(true)}>
            ⌘K
          </button>
        </div>
      </header>

      {/* Mobile slide-over menu (professional drawer) */}
      {mobileMoreOpen && (
        <div className="mobile-drawer-root md:hidden" role="dialog" aria-modal="true" aria-label="Menu">
          <button
            type="button"
            className="mobile-drawer-backdrop"
            aria-label="Close menu"
            onClick={() => setMobileMoreOpen(false)}
          />
          <aside className="mobile-drawer-panel">
            <div className="mobile-drawer-head">
              <div>
                <div className="font-display text-lg">Stockky</div>
                <div className="mono text-[9px] text-mist tracking-widest uppercase">Terminal · NSE</div>
              </div>
              <button type="button" className="btn-terminal text-[10px]" onClick={() => setMobileMoreOpen(false)}>
                Close
              </button>
            </div>
            <nav className="mobile-drawer-nav">
              {navItems.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`mobile-drawer-link ${tab === item.id ? "active" : ""}`}
                  onClick={() => {
                    setTab(item.id as Tab);
                    if (item.id === "watchlist") setShowWatchlist(true);
                    else setShowWatchlist(false);
                    if (item.id !== "settings") setShowSettings(false);
                    setMobileMoreOpen(false);
                  }}
                >
                  <span className="opacity-70 w-5 text-center">{item.icon}</span>
                  {item.label}
                </button>
              ))}
            </nav>
            <div className="mobile-drawer-foot">
              <button
                type="button"
                className="mobile-drawer-link"
                onClick={() => { setCmdOpen(true); setMobileMoreOpen(false); }}
              >
                <span className="kbd">⌘K</span> Command
              </button>
              <button
                type="button"
                className="mobile-drawer-link"
                onClick={() => { setShowServiceManager(true); setMobileMoreOpen(false); }}
              >
                Services
              </button>
              <button
                type="button"
                className="mobile-drawer-link"
                onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
              >
                {theme === "dark" ? "☀ Light mode" : "☾ Dark mode"}
              </button>
            </div>
          </aside>
        </div>
      )}

      {/* Bloomberg-style status strip — clickable */}
      <div className="terminal-status-strip hidden md:flex">
        <button type="button" className="pill amber-txt" onClick={() => setTab("dashboard")} title="Go to Dashboard">
          STOCKKY TERMINAL
        </button>
        <button
          type="button"
          className={`pill ${wsLive ? "live" : ""}`}
          onClick={() => setCmdOpen(true)}
          title="Command palette"
        >
          {wsLive ? "LIVE" : "POLL"}
        </button>
        <button
          type="button"
          className={`pill ${backendUp === "down" ? "pill-danger" : backendUp === "up" ? "live" : ""}`}
          onClick={() => {
            if (backendUp === "down") handleWakeBackend();
            else setTab("settings");
          }}
          title={backendUp === "down" ? "Wake backend" : "Settings"}
        >
          BACKEND {backendUp === "up" ? "UP" : backendUp === "down" ? "DOWN" : "…"}
        </button>
        <button type="button" className="pill" onClick={() => setCmdOpen(true)} title="Jump anywhere">
          TAB {tab.toUpperCase()}
        </button>
        <button
          type="button"
          className="pill"
          onClick={() => setShowServiceManager(true)}
          title="Service manager"
        >
          SERVICES
        </button>
        <span className="mono text-[10px] text-mist/60 ml-auto status-clock">
          {new Date().toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })} IST
        </span>
      </div>

      {showServiceManager && (
        <ServiceManager onClose={() => setShowServiceManager(false)} />
      )}

      {backendUp === "down" && !showSettings && (
        <div className="border-b border-signal-sell/30 bg-signal-sell/5">
          <div className="max-w-6xl mx-auto px-4 sm:px-6 py-3 flex items-center justify-between gap-3 flex-wrap">
            <p className="font-mono text-xs text-signal-sell">
              Can't reach the backend at <span className="text-signal-sell/80">{getApiUrl() || "(not set)"}</span>.
            </p>
            <div className="flex gap-3">
              <button
                onClick={handleWakeBackend}
                disabled={isWaking}
                className="font-mono text-xs text-paper bg-signal-prepare/20 border border-signal-prepare/40 rounded-lg px-4 py-1.5 hover:bg-signal-prepare/30 transition disabled:opacity-50"
              >
                {isWaking ? "Waking..." : "Wake Backend"}
              </button>
              <button onClick={checkBackend} className="font-mono text-xs text-mist hover:text-paper underline">
                Retry
              </button>
              <button
                onClick={() => setShowSettings(true)}
                className="font-mono text-xs text-signal-sell hover:text-paper underline"
              >
                Fix in Settings
              </button>
            </div>
          </div>
        </div>
      )}

      {showWatchlist && tab === "dashboard" && (
        <div className="border-b border-slate/60 bg-graphite">
          <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6">
            <WatchlistManager
              symbols={watchlist}
              onChange={handleWatchlistUpdate}
              onAnalyse={(s) => {
                setShowWatchlist(false);
                handleSearch(s);
              }}
              onScanWatchlist={() => {
                setShowWatchlist(false);
                handleScanWatchlist();
              }}
            />
          </div>
        </div>
      )}

      <main className="terminal-main max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-8 pb-24 md:pb-8">
        {tab === "notifications" ? (
          <div className="page-terminal">
            <p className="dash-section-title">Alerts</p>
            <NotificationsPanel />
          </div>
        ) : tab === "training" ? (
          <div className="page-terminal">
            <p className="dash-section-title">Training Lab</p>
            <Training />
          </div>
        ) : tab === "trades" ? (
          <div className="page-terminal">
            <p className="dash-section-title">Paper Trades</p>
            <Trades />
          </div>
        ) : tab === "datafeed" ? (
          <div className="page-terminal">
            <p className="dash-section-title">Data Feed</p>
            <DataFeed />
            <DataHealthAudit />
          </div>
        ) : tab === "hot" ? (
          <div className="page-terminal">
            <p className="dash-section-title">Hot Picks</p>
            <HotStocks
              onAnalyze={(s) => {
                setTab("dashboard");
                handleSearch(s);
              }}
            />
          </div>
        ) : tab === "surprise" ? (
          <div className="page-terminal">
            <p className="dash-section-title">Surprise Momentum</p>
            <SurpriseStocks
              onSelect={(s) => {
                setTab("dashboard");
                handleSearch(s);
              }}
            />
          </div>
        ) : tab === "settings" ? (
          <SettingsPage
            backendUp={backendUp}
            onSaved={checkBackend}
            theme={theme}
            setTheme={setTheme}
            onOpenServices={() => setShowServiceManager(true)}
          />
        ) : tab === "watchlist" ? (
          <div className="page-terminal">
            <p className="dash-section-title">Watchlist</p>
            <WatchlistManager
              symbols={watchlist}
              onChange={handleWatchlistUpdate}
              onAnalyse={(s) => {
                setTab("dashboard");
                handleSearch(s);
              }}
              onScanWatchlist={() => {
                setTab("dashboard");
                handleScanWatchlist();
              }}
            />
          </div>
        ) : (
          <>
            {/* Dashboard content — terminal-style */}
            <section className="mb-6 sm:mb-8 dash-terminal">
              <div className="dash-hero terminal-panel">
                <div className="dash-hero-top">
                  <div>
                    <p className="dash-section-title">Market terminal</p>
                    <MarketClock />
                  </div>
                  <button type="button" className="btn-terminal" onClick={() => setCmdOpen(true)}>
                    Ctrl+K
                  </button>
                </div>
                <h1 className="dash-hero-title">
                  Know your next move. <span className="num-amber">In one call.</span>
                </h1>
                <p className="dash-hero-sub">
                  Technical, fundamental, news and AI signals — one decision.
                  Press <span className="kbd">Ctrl</span>+<span className="kbd">K</span> to jump.
                </p>

                <div className="dash-search-row">
                  <div className="dash-search">
                    <span className="dash-search-prefix">NSE</span>
                    <input
                      value={query}
                      onChange={(e) => setQuery(e.target.value.toUpperCase())}
                      onKeyDown={(e) => e.key === "Enter" && handleSearch(query)}
                      placeholder="TCS, INFY, RELIANCE…"
                      autoComplete="off"
                      spellCheck={false}
                      aria-label="Symbol search"
                    />
                    <button
                      type="button"
                      onClick={() => handleSearch(query)}
                      className="btn-primary-term shrink-0"
                      disabled={!query.trim()}
                    >
                      Analyse
                    </button>
                  </div>
                  <div className="dash-search-actions dash-scan-pair">
                    <button
                      type="button"
                      onClick={() => handleScan(true)}
                      className="btn-scan-lite whitespace-nowrap"
                      title="Lite Market Scan — Neon feed + instant scores, minimal downstream load (recommended on free tier)"
                    >
                      ⚡ Lite Market Scan
                    </button>
                    <button
                      type="button"
                      onClick={() => handleScan(false)}
                      className="btn-scan-full whitespace-nowrap"
                      title="Full Market Scan — all pillars (technical, fundamental, news, events, prediction)"
                    >
                      ▶ Run Market Scan
                    </button>
                  </div>
                </div>
              </div>

              {/* ── NEW: Market Sentiment Header placed above Market Movers ── */}
              <div className="mt-8">
                <MarketSentimentHeader />
              </div>

              <div className="mt-8">
                <MarketMovers onSelect={handleSearch} />
              </div>

              {watchlist.length > 0 && view.mode === "idle" && (
                <div className="flex flex-wrap gap-2 mt-4">
                  {watchlist.slice(0, 10).map((s) => (
                    <button
                      key={s}
                      onClick={() => handleSearch(s)}
                      className="font-mono text-[11px] text-mist hover:text-paper border border-slate/60 hover:border-mist/60 rounded-md px-2.5 py-1 transition"
                    >
                      {s}
                    </button>
                  ))}
                  {watchlist.length > 10 && (
                    <span className="font-mono text-[11px] text-mist/40 py-1">
                      +{watchlist.length - 10} more
                    </span>
                  )}
                </div>
              )}
            </section>

            <section>
              {view.mode === "idle" && (
                <>
                  <BreadthStrip mood={null} />
                  <div className="border border-dashed border-slate rounded-xl p-10 sm:p-16 text-center mb-4">
                    <p className="text-mist/40 font-mono text-xs">
                      Search a symbol or run Market Scan. Last scan results restore after refresh.
                    </p>
                  </div>
                </>
              )}

              {view.mode === "loading" && (
                <div className="w-full max-w-2xl space-y-4">
                  <div className="rounded-xl border border-slate bg-graphite p-6 sm:p-8">
                    <p className="font-mono text-xs text-mist mb-2">{view.label}</p>
                    {view.progress && (
                      <div className="mt-4 space-y-3">
                        {(() => {
                          const p = view.progress;
                          const pct = Math.min(100, Math.round((p.processed / Math.max(1, p.total)) * 100));
                          let rem = p.estimatedRemaining;
                          if ((rem === undefined || rem === null || rem <= 0) && p.processed >= 8 && p.total > p.processed && p.elapsed > 0) {
                            const avg = Math.min(30, Math.max(p.elapsed / p.processed, 0.3)); // clamp 0.3–30s/symbol
                            rem = Math.min(6 * 3600, (p.total - p.processed) * avg);
                          }
                          return (
                            <>
                              <div className="flex justify-between items-end font-mono text-[11px]">
                                <div>
                                  <span className="text-mist/50">Processed </span>
                                  <span className="text-paper font-semibold">{p.processed}</span>
                                  <span className="text-mist/40"> / {p.total}</span>
                                </div>
                                <div className="text-right">
                                  <span className="text-signal-prepare text-sm font-semibold">{pct}%</span>
                                  <span className="text-mist/50 ml-2">⏱️ {formatTime(p.elapsed)}</span>
                                </div>
                              </div>
                              <div className="relative w-full h-2.5 bg-ink/80 border border-slate/50 rounded-full overflow-hidden">
                                <div
                                  className="h-full rounded-full bg-gradient-to-r from-signal-prepare/80 via-sky-400/70 to-signal-prepare transition-all duration-500 ease-out"
                                  style={{ width: `${pct}%` }}
                                />
                                <div
                                  className="absolute inset-0 opacity-30 pointer-events-none"
                                  style={{
                                    background:
                                      "linear-gradient(90deg, transparent, rgba(255,255,255,0.15), transparent)",
                                    backgroundSize: "200% 100%",
                                    animation: pct < 100 ? "stockky-shimmer 1.6s linear infinite" : "none",
                                  }}
                                />
                              </div>
                              <div className="flex justify-between font-mono text-[10px] text-mist/50">
                                <span>
                                  {scanTransport === "stream"
                                    ? "● NDJSON stream"
                                    : wsLive
                                      ? "● Live WS"
                                      : "○ HTTP poll"}
                                </span>
                                <span>
                                  {rem != null && rem > 0
                                    ? `Est. remaining ${formatTime(rem)}`
                                    : p.processed === 0
                                      ? "Calculating…"
                                      : p.processed >= p.total
                                        ? "Finishing…"
                                        : ""}
                                </span>
                              </div>
                            </>
                          );
                        })()}
                      </div>
                    )}
                    {(scanTaskId || scanTransport === "stream") && (
                      <button
                        onClick={handleStopScan}
                        disabled={stoppingScan || cancelRequested}
                        className="mt-4 font-mono text-xs text-signal-avoid border border-signal-avoid/40 rounded-lg px-3 py-2 hover:bg-signal-avoid/10 transition disabled:opacity-50 w-full"
                      >
                        {cancelRequested ? "Stopping — finishing up..." : "⏹ Stop Scan"}
                      </button>
                    )}
                    <div className="mt-6">
                      <Pipeline running={true} />
                    </div>
                  </div>

                  {/* Live results strip while streaming */}
                  {scanTransport === "stream" && liveScanRows.length > 0 && (
                    <div className="rounded-xl border border-slate/80 bg-graphite/80 p-4">
                      <div className="flex items-center justify-between mb-3">
                        <p className="font-mono text-[10px] uppercase tracking-widest text-mist/60">
                          Live results
                        </p>
                        <p className="font-mono text-[10px] text-mist/40">
                          last {Math.min(liveScanRows.length, 12)} of {liveScanRows.length}
                        </p>
                      </div>
                      <div className="space-y-1 max-h-64 overflow-y-auto pr-1">
                        {[...liveScanRows].slice(-12).reverse().map((r, i) => {
                          const d = String(r.decision || "?");
                          const color =
                            d.includes("BUY")
                              ? "text-signal-buy"
                              : d === "SELL"
                                ? "text-signal-sell"
                                : d === "ERROR"
                                  ? "text-signal-avoid"
                                  : "text-mist";
                          return (
                            <button
                              key={r.symbol || `row-${i}`}
                              type="button"
                              onClick={() => r.symbol && handleSearch(r.symbol)}
                              className="w-full flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 hover:bg-ink/50 transition text-left"
                            >
                              <span className="font-mono text-xs text-paper truncate">{r.symbol}</span>
                              <span className={`font-mono text-[10px] shrink-0 ${color}`}>{d}</span>
                              <span className="font-mono text-[10px] text-mist/40 shrink-0 w-10 text-right">
                                {r.combined_score != null ? Math.round(Number(r.combined_score)) : "—"}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
              )}


              {view.mode === "error" && (
                <div className="rounded-xl border border-signal-sell/40 bg-signal-sell/5 p-6">
                  <p className="font-mono text-xs text-signal-sell/70 uppercase tracking-widest mb-1">
                    Error
                  </p>
                  <p className="text-sm text-signal-sell break-words">{view.message}</p>
                  {view.message.includes("timeout") || view.message.includes("reach") && (
                    <p className="text-xs text-mist/60 mt-2">
                      ⏳ Free‑tier services may take up to 60 seconds to wake up on the first request.
                      Wait a moment and try again.
                    </p>
                  )}
                  <div className="flex gap-4 mt-4">
                    <button
                      onClick={handleRetry}
                      disabled={isRetrying}
                      className="font-mono text-xs text-paper bg-signal-prepare/20 border border-signal-prepare/40 rounded-lg px-4 py-1.5 hover:bg-signal-prepare/30 transition disabled:opacity-50"
                    >
                      {isRetrying ? "⏳ Restarting..." : (scanTaskId ? "Resume Scan" : "Try again")}
                    </button>
                    <button
                      onClick={() => setShowSettings(true)}
                      className="font-mono text-xs text-mist hover:text-paper underline"
                    >
                      Check backend settings
                    </button>
                  </div>
                </div>
              )}

              {view.mode === "stock" && (
                <DecisionCard
                  data={view.data}
                  onBack={() => setView({ mode: "idle" })}
                  onSearchRelated={handleSearch}
                  onAddToWatchlist={handleAddToWatchlist}
                />
              )}

              {view.mode === "scan" && (
                <>
                  <BreadthStrip
                    mood={(view.data as any)?.market_mood}
                    advancing={(view.data as any)?.market_stats?.buy_signals}
                    declining={(view.data as any)?.market_stats?.sell_signals}
                  />
                  <SignalStream
                    rows={
                      ((view.data as any)?.recommendations ||
                        (view.data as any)?.all_results ||
                        []) as any[]
                    }
                    partial={Boolean((view.data as any)?.partial || (view.data as any)?.stopped_early)}
                    filter={signalFilter}
                    onFilter={setSignalFilter}
                    onSelect={(sym) => handleSearch(sym)}
                    title="Signal Stream & Audit"
                  />
                  <MultiHorizonScanLists data={(view as any).data} />
                  <ScanPanel
                    result={view.data}
                    onSelect={handleSearch}
                    onBack={() => setView({ mode: "idle" })}
                    onAddToWatchlist={handleAddToWatchlist}
                    onAddManyToWatchlist={handleAddManyToWatchlist}
                    onSendTopPicks={handleSendTopPicks}
                    onSendAllActionable={handleSendAllActionable}
                  />
                </>
              )}
            </section>
          </>
        )}
      </main>

      
      {/* Mobile bottom navigation */}
      <nav className="terminal-bottom-nav md:hidden" aria-label="Primary">
        {navItems.filter((i) => i.id !== "settings").slice(0, 5).map((item) => (
          <button
            key={item.id}
            type="button"
            className={tab === item.id ? "active" : ""}
            onClick={() => setTab(item.id)}
          >
            <span className="block text-[12px] mb-0.5">{item.icon}</span>
            <span>{item.short}</span>
          </button>
        ))}
        <button
          type="button"
          className={mobileMoreOpen || tab === "settings" ? "active" : ""}
          onClick={() => setMobileMoreOpen(true)}
        >
          <span className="block text-[12px] mb-0.5">☰</span>
          <span>More</span>
        </button>
      </nav>

      <footer className="md:ml-[200px] px-4 sm:px-6 py-5 border-t border-slate/40 mt-8 pb-20 md:pb-5">
        <p className="text-[10px] text-mist/40 font-mono tracking-wide uppercase">
          Stockky Terminal · Informational only — not investment advice · Verify before trading
        </p>
      </footer>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`font-mono text-xs uppercase tracking-widest px-3 py-2 rounded-lg transition ${
        active ? "text-paper bg-slate/60" : "text-mist hover:text-paper"
      }`}
    >
      {children}
    </button>
  );
}

function BackendStatusDot({
  status,
  onClick,
}: {
  status: "checking" | "up" | "down";
  onClick: () => void;
}) {
  const color =
    status === "up" ? "bg-signal-buy" : status === "down" ? "bg-signal-sell" : "bg-signal-hold animate-pulse";
  const label = status === "up" ? "Backend connected" : status === "down" ? "Backend unreachable" : "Checking...";
  return (
    <button
      onClick={onClick}
      title={label}
      className="hidden sm:flex items-center gap-1.5 font-mono text-[10px] text-mist/60 hover:text-mist transition px-1"
    >
      <span className={`w-1.5 h-1.5 rounded-full ${color}`} />
      {label}
    </button>
  );
}



function MarketClock() {
  const [now, setNow] = useState(() => new Date());
  const [phase, setPhase] = useState<string | null>(null);
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);
  useEffect(() => {
    let c = false;
    (async () => {
      try {
        const { getApiUrl } = await import("./api");
        const base = getApiUrl();
        if (!base) return;
        const res = await fetch(`${base.replace(/\/$/, "")}/market/session`);
        if (!c && res.ok) {
          const j = await res.json();
          setPhase(j.phase || null);
        }
      } catch { /* ignore */ }
    })();
    return () => { c = true; };
  }, []);
  const ist = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
    weekday: "short",
  }).format(now);
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    weekday: "short",
  }).formatToParts(now);
  const map: Record<string, string> = {};
  for (const p of parts) if (p.type !== "literal") map[p.type] = p.value;
  const hm = parseInt((map.hour || "0") + (map.minute || "0"), 10);
  const day = map.weekday || "";
  const weekday = !["Sat", "Sun"].includes(day);
  const open = phase ? phase === "open" : weekday && hm >= 915 && hm <= 1530;
  const pre = phase ? phase === "preopen" : weekday && hm >= 830 && hm < 915;
  const post = phase ? phase === "post" : weekday && hm > 1530 && hm <= 1600;
  const holiday = phase === "holiday";
  const label = holiday ? "NSE HOLIDAY" : open ? "NSE OPEN" : pre ? "PRE-OPEN" : post ? "POST" : "NSE CLOSED";
  const color = holiday ? "text-signal-sell" : open ? "text-signal-buy" : pre || post ? "text-amber-300" : "text-mist/60";
  return (
    <div className="market-clock mono text-[11px] flex flex-wrap gap-3 mb-2">
      <span className={color}>{label}</span>
      <span className="text-mist/70">{ist} IST</span>
      <span className="text-mist/50">Session 09:15–15:30 · Full warm 08:30–16:00</span>
    </div>
  );
}

function SettingsPage({
  backendUp,
  onSaved,
  theme,
  setTheme,
  onOpenServices,
}: {
  backendUp: "checking" | "up" | "down";
  onSaved: () => void;
  theme: "dark" | "light";
  setTheme: (t: "dark" | "light") => void;
  onOpenServices: () => void;
}) {
  const [url, setUrl] = useState(getApiUrl());
  const [savedMsg, setSavedMsg] = useState<string | null>(null);

  function save() {
    setApiUrl(url);
    onSaved();
    setSavedMsg("API URL saved on this device.");
    setTimeout(() => setSavedMsg(null), 3000);
  }

  return (
    <div className="page-terminal settings-page space-y-4">
      <p className="dash-section-title">Settings</p>
      <p className="text-xs text-mist/60 mb-2">API connection · theme · services · deploy checklist</p>
      <section className="terminal-panel">
        <h3 className="mono text-xs text-mist uppercase tracking-widest mb-2">Backend connection</h3>
        <p className="text-mist/70 text-xs mb-3 max-w-lg">
          API Gateway public URL. Required when frontend and backend are deployed separately.
          Status:{" "}
          <strong className={backendUp === "up" ? "text-signal-buy" : backendUp === "down" ? "text-signal-sell" : "text-mist"}>
            {backendUp}
          </strong>
        </p>
        <div className="flex flex-col sm:flex-row gap-2">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && save()}
            placeholder="https://your-api-gateway.onrender.com"
            className="flex-1 bg-ink/60 border border-slate rounded-lg px-3 py-2 font-mono text-xs text-paper placeholder:text-mist/30 outline-none focus:border-cyan-500/50"
            spellCheck={false}
            autoComplete="off"
          />
          <button type="button" onClick={save} className="btn-terminal">
            Save URL
          </button>
        </div>
        {savedMsg && <p className="mono text-xs text-signal-buy mt-2 mb-0">{savedMsg}</p>}
      </section>

      <section className="terminal-panel">
        <h3 className="mono text-xs text-mist uppercase tracking-widest mb-2">Appearance</h3>
        <button type="button" className="btn-terminal" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
          Switch to {theme === "dark" ? "light" : "dark"} mode
        </button>
      </section>

      <section className="terminal-panel">
        <h3 className="mono text-xs text-mist uppercase tracking-widest mb-2">Services</h3>
        <p className="text-mist/70 text-xs mb-3">Wake or inspect microservice health.</p>
        <div className="flex flex-wrap gap-2 mb-2">
          <button
            type="button"
            className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-red-600/90 hover:bg-red-500 text-white border border-red-400/40"
            onClick={() => powerOffAll()}
            title="Stop scan, data-feed, hot-picks; reset UI"
          >
            ⏻ Power Off
          </button>
          <button type="button" className="btn-terminal" onClick={onOpenServices}>
            Open Service Manager
          </button>
        </div>
      </section>

      <section className="terminal-panel">
        <RateLimitDashboard />
      </section>

      <section className="terminal-panel">
        <h3 className="mono text-xs text-mist uppercase tracking-widest mb-2">Deploy checklist</h3>
        <ul className="text-xs text-mist/80 space-y-1.5 mono pl-4 list-disc">
          <li>Set VITE_API_URL at frontend build time to your API Gateway</li>
          <li>Set DATABASE_URL on decision-prediction-service (Neon/Supabase Postgres)</li>
          <li>Set UPSTASH_REDIS_REST_URL + TOKEN on all services</li>
          <li>Replace placeholder *.onrender.com URLs with your real service URLs</li>
          <li>Optional: CALLMEBOT_USER / CALLMEBOT_USERS for voice alerts</li>
        </ul>
      </section>
    </div>
  );
}

function SettingsBanner({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [url, setUrl] = useState(getApiUrl());

  function save() {
    setApiUrl(url);
    onSaved();
    onClose();
  }

  return (
    <div className="border-b border-slate/60 bg-graphite">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-5">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="flex-1 min-w-[260px]">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
              Backend connection
            </h3>
            <p className="text-mist/70 text-xs mb-3 max-w-md">
              This is the URL of your deployed API Gateway service. If the app shows "Failed to
              fetch", it usually means this wasn't set when the frontend was built -- set it here
              once and it's remembered on this device.
            </p>
            <div className="flex gap-2">
              <input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && save()}
                placeholder="https://api-gateway-puwd.onrender.com"
                className="flex-1 bg-ink/60 border border-slate rounded-lg px-3 py-2 font-mono text-xs text-paper placeholder:text-mist/30 outline-none focus:border-signal-prepare/60 transition"
                spellCheck={false}
                autoComplete="off"
              />
              <button
                onClick={save}
                className="border border-slate rounded-lg px-4 py-2 font-mono text-xs text-mist hover:text-paper hover:border-signal-prepare/60 transition"
              >
                Save
              </button>
            </div>
          </div>
          <button
            onClick={onClose}
            className="font-mono text-xs text-mist hover:text-paper underline shrink-0"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}