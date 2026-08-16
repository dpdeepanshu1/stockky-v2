// frontend/src/components/Training.tsx

import { useEffect, useState, useRef } from "react";
import { api, TrainingStatusResponse, PeriodRollupItem, TrainingProgress } from "../api";

export default function Training() {
  const [status, setStatus] = useState<TrainingStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [training, setTraining] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [showFolds, setShowFolds] = useState(false);
  const [toast, setToast] = useState<{ type: "success" | "error" | "info"; message: string } | null>(null);
  const [isStopping, setIsStopping] = useState(false);

  const [predictions, setPredictions] = useState<any[]>([]);
  const [insights, setInsights] = useState<any[]>([]);
  const [summaryMetrics, setSummaryMetrics] = useState<any>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [loadingInsights, setLoadingInsights] = useState(false);

  // NEW: daily/weekly pick-tracking rollup
  const [periodView, setPeriodView] = useState<"daily" | "weekly">("daily");
  const [periodRollup, setPeriodRollup] = useState<PeriodRollupItem[]>([]);
  const [loadingRollup, setLoadingRollup] = useState(false);

  // NEW: manual intervention controls (for when scheduler-service isn't running)
  const [runningT1, setRunningT1] = useState(false);
  const [runningT5, setRunningT5] = useState(false);

  // NEW: animated live training progress
  const [trainProgress, setTrainProgress] = useState<TrainingProgress | null>(null);
  const progressPollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const timerIntervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ---------- API calls ----------
  const fetchStatus = async () => {
    try {
      setLoading(true);
      const data = await api.getTrainingStatus();
      setStatus(data);

      // If training is active in UI and backend says it's not running anymore
      if (training && !data.training_in_progress) {
        // Training finished on server — stop local timer/UI
        setTraining(false);
        if (timerIntervalRef.current) { clearInterval(timerIntervalRef.current); timerIntervalRef.current = null; }
        const succeeded = Boolean(data.production_model_exists && data.last_training);
        stopTraining(succeeded);
      }
    } catch (err) {
      showToast("error", "Failed to fetch training status.");
    } finally {
      setLoading(false);
    }
  };

  const fetchPredictionHistory = async () => {
    setLoadingHistory(true);
    try {
      const data = await api.getPredictionHistory(20);
      setPredictions(data.predictions || []);
    } catch (err) {
      console.error("Failed to fetch prediction history:", err);
    } finally {
      setLoadingHistory(false);
    }
  };

  const fetchPeriodRollup = async (view: "daily" | "weekly") => {
    setLoadingRollup(true);
    try {
      const data = view === "daily" ? await api.getDailyRollup(30) : await api.getWeeklyRollup(12);
      setPeriodRollup(Array.isArray(data) ? data : []);
    } catch (err) {
      console.error("Failed to fetch period rollup:", err);
    } finally {
      setLoadingRollup(false);
    }
  };

  const fetchInsights = async () => {
    setLoadingInsights(true);
    try {
      const response = await fetch(
        `${import.meta.env.VITE_API_URL}/training/api/insights`,
        { headers: { "Content-Type": "application/json" } }
      );
      if (response.ok) {
        const data = await response.json();
        setInsights(data.insights || []);
      }
    } catch (err) {
      console.error("Failed to fetch insights:", err);
    } finally {
      setLoadingInsights(false);
    }
  };

  const fetchSummaryMetrics = async () => {
    try {
      const response = await fetch(
        `${import.meta.env.VITE_API_URL}/training/api/metrics/summary`,
        { headers: { "Content-Type": "application/json" } }
      );
      if (response.ok) {
        const data = await response.json();
        setSummaryMetrics(data.latest_run || null);
      }
    } catch (err) {
      console.error("Failed to fetch summary metrics:", err);
    }
  };

  const clearLock = async () => {
    try {
      await api.clearTrainingLock();
      showToast("success", "Training lock cleared.");
      return true;
    } catch {
      showToast("error", "Failed to clear lock.");
      return false;
    }
  };

  // Manual fallback for evaluate_t1/evaluate_t5, in case scheduler-service
  // isn't running. Sweeps every pending prediction, same endpoint the
  // backend would call on a cron.
  const runEvaluation = async (period: "t1" | "t5") => {
    const setRunning = period === "t1" ? setRunningT1 : setRunningT5;
    setRunning(true);
    try {
      await api.triggerEvaluation(period);
      showToast("info", `${period.toUpperCase()} evaluation sweep started.`);
      setTimeout(() => {
        fetchPredictionHistory();
        fetchPeriodRollup(periodView);
      }, 5000);
    } catch {
      showToast("error", `Failed to trigger ${period.toUpperCase()} evaluation.`);
    } finally {
      setRunning(false);
    }
  };

  // ---------- Lifecycle ----------
  useEffect(() => {
    fetchStatus();
    fetchPredictionHistory();
    fetchInsights();
    fetchSummaryMetrics();
    fetchPeriodRollup("daily");
    return () => {
      if (timerIntervalRef.current) clearInterval(timerIntervalRef.current);
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    };
  }, []);

  useEffect(() => {
    fetchPeriodRollup(periodView);
  }, [periodView]);

  // Poll /api/train/progress while a run is active, for the animated panel.
  useEffect(() => {
    const isActive = training || status?.training_in_progress;
    if (!isActive) {
      if (progressPollRef.current) clearInterval(progressPollRef.current);
      return;
    }
    const poll = async () => {
      try {
        const p = await api.getTrainingProgress();
        setTrainProgress(p);
      } catch {
        // Progress endpoint may not be routed through the gateway yet —
        // fail quietly, the rest of the training UI still works without it.
      }
    };
    poll();
    progressPollRef.current = setInterval(poll, 2000);
    return () => {
      if (progressPollRef.current) clearInterval(progressPollRef.current);
    };
  }, [training, status?.training_in_progress]);

  // ---------- UI helpers ----------
  const showToast = (type: "success" | "error" | "info", message: string) => {
    setToast({ type, message });
    setTimeout(() => setToast(null), 5000);
  };

  const startTraining = () => {
    setTraining(true);
    setElapsedSeconds(0);

    if (timerIntervalRef.current) clearInterval(timerIntervalRef.current);
    timerIntervalRef.current = setInterval(() => {
      setElapsedSeconds((prev) => prev + 1);
    }, 1000);

    if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    pollIntervalRef.current = setInterval(() => {
      fetchStatus();
    }, 5000);
  };

  const stopTraining = (success: boolean) => {
    setTraining(false);
    // Update status so the "running" card disappears
    setStatus((prev) => prev ? { ...prev, training_in_progress: false } : prev);
    if (timerIntervalRef.current) {
      clearInterval(timerIntervalRef.current);
      timerIntervalRef.current = null;
    }
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
    if (success) {
      showToast("success", "✅ Training completed successfully! Model is deployed.");
      fetchStatus(); // refresh to get latest metrics
    } else {
      // Only show error if we were actually training and it failed
      if (training) {
        showToast("error", "❌ Training stopped or interrupted.");
      }
    }
  };

  // ---------- Handlers ----------
  const handleTriggerTraining = async () => {
    if (training) return;

    if (status?.training_in_progress) {
      showToast("info", "⏳ Training is already running. Monitoring...");
      startTraining();
      return;
    }

    showToast("info", "⏳ Starting training...");

    try {
      const response = await api.triggerTraining();
      if (response.status === "started" || response.status === "Training started successfully" || (response as any).ok === true || String(response.status || "").toLowerCase().includes("start")) {
        startTraining();
        showToast("info", "⏳ Training started. This may take a few minutes.");
      } else {
        showToast("error", `⚠️ Training failed: ${response.status}`);
      }
    } catch (err: any) {
      if (err?.status === 409 || err?.message?.includes("409")) {
        showToast("info", "⏳ Training already in progress. Resuming monitoring...");
        startTraining();
      } else {
        showToast("error", "❌ Failed to trigger training. Please try again.");
      }
    }
  };

  const handleStopTraining = async () => {
    if (!training) {
      showToast("info", "No training in progress.");
      return;
    }
    setIsStopping(true);

    const cleared = await clearLock();
    // Always stop the UI regardless of lock status
    stopTraining(false);
    if (!cleared) {
      showToast("info", "Lock may already be cleared. Training stopped in UI.");
    }
    setIsStopping(false);
  };

  const handleRefresh = () => {
    fetchStatus();
    fetchPredictionHistory();
    fetchInsights();
    fetchSummaryMetrics();
    showToast("info", "Refreshing all data...");
  };

  const handleRestart = () => {
    window.location.reload();
  };

  // ---------- Render helpers ----------
  const formatDate = (dateStr: string | null | undefined) => {
    if (!dateStr) return "Never";
    const dt = new Date(dateStr);
    // Force IST display
    const formatter = new Intl.DateTimeFormat("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
    return formatter.format(dt);
  };

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
  };

  const renderMetrics = (metrics: Record<string, number>) => {
    if (!metrics || Object.keys(metrics).length === 0) {
      return <p className="text-mist/40 text-sm">No walk‑forward metrics available.</p>;
    }

    const metricLabels: Record<string, string> = {
      SharpeRatio: "Sharpe Ratio",
      SortinoRatio: "Sortino Ratio",
      MaximumDrawdown: "Max Drawdown",
      MaximumDrawdownDuration: "Max Drawdown Duration (days)",
      WinRate: "Win Rate",
      ProfitFactor: "Profit Factor",
      CumulativeReturn: "Cumulative Return",
      DirectionalAccuracy: "Directional Accuracy",
      RMSE: "RMSE",
      MAE: "MAE",
    };

    const formatValue = (key: string, value: number) => {
      if (key === "MaximumDrawdown" || key === "CumulativeReturn" || key === "WinRate" || key === "DirectionalAccuracy") {
        return (value * 100).toFixed(2) + "%";
      }
      if (key === "ProfitFactor" || key === "SharpeRatio" || key === "SortinoRatio") {
        return value.toFixed(3);
      }
      return value.toFixed(4);
    };

    return (
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mt-2">
        {Object.entries(metricLabels).map(([key, label]) => {
          const val = metrics[key];
          if (val === undefined || val === null) return null;
          return (
            <div key={key} className="bg-ink/40 border border-slate/40 rounded-lg px-3 py-2">
              <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">{label}</div>
              <div className="font-mono text-sm text-paper mt-0.5">{formatValue(key, val)}</div>
            </div>
          );
        })}
      </div>
    );
  };

  const renderPeriodRollup = () => {
    if (loadingRollup) return <Spinner />;
    if (periodRollup.length === 0) {
      return (
        <p className="text-mist/40 text-sm py-2">
          No picks recorded in this window yet. Every BUY NOW / PREPARE TO BUY
          from a market scan lands here once decision-engine records it.
        </p>
      );
    }
    return (
      <div className="overflow-x-auto mt-2">
        <table className="w-full text-xs font-mono">
          <thead>
            <tr className="text-mist/50 border-b border-slate/40">
              <th className="text-left py-1 pr-3">{periodView === "daily" ? "Date" : "Week"}</th>
              <th className="text-right py-1 px-2">Picks</th>
              <th className="text-right py-1 px-2">Buy Now</th>
              <th className="text-right py-1 px-2">Prepare</th>
              <th className="text-right py-1 px-2">T+1 Success</th>
              <th className="text-right py-1 px-2">T+1 Avg</th>
              <th className="text-right py-1 px-2">T+5 Success</th>
              <th className="text-right py-1 px-2">T+5 Avg</th>
              <th className="text-right py-1 pl-2">Pending</th>
            </tr>
          </thead>
          <tbody>
            {periodRollup.map((row) => (
              <tr key={row.period} className="border-b border-slate/30">
                <td className="py-1 pr-3 text-paper">{row.period}</td>
                <td className="text-right py-1 px-2 text-mist/80">{row.predictions_recorded}</td>
                <td className="text-right py-1 px-2 text-mist/80">{row.buy_now}</td>
                <td className="text-right py-1 px-2 text-mist/80">{row.prepare_to_buy}</td>
                <td className={`text-right py-1 px-2 ${row.t1_success_rate == null ? "text-mist/40" : row.t1_success_rate >= 50 ? "text-signal-buy" : "text-red-400"}`}>
                  {row.t1_success_rate == null ? "—" : `${row.t1_success_rate}%`}
                </td>
                <td className="text-right py-1 px-2 text-mist/60">{row.t1_avg_return_pct == null ? "—" : `${row.t1_avg_return_pct}%`}</td>
                <td className={`text-right py-1 px-2 ${row.t5_success_rate == null ? "text-mist/40" : row.t5_success_rate >= 50 ? "text-signal-buy" : "text-red-400"}`}>
                  {row.t5_success_rate == null ? "—" : `${row.t5_success_rate}%`}
                </td>
                <td className="text-right py-1 px-2 text-mist/60">{row.t5_avg_return_pct == null ? "—" : `${row.t5_avg_return_pct}%`}</td>
                <td className="text-right py-1 pl-2 text-mist/50">{row.t1_pending}/{row.t5_pending}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  };

  const renderPredictionHistory = () => {
    if (loadingHistory) return <Spinner />;
    if (!predictions.length) return <p className="text-mist/40 text-sm">No predictions recorded yet.</p>;

    return (
      <div className="overflow-x-auto mt-2">
        <table className="w-full text-xs font-mono">
          <thead>
            <tr className="text-mist/50 border-b border-slate/40">
              <th className="text-left py-1 pr-4">Symbol</th>
              <th className="text-left py-1 pr-4">Decision</th>
              <th className="text-left py-1 pr-4">Price</th>
              <th className="text-left py-1 pr-4">Date</th>
              <th className="text-left py-1 pr-4">T+1 Success</th>
              <th className="text-left py-1 pr-4">T+5 Success</th>
              <th className="text-left py-1">Outcome</th>
            </tr>
          </thead>
          <tbody>
            {predictions.map((p) => (
              <tr key={p.prediction_id} className="border-b border-slate/30">
                <td className="py-1 pr-4 text-paper">{p.symbol}</td>
                <td className="py-1 pr-4 text-mist/80">{p.decision}</td>
                <td className="py-1 pr-4 text-mist/80">₹{p.price?.toFixed(2) || "—"}</td>
                <td className="py-1 pr-4 text-mist/60">{formatDate(p.timestamp)}</td>
                <td className="py-1 pr-4">
                  <span className={p.t1_success === 1 ? "text-signal-buy" : p.t1_success === 0 ? "text-mist/40" : "text-red-400"}>
                    {p.t1_success === 1 ? "✅" : p.t1_success === 0 ? "⏳" : "❌"}
                  </span>
                </td>
                <td className="py-1 pr-4">
                  <span className={p.t5_success === 1 ? "text-signal-buy" : p.t5_success === 0 ? "text-mist/40" : "text-red-400"}>
                    {p.t5_success === 1 ? "✅" : p.t5_success === 0 ? "⏳" : "❌"}
                  </span>
                </td>
                <td className="py-1">
                  {p.outcomes?.length ? (
                    <span className="text-mist/60">
                      {p.outcomes.map((o: any) => `${o.period}: ${o.return_pct?.toFixed(2) || "—"}%`).join(" | ")}
                    </span>
                  ) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  };

  const renderInsights = () => {
    if (loadingInsights) return <Spinner />;
    if (!insights.length) return <p className="text-mist/40 text-sm">No insights available yet.</p>;

    return (
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-2">
        {insights.map((insight, idx) => (
          <div key={idx} className="bg-ink/40 border border-slate/40 rounded-lg px-3 py-2">
            <div className="font-mono text-xs text-paper">{insight.insight}</div>
            <div className="flex gap-3 mt-1 text-xs text-mist/60">
              <span>📊 {insight.sample_size} samples</span>
              <span className={insight.confidence === "high" ? "text-signal-buy" : "text-yellow-400"}>
                {insight.confidence} confidence
              </span>
              {insight.active && <span className="text-green-400">• active</span>}
            </div>
          </div>
        ))}
      </div>
    );
  };

  return (
    <div className="space-y-6">
      {/* Toast Alert */}
      {toast && (
        <div
          className={`fixed top-20 right-4 z-50 px-5 py-3 rounded-xl shadow-2xl font-mono text-sm flex items-center gap-3 transition-all duration-300 transform ${
            toast.type === "success"
              ? "bg-green-500/20 border border-green-400/40 text-green-400"
              : toast.type === "error"
              ? "bg-red-500/20 border border-red-400/40 text-red-400"
              : "bg-blue-500/20 border border-blue-400/40 text-blue-400"
          } animate-slideIn`}
        >
          <span>{toast.message}</span>
          <button
            onClick={() => setToast(null)}
            className="ml-2 text-mist/60 hover:text-paper transition"
          >
            ✕
          </button>
        </div>
      )}

      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h2 className="font-display text-2xl text-paper">🧠 Training Intelligence</h2>
        <div className="flex flex-wrap gap-3">
          <button
            onClick={handleTriggerTraining}
            disabled={training || status?.training_in_progress}
            className={`font-mono text-sm px-5 py-2 rounded-lg transition-all ${
              training || status?.training_in_progress
                ? "bg-slate/30 text-mist/50 cursor-not-allowed"
                : "bg-signal-prepare/20 text-signal-prepare border border-signal-prepare/30 hover:bg-signal-prepare/30"
            }`}
          >
            {training || status?.training_in_progress ? (
              <span className="flex items-center gap-2">
                <Spinner />
                {training ? "Training..." : "Running..."}
              </span>
            ) : (
              "⚡ Trigger Training"
            )}
          </button>

          <button
            onClick={handleStopTraining}
            disabled={!training || isStopping}
            className="font-mono text-sm px-4 py-2 rounded-lg bg-red-500/20 text-red-400 border border-red-400/30 hover:bg-red-500/30 transition disabled:opacity-50"
          >
            {isStopping ? "Stopping..." : "⏹ Stop Training"}
          </button>

          <button
            onClick={handleRefresh}
            className="font-mono text-sm px-4 py-2 rounded-lg bg-blue-500/20 text-blue-400 border border-blue-400/30 hover:bg-blue-500/30 transition"
          >
            🔄 Refresh
          </button>

          <button
            onClick={handleRestart}
            className="font-mono text-sm px-4 py-2 rounded-lg bg-yellow-500/20 text-yellow-400 border border-yellow-400/30 hover:bg-yellow-500/30 transition"
          >
            🔁 Restart Page
          </button>
        </div>
      </div>

      {/* Manual intervention: for when scheduler-service isn't running these on
          its own cron. Each button hits the same endpoint the automation would. */}
      <div className="bg-graphite border border-slate/60 rounded-xl p-4">
        <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-3">
          🛠️ Manual Controls (use if automation isn't running)
        </h3>
        <div className="flex flex-wrap gap-3">
          <button
            onClick={() => runEvaluation("t1")}
            disabled={runningT1}
            className="font-mono text-xs px-4 py-2 rounded-lg bg-slate/30 text-mist hover:bg-slate/50 transition disabled:opacity-50 flex items-center gap-2"
          >
            {runningT1 ? <Spinner /> : null} Run T+1 Evaluation Sweep
          </button>
          <button
            onClick={() => runEvaluation("t5")}
            disabled={runningT5}
            className="font-mono text-xs px-4 py-2 rounded-lg bg-slate/30 text-mist hover:bg-slate/50 transition disabled:opacity-50 flex items-center gap-2"
          >
            {runningT5 ? <Spinner /> : null} Run T+5 Evaluation Sweep
          </button>
        </div>
        <p className="text-mist/40 text-[11px] mt-2">
          Trade mark-to-market has its own manual trigger on the Trades tab.
        </p>
      </div>

      {/* Training in progress: animated stage pipeline */}
      {(training || status?.training_in_progress) && (
        <div className="bg-graphite border border-signal-prepare/30 rounded-xl p-5">
          <div className="flex items-center gap-4 mb-4">
            <Spinner size="lg" />
            <div>
              <h3 className="font-display text-lg text-signal-prepare">
                {training ? "Training in progress..." : "Training is running in background..."}
              </h3>
              <div className="flex flex-wrap gap-6 mt-1 text-sm">
                <div>
                  <span className="text-mist/60">Elapsed: </span>
                  <span className="font-mono text-paper">{formatTime(elapsedSeconds)}</span>
                </div>
                {(() => {
                  const eta = estimateTrainingRemaining(trainProgress?.stage, elapsedSeconds);
                  return eta != null && eta > 0 ? (
                    <div>
                      <span className="text-mist/60">Est. remaining: </span>
                      <span className="font-mono text-paper">~{formatTime(eta)}</span>
                    </div>
                  ) : null;
                })()}
              </div>
            </div>
          </div>

          <StageTracker stage={trainProgress?.stage} />

          {trainProgress?.detail && Object.keys(trainProgress.detail).length > 0 && (
            <div className="mt-4 bg-ink/40 border border-slate/30 rounded-lg p-3 font-mono text-xs">
              {trainProgress.detail.dataset_size != null && (
                <div className="text-mist/70">
                  Dataset: <span className="text-paper">{String(trainProgress.detail.dataset_size)}</span> examples across{" "}
                  <span className="text-paper">{String(trainProgress.detail.num_symbols)}</span> symbols
                </div>
              )}
              {Array.isArray(trainProgress.detail.symbols_sample) && trainProgress.detail.symbols_sample.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {(trainProgress.detail.symbols_sample as string[]).map((s, i) => (
                    <span
                      key={s}
                      className="bg-signal-prepare/10 border border-signal-prepare/30 text-signal-prepare rounded px-2 py-0.5 text-[10px]"
                      style={{ animation: `fadeInUp 0.3s ease-out ${i * 0.05}s both` }}
                    >
                      {s}
                    </span>
                  ))}
                </div>
              )}
              {trainProgress.detail.train_samples != null && trainProgress.detail.holdout_samples != null && (
                <div className="text-mist/70 mt-2">
                  Split: <span className="text-paper">{String(trainProgress.detail.train_samples)}</span> train /{" "}
                  <span className="text-paper">{String(trainProgress.detail.holdout_samples)}</span> holdout
                </div>
              )}
            </div>
          )}

          <div className="mt-2 text-xs text-mist/40">
            The page auto‑updates when done. You can also click Refresh or Stop.
          </div>
        </div>
      )}

      <style>{`
        @keyframes fadeInUp {
          from { opacity: 0; transform: translateY(4px); }
          to { opacity: 1; transform: translateY(0); }
        }
      `}</style>

      {/* Status Cards */}
      {loading ? (
        <div className="flex justify-center py-12">
          <Spinner size="lg" />
        </div>
      ) : (
        <div className="space-y-6">
          {/* Production Model */}
          <div className="bg-graphite border border-slate rounded-xl p-5">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
              📦 Production Model
            </h3>
            {status?.production_model_exists ? (
              <div>
                <span className="font-mono text-sm text-signal-buy">✅ Deployed</span>
                <div className="mt-2 text-xs text-mist/60">
                  Last training: {formatDate(status?.last_training)}
                  {status?.model_version && (
                    <span className="ml-4 text-mist/40">Version: {status.model_version}</span>
                  )}
                </div>
              </div>
            ) : (
              <p className="text-mist/40 text-sm">No production model deployed.</p>
            )}
          </div>

          {/* Overall Stats */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <StatCard label="Last Training" value={formatDate(status?.last_training)} />
            <StatCard label="Dataset Size" value={status?.dataset_size ?? 0} />
            <StatCard label="Symbols" value={status?.num_symbols ?? 0} />
          </div>

          {/* Walk‑Forward Metrics */}
          <div className="bg-graphite border border-slate/60 rounded-xl p-5">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
              📉 Walk‑Forward Performance Metrics
            </h3>
            {renderMetrics(status?.metrics || {})}
          </div>

          {/* Fold Details */}
          {status?.fold_details && status.fold_details.length > 0 && (
            <div className="bg-graphite border border-slate/40 rounded-xl p-5">
              <button
                onClick={() => setShowFolds(!showFolds)}
                className="font-mono text-xs text-mist uppercase tracking-widest flex items-center gap-2 hover:text-paper transition"
              >
                📋 Fold Details
                <span className="text-xs">{showFolds ? "▲" : "▼"}</span>
              </button>
              {showFolds && (
                <div className="mt-3 overflow-x-auto">
                  <table className="w-full text-xs font-mono">
                    <thead>
                      <tr className="text-mist/50 border-b border-slate/40">
                        <th className="text-left py-1 pr-4">Fold</th>
                        <th className="text-left py-1 pr-4">Train Start</th>
                        <th className="text-left py-1 pr-4">Train End</th>
                        <th className="text-left py-1 pr-4">Val Start</th>
                        <th className="text-left py-1 pr-4">Val End</th>
                        <th className="text-left py-1 pr-4">Train Samples</th>
                        <th className="text-left py-1">Val Samples</th>
                      </tr>
                    </thead>
                    <tbody>
                      {status.fold_details.map((fold) => (
                        <tr key={fold.fold} className="border-b border-slate/30">
                          <td className="py-1 pr-4 text-paper">{fold.fold}</td>
                          <td className="py-1 pr-4 text-mist/70">{fold.train_start}</td>
                          <td className="py-1 pr-4 text-mist/70">{fold.train_end}</td>
                          <td className="py-1 pr-4 text-mist/70">{fold.val_start}</td>
                          <td className="py-1 pr-4 text-mist/70">{fold.val_end}</td>
                          <td className="py-1 pr-4 text-mist/70">{fold.train_samples}</td>
                          <td className="py-1 text-mist/70">{fold.val_samples}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Daily / Weekly Pick Tracking */}
          <div className="bg-graphite border border-slate/60 rounded-xl p-5">
            <div className="flex items-center justify-between mb-2">
              <h3 className="font-mono text-xs text-mist uppercase tracking-widest">
                📅 Pick Tracking (T+1 / T+5 by {periodView === "daily" ? "day" : "week"})
              </h3>
              <div className="flex gap-1 bg-ink/40 border border-slate/40 rounded-lg p-0.5">
                {(["daily", "weekly"] as const).map((v) => (
                  <button
                    key={v}
                    onClick={() => setPeriodView(v)}
                    className={`px-3 py-1 text-xs font-mono uppercase rounded-md transition-colors ${
                      periodView === v ? "bg-slate/60 text-paper" : "text-mist/50 hover:text-mist"
                    }`}
                  >
                    {v}
                  </button>
                ))}
              </div>
            </div>
            {renderPeriodRollup()}
          </div>

          {/* Prediction History */}
          <div className="bg-graphite border border-slate/60 rounded-xl p-5">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
              📋 Prediction History (T+1 / T+5 tracking)
            </h3>
            {renderPredictionHistory()}
          </div>

          {/* Summary Metrics */}
          {summaryMetrics && (
            <div className="bg-graphite border border-slate/60 rounded-xl p-5">
              <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
                📊 Training Run Summary
              </h3>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-2">
                <StatCard label="Run Date" value={formatDate(summaryMetrics.timestamp)} />
                <StatCard label="Dataset Size" value={summaryMetrics.dataset_size || 0} />
                <StatCard label="Symbols" value={summaryMetrics.num_symbols || 0} />
                <StatCard label="Sharpe" value={summaryMetrics.metrics?.SharpeRatio?.toFixed(3) || "—"} />
              </div>
            </div>
          )}

          {/* Learning Insights */}
          <div className="bg-graphite border border-slate/60 rounded-xl p-5">
            <h3 className="font-mono text-xs text-mist uppercase tracking-widest mb-2">
              💡 Learning Insights
            </h3>
            {renderInsights()}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Helper Components ──

const TRAINING_STAGES: { key: string; label: string }[] = [
  { key: "loading_data", label: "Loading" },
  { key: "data_loaded", label: "Loaded" },
  { key: "splitting", label: "Splitting" },
  { key: "fitting_model", label: "Fitting" },
  { key: "evaluating", label: "Evaluating" },
  { key: "saving_model", label: "Saving" },
  { key: "done", label: "Done" },
];

function StageTracker({ stage }: { stage?: string }) {
  const currentIdx = TRAINING_STAGES.findIndex((s) => s.key === stage);
  // No stage data yet (still connecting, or /api/train/progress hasn't
  // responded on this poll) — previously this silently rendered every dot
  // as plain "pending" with nothing highlighted at all, which looked
  // exactly like a static list of labels rather than a live pipeline.
  // Showing "connecting" and lighting the first dot makes it obvious
  // something is actually happening rather than looking broken.
  const connecting = currentIdx === -1 && stage !== "done";
  const effectiveIdx = connecting ? 0 : currentIdx;

  return (
    <div>
      {connecting && (
        <div className="text-[10px] font-mono text-mist/40 uppercase tracking-widest mb-2">
          Connecting to training progress...
        </div>
      )}
      <div className="flex items-center">
        {TRAINING_STAGES.map((s, i) => {
          const isDone = effectiveIdx > i || stage === "done";
          const isCurrent = effectiveIdx === i && stage !== "done";
          return (
            <div key={s.key} className="flex items-center flex-1 last:flex-none">
              <div className="flex flex-col items-center gap-1">
                <div
                  className={`w-3 h-3 rounded-full border-2 transition-all duration-300 ${
                    isDone
                      ? "bg-signal-buy border-signal-buy"
                      : isCurrent
                      ? "bg-signal-prepare border-signal-prepare animate-pulse scale-125"
                      : "bg-transparent border-slate/50"
                  }`}
                />
                <span
                  className={`text-[9px] font-mono uppercase whitespace-nowrap ${
                    isDone ? "text-signal-buy" : isCurrent ? "text-signal-prepare" : "text-mist/30"
                  }`}
                >
                  {s.label}
                </span>
              </div>
              {i < TRAINING_STAGES.length - 1 && (
                <div
                  className={`h-0.5 flex-1 mx-1 transition-all duration-500 ${
                    isDone ? "bg-signal-buy" : "bg-slate/30"
                  }`}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Rough ETA: same linear-extrapolation approach the scan progress already
// uses (average time per unit so far, projected across what's left) —
// here the "unit" is pipeline stages rather than symbols. Training doesn't
// have a symbol count to extrapolate from, so this is necessarily rougher,
// labeled clearly as an estimate rather than a precise countdown.
function estimateTrainingRemaining(stage: string | undefined, elapsedSeconds: number): number | null {
  const idx = TRAINING_STAGES.findIndex((s) => s.key === stage);
  if (idx < 0 || stage === "done" || elapsedSeconds <= 0) return null;
  const stagesElapsed = idx + 1;
  const stagesRemaining = TRAINING_STAGES.length - stagesElapsed;
  if (stagesRemaining <= 0) return null;
  const avgPerStage = elapsedSeconds / stagesElapsed;
  return Math.round(avgPerStage * stagesRemaining);
}

function Spinner({ size = "sm" }: { size?: "sm" | "lg" }) {
  const dimension = size === "lg" ? "w-8 h-8" : "w-4 h-4";
  return (
    <div className={`${dimension} border-2 border-current border-t-transparent rounded-full animate-spin`} />
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-ink/40 border border-slate/40 rounded-xl px-4 py-3">
      <div className="font-mono text-[10px] text-mist/50 uppercase tracking-wider">{label}</div>
      <div className="font-mono text-lg text-paper mt-1">{value}</div>
    </div>
  );
}