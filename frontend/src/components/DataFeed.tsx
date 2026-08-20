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
  stop_requested?: boolean;
  updated_at?: string;
  started_at?: string;
  checkpoint?: { cursor?: number; done?: string[]; universe?: string[] };
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
      const stStatus = String(st.status || "idle");
      // Active only while worker is running/stopping — never when done/idle
      const active = stStatus === "running" || stStatus === "stopping";
      setRunning(active);
      if (st.message) setBanner(String(st.message));
      setErr(null);
      return st;
    } catch (e: any) {
      setErr(e?.message || "Failed to load data feed status");
      return null;
    }
  }, []);

  // Load once on mount — no recursive refresh
  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll only while backend job is running/stopping (stops immediately when done)
  useEffect(() => {
    const st = job?.status || "idle";
    if (st !== "running" && st !== "stopping") {
      if (running) setRunning(false);
      return;
    }
    const id = window.setInterval(() => {
      void refresh();
    }, 2500);
    return () => window.clearInterval(id);
  }, [job?.status, refresh, running]);

  const total = job?.total ?? 0;
  const processed = job?.processed ?? job?.checkpoint?.cursor ?? 0;
  const status: string = job?.status || "idle";
  const complete = total > 0 && processed >= total && status === "done";
  const partial =
    status === "stopped" ||
    status === "error" ||
    !!meta?.partial ||
    (total > 0 && processed > 0 && processed < total);

  // Resume when we have a checkpoint mid-way and are not actively progressing
  const canResume =
    busy == null &&
    !complete &&
    total > 0 &&
    processed > 0 &&
    processed < total &&
    (status === "stopped" ||
      status === "error" ||
      status === "idle" ||
      status === "done" ||
      // stuck running (stop requested or UI thinks worker dead)
      (status === "running" && !!job?.stop_requested) ||
      status === "running");

  // Stop only when truly running (worker alive or claimed running)
  const canStop = busy == null && (status === "running" || status === "stopping");

  // Full feed when not complete and not running
  const isActivelyRunning = status === "running" && !job?.stop_requested;
  const canStart = busy == null && !isActivelyRunning && !complete;
  const canFullRefeed = busy == null && complete && !isActivelyRunning;

  const pct = useMemo(() => {
    if (total > 0) return Math.min(100, Math.round((processed / total) * 100));
    return status === "running" ? 5 : 0;
  }, [processed, total, status]);

  const startFresh = async () => {
    setErr(null);
    setBanner(null);
    setBusy("start");
    try {
      setRunning(true);
      // 1. Instantly nuke corrupted DB state and re-lock unique constraint on k
      try {
        const reset = await api.hardResetDataFeed();
        setBanner(reset?.message || "Database wiped and locked. Starting fresh feed…");
      } catch (resetErr: any) {
        // Soft-fail: still attempt feed so a transient reset error does not block the user
        console.warn("hard-reset failed, continuing to feed:", resetErr);
        setBanner("Hard-reset skipped — starting feed…");
      }
      // 2. Trigger the fresh data feed
      const res = await api.runDataFeed(true);
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
      // If stuck running, stop force-commits first (backend resume also does this)
      if (status === "running") {
        try {
          await api.stopDataFeed();
        } catch {
          /* continue to resume */
        }
      }
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
    setRunning(false); // stop local poll immediately
    try {
      const res = await api.stopDataFeed();
      setBanner((res && res.message) || "Stopped — progress committed");
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "Failed to stop data feed");
    } finally {
      setBusy(null);
    }
  };

  const onRefreshPage = async () => {
    setErr(null);
    setBusy("refresh-ui");
    try {
      await refresh();
      setBanner("Status refreshed (checkpoint kept)");
    } catch (e: any) {
      setErr(e?.message || "Refresh failed");
    } finally {
      setBusy(null);
    }
  };

  /** Only symbols not already in the data-feed cache (new universe members). Manual click only. */
  const startNewOnly = async () => {
    setErr(null);
    setBanner(null);
    setBusy("start-new");
    try {
      setRunning(true);
      const res = await (api as any).dataFeedRunNewOnly();
      setBanner(res?.message || "Feeding newly added stocks only…");
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "Failed to start new-only data feed");
      setRunning(false);
    } finally {
      setBusy(null);
    }
  };

  const stocksInFeed =
    meta?.last_count ?? job?.ok_count ?? job?.checkpoint?.done?.length ?? 0;

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
              disabled={!canResume}
              title="Continue from last committed checkpoint (works after sleep/timeout)"
              className="font-mono text-xs px-3 py-2 rounded-lg bg-sky-500/20 border border-sky-500/40 text-sky-200 hover:bg-sky-500/30 disabled:opacity-40"
            >
              {busy === "resume" ? "Resuming…" : "Resume"}
            </button>
            <button
              type="button"
              onClick={onStop}
              disabled={!canStop}
              title="Stop now and commit checkpoint (force, even if worker died)"
              className="font-mono text-xs px-3 py-2 rounded-lg bg-rose-500/20 border border-rose-500/40 text-rose-200 hover:bg-rose-500/30 disabled:opacity-40"
            >
              {busy === "stop" ? "Stopping…" : "Stop"}
            </button>
            <button
              type="button"
              onClick={onRefreshPage}
              disabled={busy != null}
              title="Refresh status from server — auto-heals stuck Running after sleep"
              className="font-mono text-xs px-3 py-2 rounded-lg bg-slate-500/20 border border-slate-400/40 text-paper hover:bg-slate-500/30 disabled:opacity-40"
            >
              {busy === "refresh-ui" ? "…" : "Refresh status"}
            </button>
            <button
              type="button"
              onClick={startFresh}
              disabled={!(canStart || canFullRefeed)}
              title={
                complete
                  ? "All stocks fed — start a full re-feed from 0"
                  : "Start feed for scan universe (from 0)"
              }
              className="font-mono text-xs px-4 py-2 rounded-lg bg-amber-500/20 border border-amber-500/40 text-amber-200 hover:bg-amber-500/30 disabled:opacity-40"
            >
              {status === "running" && !job?.stop_requested
                ? "Feeding…"
                : complete
                ? "Full re-feed"
                : "Data feed to All Scan Universe Stocks"}
            </button>
            <button
              type="button"
              onClick={startNewOnly}
              disabled={busy != null || isActivelyRunning}
              title="Only feed symbols that are not already in the data-feed store"
              className="font-mono text-xs px-4 py-2 rounded-lg bg-sky-500/15 border border-sky-500/40 text-sky-100 hover:bg-sky-500/25 disabled:opacity-40"
            >
              {busy === "start-new" ? "Feeding new…" : "Feed newly added stocks only"}
            </button>
          </div>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-5">
          <div className="rounded-xl border border-slate/40 bg-ink/40 p-3">
            <div className="font-mono text-[10px] text-mist uppercase tracking-wider">Stocks in feed</div>
            <div className="font-mono text-lg text-paper mt-1">{stocksInFeed || "—"}</div>
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
            <div className="font-mono text-sm text-paper mt-1 capitalize">{status}</div>
          </div>
          <div className="rounded-xl border border-slate/40 bg-ink/40 p-3">
            <div className="font-mono text-[10px] text-mist uppercase tracking-wider">Progress</div>
            <div className="font-mono text-sm text-paper mt-1">
              {processed}/{total} ({pct}%)
            </div>
          </div>
        </div>

        {(status === "running" || status === "stopped" || (total > 0 && processed > 0)) && (
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
            {status === "running" && !job?.stop_requested && <Pipeline running={true} />}
          </div>
        )}

        {partial && status !== "running" && (
          <p className="mt-3 font-mono text-[11px] text-sky-300/80">
            Checkpoint at {processed}/{total}
            {stocksInFeed ? ` · ${stocksInFeed} stocks saved` : ""}. Press <b>Resume</b> to continue
            without re-feeding completed symbols.
          </p>
        )}

        {status === "running" && job?.stop_requested && (
          <p className="mt-3 font-mono text-[11px] text-amber-300/90">
            Stop was requested. Click <b>Refresh status</b> or <b>Stop</b> again to force-commit, then{" "}
            <b>Resume</b>.
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
