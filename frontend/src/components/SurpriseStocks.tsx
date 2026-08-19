// frontend/src/components/SurpriseStocks.tsx
import { useCallback, useEffect, useState } from "react";
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

  const fetchSurpriseStocks = useCallback(async (force = false) => {
    setLoading(true);
    setError(null);
    const ac = new AbortController();
    try {
      // Prefer NDJSON stream so rows appear progressively
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
            // progressive UI (cap sort each update)
            const sorted = [...live].sort((a, b) => b.score - a.score);
            setStocks(sorted);
          }
        }
      } catch (streamErr: any) {
        if (streamErr?.name === "AbortError") return;
        if (!usedStream) {
          // Fallback to single JSON response
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
    return () => window.clearInterval(interval);
  }, [fetchSurpriseStocks]);

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
        <button
          type="button"
          onClick={() => fetchSurpriseStocks(true)}
          disabled={loading}
          className="font-mono text-xs px-4 py-2 rounded-lg bg-emerald-600/30 text-emerald-300 border border-emerald-500/40 hover:bg-emerald-600/45 transition disabled:opacity-50"
        >
          {loading ? "Scanning…" : "Refresh Scan"}
        </button>
      </div>

      {error && (
        <div className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 font-mono text-[11px] text-amber-200">
          {error}
          {String(error).toLowerCase().includes("premarket") ||
          String(error).toLowerCase().includes("empty") ? (
            <span className="block mt-1 text-mist/60">
              Run the 08:55 premarket job (GitHub Action or POST /surprise/premarket) first.
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
