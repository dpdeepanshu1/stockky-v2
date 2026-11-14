// F&O Overview > Engine status (E539, redesign round 6): one row of status pills that replaces the four-line lock list and the Market / runner chips.
// It only reads what the page already polls (overview, pipeline stage summary, readiness): no new call. The cards below keep every control.
import type { FnoOverview, FnoPipeline, FnoReadiness } from "../../fnoApi";
import { enginePills } from "../../fnoEngineView";
import { StatusPill } from "../kit";

export default function EngineStatusRow({ o, pipe, ready }: { o: FnoOverview | null | undefined; pipe?: FnoPipeline["stage_summary"]; ready?: FnoReadiness | null }) {
  const pills = enginePills(o, pipe, ready);
  if (!pills.length) return null;
  return (
    <ul className="oc-engine" aria-label="Engine status">
      {pills.map((p) => <li key={p.id} title={p.title}><StatusPill state={p.state}>{p.text}</StatusPill></li>)}
    </ul>
  );
}
