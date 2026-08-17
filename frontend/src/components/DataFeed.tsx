import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import Pipeline from "./Pipeline";

type Job = {
  status?: string;
  processed?: number;
  total?: number;
  elapsed_sec?: number;
  estimated_remaining_sec?: number | null;
  message?: string;
};

type Meta = {
  last_success_at?: string | null;
  last_count?: number;
  last_message?: string;
  source?: string;
  universe_size?: number;
};

function fmtSec(s?: number | null) {
  if (s == null || Number.isNaN(s)) return "—";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}m ${r}s`;
}

export default function DataFeed() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [running, setRunning] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const st = await api.getDataFeedStatus();
      setJob(st);
      setMeta(st.meta || null);
      if (st.status === "running") setRunning(true);
      if (st.status === "done" || st.status === "idle" || st.status === "error") {
        if (st.status === "done" && st.message) setBanner(st.message);
        setRunning(st.status === "running");
      }
    } catch (e: any) {
      setErr(e?.message || "Failed to load data feed status");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!running) return;
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [running, refresh]);

  const startFeed = async () => {
    setErr(null);
    setBanner(null);
    try {
      setRunning(true);
      const res = await api.runDataFeed(true);
      setBanner(res.message || "Data feed started");
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "Failed to start data feed");
      setRunning(false);
    }
  };

  const pct =
    job?.total && job.total > 0
      ? Math.min(100, Math.round(((job.processed || 0) / job.total) * 100))
      : running
      ? 5
      : 0;

  return (
    <div className="space-y-4">
      <div className="rounded-2xl border border-slate/50 bg-graphite/80 p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-display text-xl text-paper tracking-wide">Data Feed</h2>
            <p className="text-mist/70 text-xs mt-1 max-w-xl">
              Stores slow-changing fields (fundamentals, sector, peers, multi-quarter, bulk/insider
              snapshot) for scan-universe stocks. Real-time tasks reuse this cache for 12–24h to stay
              inside free-tier rate limits. Price & intraday technicals stay live.
            </p>
          </div>
          <button
            type="button"
            onClick={startFeed}
            disabled={running}
            className="font-mono text-xs px-4 py-2 rounded-lg bg-amber-500/20 border border-amber-500/40 text-amber-200 hover:bg-amber-500/30 disabled:opacity-50"
          >
            {running ? "Feeding…" : "Data feed to All Scan Universe Stocks"}
          </button>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-5">
          <div className="rounded-xl border border-slate/40 bg-ink/40 p-3">
            <div className="font-mono text-[10px] text-mist uppercase tracking-wider">Stocks in feed</div>
            <div className="font-mono text-lg text-paper mt-1">{meta?.last_count ?? "—"}</div>
          </div>
          <div className="rounded-xl border border-slate/40 bg-ink/40 p-3">
            <div className="font-mono text-[10px] text-mist uppercase tracking-wider">Last success</div>
            <div className="font-mono text-[11px] text-paper mt-1 break-all">
              {meta?.last_success_at
                ? new Date(meta.last_success_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })
                : "Never"}
            </div>
          </div>
          <div className="rounded-xl border border-slate/40 bg-ink/40 p-3">
            <div className="font-mono text-[10px] text-mist uppercase tracking-wider">Job</div>
            <div className="font-mono text-sm text-paper mt-1 capitalize">{job?.status || "idle"}</div>
          </div>
          <div className="rounded-xl border border-slate/40 bg-ink/40 p-3">
            <div className="font-mono text-[10px] text-mist uppercase tracking-wider">Progress</div>
            <div className="font-mono text-sm text-paper mt-1">
              {job?.processed ?? 0}/{job?.total ?? 0} ({pct}%)
            </div>
          </div>
        </div>

        {(running || (job?.status === "running")) && (
          <div className="mt-5 space-y-3">
            <div className="h-2 rounded-full bg-slate/40 overflow-hidden">
              <div
                className="h-full bg-amber-500/80 transition-all duration-500"
                style={{ width: `${pct}%` }}
              />
            </div>
            <div className="flex flex-wrap gap-4 font-mono text-[11px] text-mist">
              <span>Elapsed {fmtSec(job?.elapsed_sec)}</span>
              <span>Remaining ~{fmtSec(job?.estimated_remaining_sec)}</span>
              <span className="text-mist/80">{job?.message}</span>
            </div>
            <Pipeline running={true} />
          </div>
        )}

        {banner && (
          <div className="mt-4 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 font-mono text-xs text-emerald-200">
            ✅ {banner}
          </div>
        )}
        {err && (
          <div className="mt-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 font-mono text-xs text-rose-200">
            {err}
          </div>
        )}
        {meta?.last_message && !banner && (
          <p className="mt-3 font-mono text-[11px] text-mist/60">{meta.last_message}</p>
        )}
      </div>
    </div>
  );
}
