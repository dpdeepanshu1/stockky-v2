import { useState } from "react";
import { api, getApiUrl, setApiUrl, normalizeGatewayUrl, stopSessionKeepAlive } from "../../api";
import RateLimitDashboard from "../RateLimitDashboard";
import { PageHeader } from "../ui/PageHeader";
import { KpiStrip } from "../ui/KpiStrip";
import { settingsKpis, checkApiUrl, powerOffReport } from "../settingsView";
import { OvernightAutomationPanel } from "./OvernightAutomationPanel";
import { Button, Card, CardHeader, Field, StatusPill } from "../kit";   // E542

async function powerOffAll() {
  // E168: one tap used to stop everything with no way back; ask first
  if (!window.confirm(
    "Switch OFF all processes?\n\n" +
      "This force-stops scan, data feed, hot picks and training, then reloads the page.\n" +
      "Only health checks will remain."
  )) return;
  try {
    // E168: go through the api wrappers so the saved API address (Settings) is used, not only VITE_API_URL
    const [po] = await Promise.allSettled([api.powerOff(), api.stopAllScans(), api.stopDataFeed()]);
    const report = powerOffReport(
      po.status === "fulfilled"
        ? { ok: true, message: po.value?.message, phases: po.value?.phases }
        : { ok: false, error: (po.reason as Error)?.message }
    );
    window.alert(report.text);
    if (!report.ok) return;   // nothing was confirmed stopped: keep the page and the local task state as they are
    // Stop client-side keep-alive and any local intervals now that the backend has gone quiet
    try {
      stopSessionKeepAlive();
    } catch {}
    try {
      sessionStorage.removeItem("stockky_scan_task");
      localStorage.removeItem("stockky_scan_task");
      sessionStorage.removeItem("stockky_ws_quotes");
    } catch {}
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
    const chk = checkApiUrl(normalizeGatewayUrl(url));   // E168: refuse junk before it is stored
    if (!chk.ok) {
      setSavedMsg(chk.message);
      setTimeout(() => setSavedMsg(null), 6000);
      return;
    }
    setApiUrl(url);
    onSaved();
    setSavedMsg("API URL saved on this device.");
    setTimeout(() => setSavedMsg(null), 3000);
  }

  return (
    <div className="page-terminal settings-page space-y-4">
      <PageHeader title="Settings" desc="Backend address, appearance and system tools." icon="sliders" kicker="System" />
      <KpiStrip items={settingsKpis({ backendUp, url: normalizeGatewayUrl(url), savedUrl: getApiUrl(), theme, dbWake: dbWakeState, dbDetail: dbWakeDetail })} label="Settings summary" />
      <Card>
        <CardHeader title="Backend connection" sub="API Gateway public URL. Required when frontend and backend are deployed separately." action={<StatusPill state={backendUp === "up" ? "live" : backendUp === "down" ? "offline" : "info"}>{backendUp === "up" ? "Backend up" : backendUp === "down" ? "Backend down" : "Checking"}</StatusPill>} />
        <div className="st-row">
          <Field
            label="API Gateway URL"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && save()}
            placeholder="https://your-api-gateway.onrender.com"
            spellCheck={false}
            autoComplete="off"
          />
          <Button onClick={save}>Save URL</Button>
        </div>
        {savedMsg && <p role="status" className={`mono text-xs mt-2 mb-0 ${savedMsg.startsWith("API URL saved") ? "text-signal-buy" : "text-signal-sell"}`}>{savedMsg}</p>}
      </Card>

      <Card>
        <CardHeader title="Appearance" sub="Light is the default; the choice is remembered on this device." action={<Button variant="ghost" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>Switch to {theme === "dark" ? "light" : "dark"} mode</Button>} />
      </Card>

      <Card>
        <CardHeader title="Databases" sub="Wakes Neon (gateway) + training service DB + cache DB together, so the first query of the session doesn't pay Neon's cold-compute latency. Runs automatically on page load; use this to force it again anytime." action={<Button variant="soft" onClick={() => void wakeDbNow()} disabled={dbWakeState === "waking"}>{dbWakeState === "waking" ? "Waking DB…" : "Wake DB"}</Button>} />
        {dbWakeDetail && (
          <p className={`mono text-xs mt-2 mb-0 ${dbWakeState === "ok" ? "text-signal-buy" : "text-signal-sell"}`}>
            {dbWakeDetail}
          </p>
        )}
      </Card>

      <Card>
        <CardHeader title="Services" sub="Wake or inspect microservice health." />
        <div className="st-actions">
          <Button variant="danger" onClick={() => powerOffAll()} title="Stop scan, data-feed, hot-picks; reset UI">⏻ Power Off</Button>
          <Button variant="ghost" onClick={onOpenServices}>Open Service Manager</Button>
        </div>
      </Card>

      <Card>
        <RateLimitDashboard />
      </Card>

      <OvernightAutomationPanel />

      <Card>
        <CardHeader title="Deploy checklist" />
        <ul className="st-list">
          <li>Set VITE_API_URL at frontend build time to your API Gateway</li>
          <li>Set DATABASE_URL on decision-prediction-service (Neon/Supabase Postgres)</li>
          <li>Set UPSTASH_REDIS_REST_URL + TOKEN on all services</li>
          <li>Replace placeholder *.onrender.com URLs with your real service URLs</li>
          <li>Optional: CALLMEBOT_USER / CALLMEBOT_USERS for voice alerts</li>
        </ul>
      </Card>
    </div>
  );
}
