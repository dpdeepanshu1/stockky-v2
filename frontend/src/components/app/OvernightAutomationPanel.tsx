import { useEffect, useState } from "react";
import { setVisibleInterval } from "../../visibleInterval";
import { apiUrl } from "../../api";

// 2026-09-11 fix — Oracle overnight automation: toggle + manual "run now"
// buttons for the two overnight phases (see api-gateway's /ops/overnight/*
// proxy routes → notification-scheduler-service's overnight_orchestrator.py).
// Replaces the Render-era GitHub Actions premarket/midnight cron workflows
// now that the whole stack runs 24/7 on Oracle instead of a sleeping
// free-tier dyno that needed an external wake-up call.
export function OvernightAutomationPanel() {
  const [config, setConfig] = useState<{
    enabled: boolean; datafeed_time: string; premarket_time: string; rest_between_steps_sec: number;
  } | null>(null);
  const [status, setStatus] = useState<{ status: string; phase: string | null; message: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState<"datafeed" | "premarket" | null>(null);

  async function refresh() {
    try {
      const [cfgRes, stRes] = await Promise.all([
        fetch(apiUrl("/ops/overnight/config")).then((r) => r.json()),
        fetch(apiUrl("/ops/overnight/status")).then((r) => r.json()),
      ]);
      if (cfgRes && cfgRes.enabled !== undefined) setConfig(cfgRes);
      if (stRes) setStatus(stRes);
    } catch {
      // Non-fatal — panel just shows stale/last-known state until the next poll.
    }
  }

  useEffect(() => {
    refresh();
    return setVisibleInterval(refresh, 20000);   // group 184: paused while the browser tab is hidden
  }, []);

  async function toggleEnabled() {
    if (!config || saving) return;
    setSaving(true);
    try {
      const r = await fetch(apiUrl(`/ops/overnight/config?enabled=${!config.enabled}`), { method: "POST" });
      const data = await r.json();
      if (data?.config) setConfig(data.config);
    } catch {
      // Non-fatal — next refresh() poll will resync the toggle's real state.
    }
    setSaving(false);
  }

  async function runNow(phase: "datafeed" | "premarket") {
    if (running) return;
    setRunning(phase);
    try {
      await fetch(apiUrl(`/ops/overnight/run?phase=${phase}`), { method: "POST" });
      await new Promise((r) => setTimeout(r, 1200));
      await refresh();
    } catch {
      // Non-fatal — status panel will show whatever state actually landed.
    }
    setRunning(null);
  }

  const isRunning = status?.status === "running";

  return (
    <section className="terminal-panel">
      <h3 className="mono text-xs text-mist uppercase tracking-widest mb-2">Overnight Automation (Oracle)</h3>
      <p className="text-mist/70 text-xs mb-3 max-w-lg">
        Runs entirely on this server overnight — no external cron needed. Phase 1 (Data Feed +
        repair-all) runs after 12 AM IST; Phase 2 (Hot Picks / Surprise / IPO premarket feed +
        repair) runs before market open. Each step rests between calls to avoid rate limits.
      </p>

      <div className="flex items-center gap-3 mb-3">
        <button
          type="button"
          className="btn-terminal text-xs"
          onClick={toggleEnabled}
          disabled={!config || saving}
        >
          {config?.enabled ? "✅ Enabled — click to disable" : "⛔ Disabled — click to enable"}
        </button>
        {config && (
          <span className="mono text-[11px] text-mist/60">
            {config.datafeed_time} IST · {config.premarket_time} IST · {config.rest_between_steps_sec}s rest
          </span>
        )}
      </div>

      <div className="flex flex-wrap gap-2 mb-2">
        <button
          type="button"
          className="btn-terminal text-xs"
          onClick={() => runNow("datafeed")}
          disabled={!!running || isRunning}
        >
          {running === "datafeed" ? "Starting…" : "▶ Run Data Feed + Repair Now"}
        </button>
        <button
          type="button"
          className="btn-terminal text-xs"
          onClick={() => runNow("premarket")}
          disabled={!!running || isRunning}
        >
          {running === "premarket" ? "Starting…" : "▶ Run Premarket Feed + Repair Now"}
        </button>
      </div>

      {status && (
        <p className={`mono text-xs mt-2 mb-0 ${status.status === "error" ? "text-signal-sell" : status.status === "running" ? "text-signal-hold" : "text-mist/70"}`}>
          {status.phase ? `[${status.phase}] ` : ""}{status.status}{status.message ? ` — ${status.message}` : ""}
        </p>
      )}
    </section>
  );
}
