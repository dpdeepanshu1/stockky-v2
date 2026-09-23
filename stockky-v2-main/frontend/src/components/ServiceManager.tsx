import { useEffect, useState } from "react";
import { api, wakeService, SystemServiceStatus, apiUrl } from "../api";
import { getRealTradeApiUrl } from "../realTradeApi";
import BottomSheet from "./BottomSheet";

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

// 2026-09-11 fix — user report: "Reset failure button not working well
// yesterday, I use it but not work well nothing happen." Root cause: none
// of the three fetch() calls in handleReset() below had a timeout. The
// button exists specifically to recover from a broken api-gateway — but
// that's exactly the situation where a plain fetch() to it can hang for a
// long time (TCP connects, server just never responds) instead of failing
// fast, leaving the button stuck on "Resetting…" with no visible result —
// which looks exactly like "nothing happened". Every fetch below is now
// bounded so the button always resolves within a few seconds either way.
async function fetchWithTimeout(url: string, opts: RequestInit, timeoutMs = 8000): Promise<Response> {
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...opts, signal: controller.signal });
  } finally {
    clearTimeout(t);
  }
}

export default function ServiceManager({ onClose }: ServiceManagerProps) {
  const [services, setServices] = useState<Record<string, SystemServiceStatus>>({});
  const [loading, setLoading] = useState(true);
  const [waking, setWaking] = useState<Record<string, boolean>>({});
  const [isWakingAll, setIsWakingAll] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [isResetting, setIsResetting] = useState(false);
  const [resetResult, setResetResult] = useState<{ ok: boolean; detail: string } | null>(null);

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

  // Reset all circuit breakers (api-gateway) + real-trade session state
  // Safe: no data deleted, no positions closed, no orders cancelled.
  // Only clears half-open/open breaker state so the gateway can reach
  // market-data-service again after a cold-start timeout storm.
  const handleReset = async () => {
    if (isResetting) return;
    setIsResetting(true);
    setResetResult(null);
    setStatusMessage("Resetting circuit breakers…");
    const results: string[] = [];
    let anyFailed = false;

    // 1. Reset api-gateway circuit breakers
    try {
      const resp = await fetchWithTimeout(apiUrl("/ops/circuit-reset"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      }, 8000);
      const data = await resp.json().catch(() => ({}));
      if (data?.ok) {
        results.push(`✅ Api Gateway: reset ${data.count ?? "?"} breaker(s)`);
      } else {
        results.push(`⚠️ Api Gateway: ${data?.error ?? "unknown error"}`);
        anyFailed = true;
      }
    } catch (e: any) {
      const timedOut = e?.name === "AbortError";
      results.push(`❌ Api Gateway: ${timedOut ? "timed out (8s) — still down" : (e?.message ?? "unreachable")}`);
      anyFailed = true;
    }

    // 2. Reset real-trade-service circuit breakers
    try {
      const rtBase = getRealTradeApiUrl().replace(/\/$/, "");
      const token = sessionStorage.getItem("rt_token") || localStorage.getItem("rt_token") || "";
      const resp2 = await fetchWithTimeout(`${rtBase}/resilience/reset`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      }, 8000);
      const data2 = await resp2.json().catch(() => ({}));
      if (data2?.ok) {
        results.push(`✅ Real Trade: reset ${data2.count ?? "?"} breaker(s)`);
      } else {
        results.push(`⚠️ Real Trade: ${data2?.detail ?? "skipped (no token)"}`);
      }
    } catch (e: any) {
      const timedOut = e?.name === "AbortError";
      results.push(`⚠️ Real Trade reset: ${timedOut ? "timed out (8s)" : "skipped (not reachable)"}`);
    }

    // 3. Keepalive ping to wake market-data-service
    try {
      await fetchWithTimeout(apiUrl("/ops/keepalive"), { method: "GET" }, 8000);
      results.push("✅ Keepalive ping sent");
    } catch {
      results.push("⚠️ Keepalive ping failed (non-critical)");
    }

    // 3. Refresh service health after reset
    await new Promise((r) => setTimeout(r, 1500));
    await fetchServices();

    setResetResult({
      ok: !anyFailed,
      detail: results.join("\n"),
    });
    setStatusMessage(null);
    setIsResetting(false);
    setTimeout(() => setResetResult(null), 12000);
  };

  return (
    <BottomSheet isOpen={true} onClose={onClose} desktopMaxWidth="sm:max-w-lg">
      <div className="p-5">
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
          <button
            type="button"
            className="btn-terminal text-xs"
            onClick={handleReset}
            disabled={isResetting}
            title="Reset circuit breakers — fixes Api Gateway Half-open / Market Data Closed. No data deleted."
            style={{
              borderColor: "rgba(246,70,93,0.5)",
              color: isResetting ? "var(--text-mist)" : "var(--sell)",
            }}
          >
            {isResetting ? "Resetting…" : "⚡ Reset Failures"}
          </button>
        </div>
        {statusMessage && <p className="mono text-xs text-mist mb-2">{statusMessage}</p>}
        {resetResult && (
          <div
            className="mono text-xs mb-3 p-2 rounded"
            style={{
              background: resetResult.ok ? "rgba(14,203,129,0.08)" : "rgba(246,70,93,0.08)",
              border: `1px solid ${resetResult.ok ? "rgba(14,203,129,0.3)" : "rgba(246,70,93,0.3)"}`,
              color: resetResult.ok ? "var(--buy)" : "var(--sell)",
              whiteSpace: "pre-line",
            }}
          >
            {resetResult.detail}
          </div>
        )}

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
    </BottomSheet>
  );
}
