/**
 * Training / T+1 / T+5 progress panel — same style as Market Scan Pipeline.
 *
 * Usage in Training.tsx:
 *   import TrainingProgressPanel from "./TrainingProgressPanel";
 *   <TrainingProgressPanel
 *     stage={trainProgress?.stage}
 *     detail={trainProgress?.detail}
 *     elapsedSec={elapsed}
 *     estimatedTotalSec={estimatedTotal}
 *     active={training || status?.training_in_progress}
 *   />
 */

import React, { useMemo } from "react";

type StageKey =
  | "idle"
  | "loading_data"
  | "building_features"
  | "walk_forward"
  | "calibrating"
  | "saving_model"
  | "evaluating_t1"
  | "evaluating_t5"
  | "done"
  | "aborted"
  | "error";

const STAGES: { key: StageKey; label: string; detail: string; weight: number }[] = [
  { key: "loading_data", label: "Loading data", detail: "Pulling universe samples & history", weight: 15 },
  { key: "building_features", label: "Building features", detail: "Technical + fund + news + peer", weight: 20 },
  { key: "walk_forward", label: "Walk-forward training", detail: "Training folds out-of-sample", weight: 35 },
  { key: "calibrating", label: "Calibration", detail: "Aligning probabilities", weight: 10 },
  { key: "saving_model", label: "Saving model", detail: "Writing production artifact", weight: 8 },
  { key: "evaluating_t1", label: "T+1 evaluation", detail: "Scoring yesterday's signals", weight: 6 },
  { key: "evaluating_t5", label: "T+5 evaluation", detail: "Scoring 5-day outcomes", weight: 6 },
];

function stageIndex(stage?: string | null): number {
  if (!stage) return -1;
  const map: Record<string, StageKey> = {
    loading_data: "loading_data",
    load: "loading_data",
    building_features: "building_features",
    features: "building_features",
    walk_forward: "walk_forward",
    training: "walk_forward",
    calibrating: "calibrating",
    calibration: "calibrating",
    saving_model: "saving_model",
    save: "saving_model",
    evaluating_t1: "evaluating_t1",
    t1: "evaluating_t1",
    evaluating_t5: "evaluating_t5",
    t5: "evaluating_t5",
    done: "done",
    aborted: "aborted",
    error: "error",
  };
  const key = map[stage] || (stage as StageKey);
  if (key === "done") return STAGES.length;
  if (key === "aborted" || key === "error") return -2;
  return STAGES.findIndex((s) => s.key === key);
}

function formatTime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "--:--";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export default function TrainingProgressPanel({
  stage,
  detail,
  elapsedSec = 0,
  estimatedTotalSec = 180,
  active = false,
  title = "Training pipeline",
}: {
  stage?: string | null;
  detail?: Record<string, unknown> | null;
  elapsedSec?: number;
  estimatedTotalSec?: number;
  active?: boolean;
  title?: string;
}) {
  const idx = stageIndex(stage);
  const done = stage === "done";
  const failed = stage === "aborted" || stage === "error";

  const progressPct = useMemo(() => {
    if (done) return 100;
    if (idx < 0) return active ? 5 : 0;
    let completed = 0;
    let total = 0;
    STAGES.forEach((s, i) => {
      total += s.weight;
      if (i < idx) completed += s.weight;
      else if (i === idx) completed += s.weight * 0.45;
    });
    return Math.min(99, Math.round((completed / total) * 100));
  }, [idx, done, active]);

  const remaining = Math.max(0, estimatedTotalSec - elapsedSec);

  if (!active && !done && !failed && idx < 0) return null;

  return (
    <div className="rounded-lg border border-zinc-700/80 bg-zinc-900/80 p-4 mb-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-zinc-100 tracking-wide">{title}</h3>
        <div className="text-xs font-mono text-cyan-400">
          {done ? "Complete" : failed ? "Stopped" : `${progressPct}%`}
        </div>
      </div>

      <div className="h-1.5 w-full rounded bg-zinc-800 overflow-hidden mb-3">
        <div
          className={`h-full transition-all duration-500 ${
            failed ? "bg-amber-500" : done ? "bg-emerald-500" : "bg-cyan-500"
          }`}
          style={{ width: `${progressPct}%` }}
        />
      </div>

      <div className="flex justify-between text-[11px] text-zinc-400 font-mono mb-3">
        <span>Elapsed {formatTime(elapsedSec)}</span>
        <span>{done || failed ? "—" : `~${formatTime(remaining)} remaining`}</span>
      </div>

      <div className="space-y-1.5">
        {STAGES.map((s, i) => {
          const state = done || i < idx ? "done" : i === idx ? "active" : "pending";
          return (
            <div key={s.key} className="flex items-start gap-2 py-1">
              <div
                className={`mt-0.5 h-2 w-2 rounded-full shrink-0 ${
                  state === "done"
                    ? "bg-emerald-500"
                    : state === "active"
                    ? "bg-cyan-400 animate-pulse"
                    : "bg-zinc-600"
                }`}
              />
              <div className="min-w-0">
                <div
                  className={`text-xs ${
                    state === "active" ? "text-cyan-300 font-medium" : state === "done" ? "text-zinc-300" : "text-zinc-500"
                  }`}
                >
                  {s.label}
                </div>
                {state === "active" && (
                  <div className="text-[11px] text-zinc-500 truncate">
                    {typeof detail?.message === "string"
                      ? detail.message
                      : s.detail}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
