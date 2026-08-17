import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import Pipeline from "./Pipeline";

type Job = {
  status?: string;
  processed?: number;
  total?: number;
  elapsed_sec?: number;
  estimated_remaining_sec?: number | null;
  message?: string;
  ok_count?: number;
  error_count?: number;
  checkpoint?: { cursor?: number; done?: string[] };
};

type Meta = {
  last_success_at?: string | null;
  last_count?: number;
  last_message?: string;
  source?: string;
  universe_size?: number;
  partial?: boolean;
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
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const st = await api.getDataFeedStatus();
      setJob(st);
      setMeta(st.meta || null);
      const isRun = st.status === "running";
      setRunning(isRun);
      if (st.status === "done" && st.message) setBanner(st.message);
      if (st.status === "stopped" && st.message) setBanner(st.message);
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

  const total = job?.total ?? 0;
  const processed = job?.processed ?? 0;
  const complete = total > 0 && processed >= total && job?.status === "done";
  const partial =
    (job?.status === "stopped" || job?.status === "error" || meta?.partial) &&
    processed > 0 &&
    total > 0 &&
    processed < total;
  const canResume =
    !running &&
    (partial ||
      (job?.status === "stopped" && processed < total) ||
      (job?.checkpoint?.cursor != null &&
        job.checkpoint.cursor > 0 &&
        job.checkpoint.cursor < (job.total || 0)));
  // Refresh active when not fully fed (or never run). Disabled only when fully complete.
  const canRefresh = !running && !complete;
  // When complete, still allow refresh to rebuild — user asked disable only when all fed
  // "Refresh Button should active in that case otherwise if we have all data feed to all stocks so it should be disble"
  // → Refresh enabled when NOT all stocks fed; disabled when complete.
  // But they also said "again active when we start" — so when starting a new cycle after complete, enable via explicit full refresh.
  // Interpretation: Refresh disabled only when status=done AND processed>=total. User can still force via... 
  // Actually for complete universe they want refresh disabled. Provide "Full refresh" that becomes active when complete? 
  // I'll: Refresh enabled when !running && !complete; when complete show "Full re-feed" as force refresh enabled.
  const canFullRefeed = !running && complete;

  const pct = useMemo(() => {
    if (total > 0) return Math.min(100, Math.round((processed / total) * 100));
    return running ? 5 : 0;
  }, [processed, total, running]);

  const startFresh = async () => {
    setErr(null);
    setBanner(null);
    setBusy("start");
    try {
      setRunning(true);
      const res = await api.runDataFeed(true); // force full
      setBanner(res.message || "Data feed started");
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "Failed to start data feed");
      setRunning(false);
    } finally {
      setBusy(null);
    }
  };

  const onResume = async () => {
    setErr(null);
    setBanner(null);
    setBusy("resume");
    try {
      setRunning(true);
      const res = await api.resumeDataFeed();
      setBanner(res.message || "Resumed from checkpoint");
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "Failed to resume data feed");
      setRunning(false);
    } finally {
      setBusy(null);
    }
  };

  const onStop = async () => {
    setErr(null);
    setBusy("stop");
    try {
      const res = await api.stopDataFeed();
      setBanner(res.message || "Stop requested — committing progress…");
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "Failed to stop data feed");
    } finally {
      setBusy(null);
    }
  };

  const onRefreshPage = async () => {
    // Soft UI refresh of status + if partial, does NOT wipe checkpoint
    setErr(null);
    setBusy("refresh-ui");
    try {
      await refresh();
      setBanner("Status refreshed");
    } catch (e: any) {
      setErr(e?.message || "Refresh failed");
    } finally {
      setBusy(null);
    }
  };

  const onFullRefresh = async () => {
    // Full re-feed from 0 — only when not complete, or explicit full refeed when complete
    await startFresh();
  };

  return (
    <div className="space-y-4">
      <div className="rounded-2xl border border-slate/50 bg-graphite/80 p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="font-display text-xl text-paper tracking-wide">Data Feed</h2>
            <p className="text-mist/70 text-xs mt-1 max-w-xl">
              Stores slow-changing fields (fundamentals, sector, peers, multi-quarter, bulk/insider
              snapshot) for scan-universe stocks. Real-time tasks reuse this cache for 12–24h to stay
              inside free-tier rate limits. Price &amp; intraday technicals stay live.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onResume}
              disabled={!canResume || busy != null}
              title="Continue from last committed checkpoint"
              className="font-mono text-xs px-3 py-2 rounded-lg bg-sky-500/20 border border-sky-500/40 text-sky-200 hover:bg-sky-500/30 disabled:opacity-40"
            >
              {busy === "resume" ? "Resuming…" : "Resume"}
            </button>
            <button
              type="button"
              onClick={onStop}
              disabled={!running || busy != null}
              title="Stop and commit what has been fed so far"
              className="font-mono text-xs px-3 py-2 rounded-lg bg-rose-500/20 border border-rose-500/40 text-rose-200 hover:bg-rose-500/30 disabled:opacity-40"
            >
              {busy === "stop" ? "Stopping…" : "Stop"}
            </button>
            <button
              type="button"
              onClick={onRefreshPage}
              disabled={busy != null}
              title="Refresh status from server (keeps checkpoint)"
              className="font-mono text-xs px-3 py-2 rounded-lg bg-slate-500/20 border border-slate-400/40 text-paper hover:bg-slate-500/30 disabled:opacity-40"
            >
              {busy === "refresh-ui" ? "…" : "Refresh status"}
            </button>
            <button
              type="button"
              onClick={onFullRefresh}
              disabled={running || (!canRefresh && !canFullRefeed) || busy != null}
              title={
                complete
                  ? "All stocks already fed — full re-feed from start"
                  : "Start / re-run feed for remaining or all symbols"
              }
              className="font-mono text-xs px-4 py-2 rounded-lg bg-amber-500/20 border border-amber-500/40 text-amber-200 hover:bg-amber-500/30 disabled:opacity-40"
            >
              {running
                ? "Feeding…"
                : complete
                ? "Full re-feed"
                : "Data feed to All Scan Universe Stocks"}
            </button>
          </div>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-5">
          <div className="rounded-xl border border-slate/40 bg-ink/40 p-3">
            <div className="font-mono text-[10px] text-mist uppercase tracking-wider">Stocks in feed</div>
            <div className="font-mono text-lg text-paper mt-1">
              {meta?.last_count ?? job?.ok_count ?? "—"}
            </div>
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
              {processed}/{total} ({pct}%)
            </div>
          </div>
        </div>

        {(running || job?.status === "running" || job?.status === "stopped") && (
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
            {running && <Pipeline running={true} />}
          </div>
        )}

        {canResume && (
          <p className="mt-3 font-mono text-[11px] text-sky-300/80">
            Checkpoint at {processed}/{total}. Press <b>Resume</b> after a sleep/timeout to continue
            without re-feeding completed stocks.
          </p>
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
