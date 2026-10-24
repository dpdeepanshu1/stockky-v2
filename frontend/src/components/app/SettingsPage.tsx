import { useState } from "react";
import { api, getApiUrl, setApiUrl } from "../../api";
import RateLimitDashboard from "../RateLimitDashboard";
import { PageHeader } from "../ui/PageHeader";
import { OvernightAutomationPanel } from "./OvernightAutomationPanel";

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
      const { stopSessionKeepAlive } = await import("../../api");
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

export function SettingsPage({
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
  const [dbWakeState, setDbWakeState] = useState<"idle" | "waking" | "ok" | "error">("idle");
  const [dbWakeDetail, setDbWakeDetail] = useState<string | null>(null);

  async function wakeDbNow() {
    setDbWakeState("waking");
    setDbWakeDetail(null);
    try {
      const res = await api.wakeAllDatabases();
      setDbWakeState(res?.ok ? "ok" : "error");
      const bits = Object.entries(res?.targets || {}).map(
        ([k, v]) => `${k}: ${v?.ok ? "up" : "failed"}`
      );
      setDbWakeDetail(bits.join(" · ") || null);
    } catch (e: any) {
      setDbWakeState("error");
      setDbWakeDetail(e?.message || "Wake failed");
    }
  }

  function save() {
    setApiUrl(url);
    onSaved();
    setSavedMsg("API URL saved on this device.");
    setTimeout(() => setSavedMsg(null), 3000);
  }

  return (
    <div className="page-terminal settings-page space-y-4">
      <PageHeader title="Settings" desc="Backend address, appearance and system tools." />
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
        <h3 className="mono text-xs text-mist uppercase tracking-widest mb-2">Databases</h3>
        <p className="text-mist/70 text-xs mb-3">
          Wakes Neon (gateway) + training service DB + cache DB together, so the first query of
          the session doesn't pay Neon's cold-compute latency. Runs automatically on page load —
          use this to force it again anytime.
        </p>
        <button
          type="button"
          className="btn-terminal"
          onClick={() => void wakeDbNow()}
          disabled={dbWakeState === "waking"}
        >
          {dbWakeState === "waking" ? "Waking DB…" : "Wake DB"}
        </button>
        {dbWakeDetail && (
          <p
            className={`mono text-xs mt-2 mb-0 ${
              dbWakeState === "ok" ? "text-signal-buy" : "text-signal-sell"
            }`}
          >
            {dbWakeDetail}
          </p>
        )}
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

      <OvernightAutomationPanel />

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
