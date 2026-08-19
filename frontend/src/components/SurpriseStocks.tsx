// frontend/src/components/SurpriseStocks.tsx
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";

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

function formatEta(sec?: number | null) {
  if (sec == null || !Number.isFinite(sec)) return "—";
  const s = Math.max(0, Math.round(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m > 0 ? `${m}m ${r}s` : `${r}s`;
}

export default function SurpriseStocks({
  onSelect,
}: {
  onSelect?: (symbol: string) => void;
}) {
  const [stocks, setStocks] = useState<SurpriseStock[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [meta, setMeta] = useState<{
    static_loaded?: number;
    quotes_ok?: number;
    universe_scanned?: number;
    elapsed_sec?: number;
  } | null>(null);
  const [lastAt, setLastAt] = useState<string | null>(null);

  const [pmRunning, setPmRunning] = useState(false);
  const [pmProgress, setPmProgress] = useState<PremarketProgress | null>(null);
  const [pmError, setPmError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

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
        // Refresh scan after baselines ready
        setTimeout(() => fetchSurpriseStocks(true), 500);
      }
      if (st?.stage === "error") {
        setPmError(st.error || st.message || "Premarket failed");
        setPmRunning(false);
        stopPmPoll();
      }
    } catch (e: any) {
      // soft — keep polling while running
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
      const res = await api.surprisePremarket();
      if (res?.already_running) {
        setPmProgress((res.progress as PremarketProgress) || pmProgress);
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

  const fetchSurpriseStocks = useCallback(async (force = false) => {
    setLoading(true);
    setError(null);
    const ac = new AbortController();
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
            if (row.event === "static_loaded") {
              setMeta((m) => ({ ...m, static_loaded: row.static_loaded }));
            }
            if (row.event === "error") {
              setError(String(row.error || "stream error"));
            }
            if (row.event === "done") {
              setMeta({
                static_loaded: row.universe ?? live.length,
                quotes_ok: row.universe,
                universe_scanned: row.universe,
                elapsed_sec: row.elapsed,
              });
            }
            continue;
          }
          if (row?.symbol) {
            live.push(row as SurpriseStock);
            const sorted = [...live].sort((a, b) => b.score - a.score);
            setStocks(sorted);
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
    }
  }, []);

  useEffect(() => {
    fetchSurpriseStocks(false);
    const interval = window.setInterval(() => fetchSurpriseStocks(false), 60_000);
    // Resume progress if a job is already running
    pollPremarket();
    return () => {
      window.clearInterval(interval);
      stopPmPoll();
    };
  }, [fetchSurpriseStocks, pollPremarket]);

  const pct = Math.min(100, Math.max(0, Number(pmProgress?.percent ?? 0)));

  return (
    <div className="rounded-xl border border-slate bg-graphite p-4 sm:p-6">
      <div className="flex flex-wrap justify-between items-start gap-4 mb-5">
        <div>
          <h2 className="font-display text-xl text-emerald-400/95">
            ⚡ Surprise Momentum Stocks
          </h2>
          <p className="font-mono text-[11px] text-mist/60 mt-1 max-w-xl">
            High RVOL, ORB breakouts & range expansion vs pre-market Neon baselines.
            Free-tier safe: one SQL load + bounded live quotes.
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
            title="Rebuild Neon surprise_static_feed (if GitHub cron was missed)"
          >
            {pmRunning ? "Premarket running…" : "🛠 Run Premarket Feed"}
          </button>
          <button
            type="button"
            onClick={() => fetchSurpriseStocks(true)}
            disabled={loading}
            className="font-mono text-xs px-4 py-2 rounded-lg bg-emerald-600/30 text-emerald-300 border border-emerald-500/40 hover:bg-emerald-600/45 transition disabled:opacity-50"
          >
            {loading ? "Scanning…" : "Refresh Scan"}
          </button>
        </div>
      </div>

      {/* Premarket pipeline progress */}
      {(pmRunning || (pmProgress && pmProgress.stage && pmProgress.stage !== "idle")) && (
        <div className="mb-5 rounded-xl border border-amber-500/30 bg-amber-500/5 px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
            <p className="font-mono text-[11px] text-amber-200">
              Premarket pipeline · {pmProgress?.stage || "—"}
              {pmProgress?.current_symbol ? (
                <span className="text-mist/60"> · {pmProgress.current_symbol}</span>
              ) : null}
            </p>
            <p className="font-mono text-[10px] text-mist/60">
              {pmProgress?.processed ?? 0}/{pmProgress?.total ?? "—"} · elapsed{" "}
              {formatEta(pmProgress?.elapsed_sec)} · ETA {formatEta(pmProgress?.eta_sec)}
            </p>
          </div>
          <div className="h-2 rounded-full bg-ink/60 overflow-hidden border border-slate/50">
            <div
              className="h-full bg-gradient-to-r from-amber-500/80 to-emerald-500/80 transition-all duration-500"
              style={{ width: `${pct}%` }}
            />
          </div>
          <p className="font-mono text-[10px] text-mist/50 mt-1.5">
            {pct}% · {pmProgress?.message || "…"}
            {pmProgress?.errors ? ` · errors ${pmProgress.errors}` : ""}
          </p>
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
          {String(error).toLowerCase().includes("premarket") ||
          String(error).toLowerCase().includes("empty") ? (
            <span className="block mt-1 text-mist/60">
              Run <strong>Premarket Feed</strong> first (or wait for the 08:55 GitHub Action).
            </span>
          ) : null}
        </div>
      )}

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
              <tr
                key={s.symbol}
                className="border-b border-slate/40 hover:bg-ink/40 text-sm transition"
              >
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
                  ₹{Number(s.price).toFixed(2)}
                </td>
                <td className="py-2.5 px-3 font-mono text-xs font-semibold text-emerald-400">
                  +{Number(s.change_pct).toFixed(2)}%
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
                  No surprise breakouts meeting volume / score thresholds right now.
                </td>
              </tr>
            )}
            {loading && stocks.length === 0 && (
              <tr>
                <td colSpan={8} className="py-10 text-center font-mono text-xs text-mist/50">
                  Scanning universe…
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
