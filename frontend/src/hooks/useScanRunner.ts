import { useState, useEffect, useRef, useCallback } from "react";
import { api, Decision } from "../api";
import { buildScanResultFromRows } from "./scanResult";
import { useStockkyRealtime, RealtimeMessage } from "../useRealtime";
import type { Tab, ViewState } from "../components/app/appTypes";

// E70 - E31 step 14: everything that starts, follows, stops and retries an analysis or a market scan, moved out of App.tsx (code unchanged):
// the view state, stream / poll / realtime transports, resume after refresh, Stop, Retry and the two "send to Telegram" buttons.
// App keeps the toast message, the tab and the search box; they come in as arguments.
export function useScanRunner({ setStatusMessage, setTab, setQuery, backendUp, handleWakeBackend }: {
  setStatusMessage: (m: string | null) => void;
  setTab: (t: Tab) => void;
  setQuery: (q: string) => void;
  backendUp: "checking" | "up" | "down";
  handleWakeBackend: () => Promise<void>;
}) {
  const [liteScan, setLiteScan] = useState(true);
  const [view, setView] = useState<ViewState>({ mode: "idle" });
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
  const lastRequestType = useRef<"stock" | "scan" | "watchlist_scan" | null>(null);
  const lastSymbol = useRef<string | null>(null);

  useEffect(() => {
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
        // Full runs must rebuild universe; lite can use cache but still needs full list
        forceRefresh: !useLite,
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
      // If stream ended early (proxy timeout / free-tier), continue via background scan
      const expected = total || 0;
      const got = rows.length;
      const earlyExit = expected > 40 && got > 0 && got < Math.floor(expected * 0.85);
      if (earlyExit && !scanCancelledRef.current) {
        console.warn(`Stream partial ${got}/${expected} — continuing via /scan/start`);
        setStatusMessage(`Stream reached ${got}/${expected} — continuing full scan in background…`);
        setScanTransport("poll");
        try {
          const { task_id } = await api.scanStart(true, useLite);
          if (task_id) {
            setScanTaskId(task_id);
            try { subscribeScan(task_id); } catch {}
            sessionStorage.setItem("stockky_scan_task_id", task_id);
            const interval = window.setInterval(() => {
              if (scanCancelledRef.current) {
                window.clearInterval(interval);
                return;
              }
              pollScanStatus(task_id);
            }, 1500);
            setPollInterval(interval);
            pollIntervalRefWs.current = interval;
            await pollScanStatus(task_id);
            return;
          }
        } catch (contErr) {
          console.warn("continue scanStart failed", contErr);
        }
      }
      const result = buildScanResultFromRows(rows, total || rows.length, partial || earlyExit);
      try {
        localStorage.setItem("stockky_last_scan", JSON.stringify(result));
      } catch {}
      setLiveScanRows([]);
      setScanTransport(null);
      // BUG FIX: only clear the ref if it's still THIS call's controller.
      // Same race as SurpriseStocks.tsx's fetchSurpriseStocks — if a newer
      // scan superseded this one (clearScanActivity() aborted this call and
      // installed a new controller before this call's own unwind reached
      // here), unconditionally nulling scanAbortRef.current would wipe out
      // the NEW scan's controller, leaving it unabortable.
      if (scanAbortRef.current === ac) scanAbortRef.current = null;
      setView({ mode: "scan", data: result });
      setStatusMessage(
        partial || earlyExit
          ? `⏹ Partial scan ${got || rows.length}/${expected || "?"} — results kept`
          : "✅ Live stream complete"
      );
      setTimeout(() => setStatusMessage(null), 4000);
    } catch (e: any) {
      if (e?.name === "AbortError" || scanCancelledRef.current) {
        const result = buildScanResultFromRows(rows, total || rows.length, true);
        try {
          localStorage.setItem("stockky_last_scan", JSON.stringify(result));
        } catch {}
        setLiveScanRows([]);
        setScanTransport(null);
        // Same guard as the success path above — don't clear a newer call's
        // controller out from under it.
        if (scanAbortRef.current === ac) scanAbortRef.current = null;
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

  return {
    view, setView, liveScanRows, scanTransport, scanTaskId, isRetrying, cancelRequested, stoppingScan, wsLive,
    handleSearch, handleScan, handleScanWatchlist, handleStopScan, handleRetry, handleSendTopPicks, handleSendAllActionable,
  };
}
