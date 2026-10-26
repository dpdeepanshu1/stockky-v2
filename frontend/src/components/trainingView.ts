// E155: summary tiles for the Training Lab tab (pure, no React: tested by plain node in trainingView.test.mjs).
// Read from the status / progress state the tab already holds. A count or rate of exactly 0 stays 0; only an absent or
// non-numeric value is shown as missing ("—"). Nothing here judges the model: it reports the stored figures and their age.
export type TrainingKpi = { label: string; value: string; sub?: string; tone?: "up" | "down" | "warn" | "info" | "flat" };

type Status = {
  production_model_exists?: boolean; model_version?: string | null; production_model_version?: string | null;
  last_training?: string | null; dataset_size?: number; num_symbols?: number;
  metrics?: Record<string, number> | null; training_in_progress?: boolean;
};
type Progress = { stage?: string; percent?: number | null; is_running?: boolean; error?: string | null };
export type TrainingInput = { status?: Status | null; progress?: Progress | null; training?: boolean };

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export function trainingAge(ms: number): string {
  if (!Number.isFinite(ms) || ms < 60_000) return "just now";
  const m = Math.floor(ms / 60_000);
  if (m < 60) return `${m}m ago`;
  const h = ms / 3_600_000;
  if (h < 24) return `${h.toFixed(1)}h ago`;
  return `${(h / 24).toFixed(1)}d ago`;
}

export function trainingKpis(i: TrainingInput | null | undefined, nowMs: number): TrainingKpi[] {
  const st = i?.status;
  if (!st) return [];
  const running = !!(i?.training || st.training_in_progress || i?.progress?.is_running);
  const ver = st.production_model_version || st.model_version || null;

  const model: TrainingKpi = st.production_model_exists
    ? { label: "Model", value: "Deployed", sub: ver ? `version ${ver}` : "version not reported", tone: "info" }
    : { label: "Model", value: "None deployed", sub: "no production model yet", tone: "warn" };

  const raw = st.last_training;
  const t = raw ? new Date(raw).getTime() : NaN;
  const last: TrainingKpi = !raw ? { label: "Last training", value: "Never", sub: "no completed run", tone: "warn" }
    : !Number.isFinite(t) ? { label: "Last training", value: "—", sub: "date unreadable", tone: "warn" }
    : { label: "Last training", value: trainingAge(nowMs - t), sub: new Date(t).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" }), tone: "info" };

  const rows = num(st.dataset_size), syms = num(st.num_symbols);
  const data: TrainingKpi = { label: "Dataset", value: rows != null ? String(rows) : "—", sub: syms != null ? `${syms} symbols` : "symbol count not reported", tone: "flat" };

  const m = st.metrics || {};
  const win = num(m.WinRate), sharpe = num(m.SharpeRatio), acc = num(m.DirectionalAccuracy);
  const parts = [sharpe != null ? `Sharpe ${sharpe.toFixed(2)}` : "", acc != null ? `direction ${(acc * 100).toFixed(1)}%` : ""].filter(Boolean);
  const wf: TrainingKpi = win != null
    ? { label: "Walk-forward win rate", value: `${(win * 100).toFixed(1)}%`, sub: parts.join(" · ") || "stored evaluation", tone: "flat" }
    : { label: "Walk-forward win rate", value: "—", sub: "no walk-forward metrics stored", tone: "flat" };

  const out = [model, last, data, wf];
  if (running) {
    const p = i?.progress;
    const pct = num(p?.percent);
    if (p?.stage === "error" || p?.error) out.push({ label: "Training now", value: "Error", sub: String(p?.error || "training reported an error").slice(0, 60), tone: "warn" });
    else out.push({ label: "Training now", value: pct != null ? `${Math.round(pct)}%` : "Running", sub: p?.stage && p.stage !== "idle" ? p.stage.replace(/_/g, " ") : "waiting for progress", tone: "info" });
  }
  return out;
}

// E164: Training Lab card bodies (pure). The old cells called .toFixed on whatever the API sent: a string or NaN threw inside render,
// a missing price showed "₹—", a missing return showed "—%", and a prediction with no evaluation yet (null) was drawn as a failed one (❌).
// backend meaning (decision-prediction-service evaluate.py): t1/t5_success 0 = pending, 1 = hit, 2 = miss; anything else = not evaluated.
const PCT_KEYS = ["MaximumDrawdown", "CumulativeReturn", "WinRate", "DirectionalAccuracy"];
const RATIO_KEYS = ["ProfitFactor", "SharpeRatio", "SortinoRatio"];
export function fmtMetric(key: string, value: unknown): string | null {
  if (value === Infinity) return key === "ProfitFactor" ? "∞" : null;
  const n = num(value);
  if (n == null) return null;
  if (PCT_KEYS.includes(key)) return `${(n * 100).toFixed(2)}%`;
  if (RATIO_KEYS.includes(key)) return n.toFixed(3);
  return n.toFixed(4);
}
export function fmtFixed(value: unknown, digits: number): string { const n = num(value); return n == null ? "—" : n.toFixed(digits); }
export type OutcomeMark = { glyph: string; word: string; tone: "up" | "down" | "flat" };
export function outcomeMark(v: unknown): OutcomeMark {
  if (v === 1) return { glyph: "✅", word: "Hit", tone: "up" };
  if (v === 2) return { glyph: "❌", word: "Miss", tone: "down" };
  if (v === 0) return { glyph: "⏳", word: "Pending", tone: "flat" };
  return { glyph: "—", word: "Not evaluated", tone: "flat" };
}
export function predictionRowView(p: { price?: unknown; t1_success?: unknown; t5_success?: unknown; outcomes?: unknown } | null | undefined) {
  const price = num(p?.price);
  const outs = Array.isArray(p?.outcomes) ? (p!.outcomes as any[]) : [];
  return {
    price: price == null || price <= 0 ? "—" : `₹${price.toFixed(2)}`,
    t1: outcomeMark(p?.t1_success), t5: outcomeMark(p?.t5_success),
    outcomes: outs.length ? outs.map((o) => `${typeof o?.period === "string" && o.period ? o.period : "—"}: ${num(o?.return_pct) == null ? "—" : `${num(o?.return_pct)!.toFixed(2)}%`}`).join(" | ") : "—",
  };
}
