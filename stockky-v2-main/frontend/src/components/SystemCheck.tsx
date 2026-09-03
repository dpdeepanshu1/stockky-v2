import { useEffect, useRef, useState } from "react";
import { api, getApiUrl, setApiUrl, SystemHealth, wakeService } from "../api";

type Stage =
  | { phase: "checking-gateway" }
  | { phase: "gateway-down" }
  | { phase: "waking"; health: SystemHealth | null; attempt: number }
  | { phase: "ready" };

const SERVICE_LABELS: Record<string, string> = {
  "api-gateway": "API Gateway",
  "market-data": "Market Data",
  "technical-analysis": "Technical Analysis",
  "fundamental-analysis": "Fundamental Analysis",
  "decision-engine": "Decision Engine",
  "news-intelligence": "News Intelligence",
  "event-tracker": "Event Tracker",
  prediction: "Prediction Model",
  notification: "Notifications",
};

const MAX_AUTO_ATTEMPTS = 6;
const ESCAPE_HATCH_AFTER_ATTEMPTS = 3;

export default function SystemCheck({ onReady }: { onReady: () => void }) {
  const [stage, setStage] = useState<Stage>({ phase: "checking-gateway" });
  const [apiUrlInput, setApiUrlInput] = useState(getApiUrl());
  const cancelled = useRef(false);
  const currentAttempt = stage.phase === "waking" ? stage.attempt : 0;

  const [wakingServices, setWakingServices] = useState<Record<string, boolean>>({});
  const [wakeMessages, setWakeMessages] = useState<Record<string, string>>({});
  const [isWakingAll, setIsWakingAll] = useState(false);
  const [isRechecking, setIsRechecking] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  useEffect(() => {
    cancelled.current = false;
    runCheck(0);
    return () => {
      cancelled.current = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function runCheck(attempt: number) {
    try {
      await api.ping();
    } catch {
      if (!cancelled.current) setStage({ phase: "gateway-down" });
      return;
    }

    try {
      const health = await api.systemHealth();
      if (cancelled.current) return;

      if (health.required_ok) {
        setStage({ phase: "ready" });
        setStatusMessage("✅ All services connected successfully!");
        setTimeout(() => {
          if (!cancelled.current) onReady();
        }, 700);
        return;
      }

      setStage({ phase: "waking", health, attempt });
      if (attempt < MAX_AUTO_ATTEMPTS) {
        setTimeout(() => runCheck(attempt + 1), 5000);
      }
    } catch (e) {
      if (!cancelled.current) {
        setStage({
          phase: "waking",
          health: null,
          attempt,
        });
        if (attempt < MAX_AUTO_ATTEMPTS) {
          setTimeout(() => runCheck(attempt + 1), 5000);
        }
      }
    }
  }

  async function handleWakeService(url: string | null | undefined, serviceName: string) {
    if (!url) return;
    setWakingServices((prev) => ({ ...prev, [serviceName]: true }));
    setWakeMessages((prev) => ({ ...prev, [serviceName]: "⏳ Waking..." }));

    try {
      await wakeService(url);
      setWakeMessages((prev) => ({ ...prev, [serviceName]: "✅ Woke! Rechecking..." }));
      await new Promise((resolve) => setTimeout(resolve, 3000));
      await runCheck(currentAttempt);
      setWakeMessages((prev) => ({ ...prev, [serviceName]: "🟢 Online" }));
      setStatusMessage(`✅ ${serviceName} woke successfully!`);
    } catch {
      setWakeMessages((prev) => ({ ...prev, [serviceName]: "❌ Wake failed" }));
      setStatusMessage(`❌ Failed to wake ${serviceName}`);
    } finally {
      setWakingServices((prev) => ({ ...prev, [serviceName]: false }));
      setTimeout(() => {
        setWakeMessages((prev) => {
          const newMsg = { ...prev };
          delete newMsg[serviceName];
          return newMsg;
        });
      }, 5000);
      setTimeout(() => setStatusMessage(null), 5000);
    }
  }

  async function wakeAllServices() {
    if (isWakingAll) return;
    setIsWakingAll(true);
    setStatusMessage("⏳ Waking all services...");

    const services = stage.phase === "waking" ? stage.health?.services : {};
    if (!services) {
      setStatusMessage("❌ No services to wake");
      setIsWakingAll(false);
      return;
    }

    let successCount = 0;
    let failCount = 0;
    for (const [name, s] of Object.entries(services)) {
      if (s.url && !s.ok) {
        try {
          await wakeService(s.url);
          successCount++;
          setStatusMessage(`⏳ Woke ${name} (${successCount}/${Object.values(services).filter(s => s.url && !s.ok).length})`);
          await new Promise((resolve) => setTimeout(resolve, 1000));
        } catch {
          failCount++;
        }
      }
    }

    setStatusMessage(`✅ Wake complete: ${successCount} services woke, ${failCount} failed`);
    await runCheck(currentAttempt);
    setIsWakingAll(false);
    setTimeout(() => setStatusMessage(null), 5000);
  }

  async function handleRecheck() {
    if (isRechecking) return;
    setIsRechecking(true);
    setStatusMessage("⏳ Rechecking services...");
    await runCheck(currentAttempt);
    setIsRechecking(false);
    setStatusMessage("✅ Recheck complete");
    setTimeout(() => setStatusMessage(null), 3000);
  }

  function saveGatewayUrl() {
    setApiUrl(apiUrlInput);
    setStage({ phase: "checking-gateway" });
    runCheck(0);
  }

  if (stage.phase === "gateway-down") {
    return (
      <GateShell>
        <p className="font-mono text-xs text-signal-sell uppercase tracking-widest mb-2">
          Can't reach the backend
        </p>
        <p className="text-mist text-sm mb-4 max-w-sm">
          Set your API Gateway URL to continue.
        </p>
        <div className="flex gap-2 max-w-md">
          <input
            value={apiUrlInput}
            onChange={(e) => setApiUrlInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && saveGatewayUrl()}
            placeholder="https://your-api-gateway.onrender.com"
            className="flex-1 bg-ink/60 border border-slate rounded-lg px-3 py-2 font-mono text-xs text-paper placeholder:text-mist/30 outline-none focus:border-signal-prepare/60 transition"
            spellCheck={false}
            autoComplete="off"
          />
          <button
            onClick={saveGatewayUrl}
            className="border border-slate rounded-lg px-4 py-2 font-mono text-xs text-mist hover:text-paper hover:border-signal-prepare/60 transition shrink-0"
          >
            Connect
          </button>
        </div>
      </GateShell>
    );
  }

  if (stage.phase === "checking-gateway") {
    return (
      <GateShell>
        <p className="font-mono text-xs text-mist uppercase tracking-widest">
          Connecting to backend...
        </p>
      </GateShell>
    );
  }

  if (stage.phase === "ready") {
    return (
      <GateShell>
        <p className="font-mono text-sm text-signal-buy uppercase tracking-widest">
          All Services Connected Successfully
        </p>
      </GateShell>
    );
  }

  // phase === "waking"
  const services = stage.health?.services || {};
  const entries = Object.entries(services);
  const showEscapeHatch = currentAttempt >= ESCAPE_HATCH_AFTER_ATTEMPTS;

  return (
    <GateShell>
      <p className="font-mono text-xs text-mist uppercase tracking-widest mb-1">
        Waking up services
      </p>
      <p className="text-mist/60 text-xs mb-6 max-w-sm">
        Everything runs on free-tier hosting, so a sleeping service can take up to a minute to
        wake on its first request. Use the buttons below to wake individual services.
      </p>

      {statusMessage && (
        <div className="mb-4 font-mono text-xs text-signal-prepare animate-pulse">
          {statusMessage}
        </div>
      )}

      <div className="space-y-1.5 mb-6 w-full max-w-sm">
        {entries.length === 0 && (
          <p className="font-mono text-[11px] text-mist/40">Checking...</p>
        )}
        {entries.map(([name, s]) => (
          <ServiceRow
            key={name}
            name={SERVICE_LABELS[name] || name}
            status={s}
            onWake={() => handleWakeService(s.url, name)}
            isWaking={!!wakingServices[name]}
            message={wakeMessages[name]}
          />
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-4">
        <button
          onClick={wakeAllServices}
          disabled={isWakingAll}
          className="font-mono text-xs text-paper bg-signal-prepare/20 border border-signal-prepare/40 rounded-lg px-4 py-2 hover:bg-signal-prepare/30 transition disabled:opacity-50"
        >
          {isWakingAll ? "⏳ Waking..." : "Wake All Services"}
        </button>
        <button
          onClick={handleRecheck}
          disabled={isRechecking}
          className="font-mono text-xs text-mist hover:text-paper border border-slate rounded-lg px-3 py-2 hover:border-mist/60 transition disabled:opacity-50"
        >
          {isRechecking ? "⏳ Rechecking..." : "Recheck now"}
        </button>
        {showEscapeHatch && (
          <button
            onClick={onReady}
            className="font-mono text-xs text-mist/50 hover:text-paper underline"
          >
            Continue anyway
          </button>
        )}
      </div>
    </GateShell>
  );
}

function ServiceRow({
  name,
  status,
  onWake,
  isWaking,
  message,
}: {
  name: string;
  status: { ok: boolean; required: boolean; status: string; url?: string | null };
  onWake: () => void;
  isWaking: boolean;
  message?: string;
}) {
  let statusText = status.status;
  let color = "text-mist/30";
  let icon = "●";

  if (status.ok) {
    color = "text-signal-buy";
    icon = "✅";
  } else if (status.status === "not_configured") {
    color = "text-mist/30";
    icon = "—";
  } else if (status.status === "unreachable" || status.status.startsWith("http_")) {
    color = "text-signal-sell";
    icon = "❌";
  } else {
    color = "text-signal-hold animate-pulse";
    icon = "⏳";
  }

  return (
    <div className="flex items-center justify-between font-mono text-[11px] border-b border-slate/30 py-1.5">
      <span className={status.required ? "text-mist" : "text-mist/50"}>
        {name}
        {!status.required && <span className="text-mist/30"> (optional)</span>}
      </span>
      <div className="flex items-center gap-2">
        <span className={color}>{icon} {statusText}</span>
        {status.url && !status.ok && (
          <button
            onClick={onWake}
            disabled={isWaking}
            className="text-xs px-2 py-0.5 border border-slate/60 rounded hover:border-mist/60 hover:text-paper transition disabled:opacity-50"
          >
            {isWaking ? "⏳" : "Wake"}
          </button>
        )}
        {message && (
          <span className="text-[10px] text-mist/60">{message}</span>
        )}
      </div>
    </div>
  );
}

function GateShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-ink text-paper flex flex-col items-center justify-center px-4">
      <span className="font-display text-xl tracking-tight mb-8">Stockky</span>
      <div className="flex flex-col items-center text-center">{children}</div>
    </div>
  );
}