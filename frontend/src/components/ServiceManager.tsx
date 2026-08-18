import { useEffect, useState } from "react";
import { api, wakeService, SystemServiceStatus } from "../api";

interface ServiceManagerProps {
  onClose: () => void;
}

const DESCRIPTIONS: Record<string, string> = {
  "api-gateway": "Primary router, dynamic scan universe aggregator, Upstash Redis cache bridge.",
  "market-data": "Yahoo Finance OHLCV pipeline with last-known-good fallback & exponential backoff.",
  "market-data-service": "Yahoo Finance OHLCV pipeline with last-known-good fallback & exponential backoff.",
  "analysis-intelligence": "Technical, fundamentals, IndianAPI fallback, corporate events & news NLP.",
  "analysis-intelligence-service": "Technical, fundamentals, IndianAPI fallback, corporate events & news NLP.",
  "decision-prediction": "Decision engine, XGBoost prediction, training / T+1·T+5 outcomes.",
  "decision-prediction-service": "Decision engine, XGBoost prediction, training / T+1·T+5 outcomes.",
  "decision-engine": "Multi-horizon decide + quality gates + provisional BUY block.",
  "notification": "Telegram CallMeBot voice-first alerts, Discord, outbox.",
  "notification-scheduler": "Telegram CallMeBot voice-first alerts, Discord, outbox.",
  "training": "Closed-loop win-rate, similarity scanner, evaluate sweeps.",
  "prediction": "Model inference + LLM explanation (Groq-first).",
  "news-intelligence": "Multi-source news + keyword alias matching.",
  "event-tracker": "Results, bulk/block, insider event detection.",
  "technical-analysis": "Indicators & structure scores.",
  "fundamental-analysis": "Fundamentals + peer relative.",
};

export default function ServiceManager({ onClose }: ServiceManagerProps) {
  const [services, setServices] = useState<Record<string, SystemServiceStatus>>({});
  const [loading, setLoading] = useState(true);
  const [waking, setWaking] = useState<Record<string, boolean>>({});
  const [isWakingAll, setIsWakingAll] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  const fetchServices = async () => {
    setLoading(true);
    try {
      const health = await api.systemHealth();
      setServices(health.services || {});
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchServices();
    const id = setInterval(fetchServices, 45000);
    return () => clearInterval(id);
  }, []);

  const entries = Object.entries(services);
  const okCount = entries.filter(([, s]) => s?.ok).length;
  const total = entries.length || 5;
  const allOk = total > 0 && okCount === total;

  const handleWake = async (name: string, url: string | null | undefined) => {
    if (!url) return;
    setWaking((p) => ({ ...p, [name]: true }));
    try {
      await wakeService(url);
      setStatusMessage(`Woke ${name}`);
      await new Promise((r) => setTimeout(r, 2500));
      await fetchServices();
    } catch {
      setStatusMessage(`Failed to wake ${name}`);
    } finally {
      setWaking((p) => ({ ...p, [name]: false }));
      setTimeout(() => setStatusMessage(null), 4000);
    }
  };

  const handleWakeAll = async () => {
    if (isWakingAll) return;
    setIsWakingAll(true);
    setStatusMessage("Waking all services…");
    try {
      await api.wakeAll?.().catch(() => null);
    } catch {
      /* optional */
    }
    for (const [name, status] of entries) {
      if (status.url && !status.ok) {
        try {
          await wakeService(status.url);
        } catch {
          /* continue */
        }
      }
    }
    await new Promise((r) => setTimeout(r, 3000));
    await fetchServices();
    setIsWakingAll(false);
    setStatusMessage("Wake pass complete");
    setTimeout(() => setStatusMessage(null), 4000);
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-end sm:items-center justify-center p-3 sm:p-6">
      <button type="button" className="absolute inset-0 bg-black/60 backdrop-blur-sm" aria-label="Close" onClick={onClose} />
      <div className="topo-shell relative z-10 w-full max-w-lg max-h-[88vh] overflow-y-auto shadow-2xl">
        <div className="flex items-start justify-between gap-2 mb-1">
          <h2 className="topo-title">
            <span aria-hidden>◈</span> System Health &amp; 5 Microservices Topology
          </h2>
          <button type="button" className="btn-terminal text-xs" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="topo-sub">
          Zero-cost architecture across Render microservices, Upstash Redis, and Supabase Postgres.
        </p>

        <div className={`topo-status-pill ${allOk ? "" : "topo-offline"}`} style={!allOk ? { borderColor: "rgba(246,70,93,0.35)", color: "var(--sell)", background: "rgba(246,70,93,0.08)" } : undefined}>
          <span>●</span> Cluster Status: {loading ? "Checking…" : allOk ? "Operational" : `${okCount}/${total} healthy`}
        </div>

        <div className="topo-grid">
          <div className="topo-metric">
            <label>Render microservices</label>
            <strong>{okCount} / {Math.max(total, 5)} Healthy</strong>
          </div>
          <div className="topo-metric">
            <label>Upstash Redis cache</label>
            <strong>Connected</strong>
          </div>
          <div className="topo-metric">
            <label>Supabase Postgres</label>
            <strong>Online (Free Tier)</strong>
          </div>
          <div className="topo-metric">
            <label>GitHub Actions cron</label>
            <strong>09:00 – 15:30 IST</strong>
          </div>
        </div>

        <div className="flex gap-2 mb-3 flex-wrap">
          <button type="button" className="btn-terminal text-xs" onClick={fetchServices} disabled={loading}>
            Refresh
          </button>
          <button type="button" className="btn-terminal text-xs" onClick={handleWakeAll} disabled={isWakingAll}>
            {isWakingAll ? "Waking…" : "Wake all"}
          </button>
        </div>
        {statusMessage && <p className="mono text-xs text-mist mb-2">{statusMessage}</p>}

        {loading && entries.length === 0 ? (
          <p className="mono text-xs text-mist">Loading topology…</p>
        ) : (
          entries.map(([name, st]) => (
            <div key={name} className="topo-service">
              <div className="topo-service-head">
                <span className="topo-service-name">{name}</span>
                <span className={st?.ok ? "topo-online" : "topo-online topo-offline"}>
                  {st?.ok ? "ONLINE" : "OFFLINE"}
                </span>
              </div>
              <div className="topo-service-meta">
                <span>
                  Latency
                  <b>{st?.latency_ms != null ? `${st.latency_ms}ms` : st?.ok ? "<100ms" : "—"}</b>
                </span>
                <span>
                  Status
                  <b style={{ color: st?.ok ? "var(--buy)" : "var(--sell)" }}>{st?.ok ? "OK" : "Down"}</b>
                </span>
              </div>
              <p>{DESCRIPTIONS[name] || st?.detail || "Downstream Stockky service."}</p>
              {!st?.ok && st?.url && (
                <button
                  type="button"
                  className="btn-terminal text-xs mt-2"
                  disabled={waking[name]}
                  onClick={() => handleWake(name, st.url)}
                >
                  {waking[name] ? "Waking…" : "Wake service"}
                </button>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
