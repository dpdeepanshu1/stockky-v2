// E539 (redesign round 6): the F&O "Engine status" row. Turns what the Overview page already polls (overview, pipeline, readiness) into a short list of
// pills. Pure: no imports, no network. Every pill carries words, so the colour is never the only signal; a missing answer is "closed" (grey), never green.
export type EnginePillState = "live" | "armed" | "paused" | "offline" | "closed" | "info";
export interface EnginePill { id: string; state: EnginePillState; text: string; title?: string }

export interface EngineIn {
  locks?: { engine_enabled?: boolean; mode?: string; real_enabled?: boolean; armed_today?: boolean } | null;
  market_open?: boolean; paused?: boolean;
  runner?: { alive?: boolean; error?: string | null } | null;
}
export interface PipelineIn { problems?: string[]; ok?: number; idle?: number }
export interface ReadinessIn { all_green?: boolean; counts?: { green?: number; red?: number; unknown?: number; hard_open?: number } }

export function enginePills(o: EngineIn | null | undefined, pipe?: PipelineIn | null, ready?: ReadinessIn | null): EnginePill[] {
  if (!o || !o.locks) return [];
  const l = o.locks;
  const out: EnginePill[] = [];
  out.push(l.engine_enabled ? { id: "engine", state: "live", text: "Engine on" } : { id: "engine", state: "offline", text: "Engine off", title: "The engine ships OFF: switch it on in the Control card" });
  const real = String(l.mode).toUpperCase() === "REAL";
  out.push({ id: "mode", state: "info", text: !real ? "Mode: DEMO" : l.real_enabled ? "Mode: REAL, orders allowed" : "Mode: REAL, orders locked", title: real ? (l.real_enabled ? "REAL orders are allowed" : "REAL mode, but REAL orders are still locked") : "Practice mode: no real order is sent" });
  out.push(l.armed_today ? { id: "armed", state: "armed", text: "Armed today" } : { id: "armed", state: "closed", text: "Not armed" });
  if (o.paused) out.push({ id: "paused", state: "paused", text: "Entries paused", title: "No new position until you press Resume entries" });
  out.push(o.market_open ? { id: "market", state: "live", text: "Market open" } : { id: "market", state: "closed", text: "Market closed" });
  out.push(o.runner?.alive ? { id: "runner", state: "live", text: "Runner alive" } : { id: "runner", state: "offline", text: "Runner stopped", title: o.runner?.error || undefined });
  if (pipe) {
    const p = pipe.problems?.length || 0;
    if (p > 0) out.push({ id: "pipeline", state: "offline", text: `Pipeline: ${p} problem${p === 1 ? "" : "s"}`, title: (pipe.problems || []).join("; ") });
    else if ((pipe.ok || 0) > 0) out.push({ id: "pipeline", state: "live", text: `Pipeline: ${pipe.ok} stage${pipe.ok === 1 ? "" : "s"} OK` });
    else out.push({ id: "pipeline", state: "closed", text: "Pipeline: idle", title: "Idle is neutral, not green" });
  }
  if (ready?.counts) {
    const c = ready.counts; const red = c.red || 0; const unk = c.unknown || 0; const hard = c.hard_open || 0;
    if (red > 0 || hard > 0) out.push({ id: "readiness", state: "paused", text: `Readiness: ${Math.max(red, hard)} not met`, title: "REAL stays closed while a hard item is not met" });
    else if (unk > 0) out.push({ id: "readiness", state: "info", text: `Readiness: ${unk} not shown yet` });
    else if (ready.all_green) out.push({ id: "readiness", state: "live", text: "Readiness: all met", title: "Evidence exists; this is not a safety guarantee" });
  }
  return out;
}
