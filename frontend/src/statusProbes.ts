// frontend/src/statusProbes.ts  (E144)
// The read-only status probes of the two trading services that had no screen (E142). Pure (no runtime imports, no network, never throws). The panel
// fetches ONE probe when its button is pressed (never on a timer: some of these read the database or the broker) and shows the JSON as the service sent it.
// statusProbes.test.mjs checks every path below is a GET route that really exists in the service, and that none of them is a write.

export interface StatusProbe { svc: "rt" | "ps"; id: string; path: string; title: string; what: string }

export const STATUS_PROBES: StatusProbe[] = [
  { svc: "rt", id: "adaptive", path: "/adaptive/status", title: "Adaptive thresholds", what: "The regime threshold now in force and how stale its inputs are." },
  { svc: "rt", id: "preopen", path: "/preopen/status", title: "Pre-open snapshot", what: "Today's 09:08 pre-open passes: coverage and the biggest gaps." },
  { svc: "rt", id: "review-fixes", path: "/review-fixes/status", title: "20 Oct review fixes", what: "Effective settings of groups 470-472 (BROKER_STOP_ENABLED must be true) and today's stop-outs." },
  { svc: "rt", id: "range52w", path: "/range52w/status", title: "52-week range rule", what: "Live or log-only, how many candidates it would have rejected since boot." },
  { svc: "rt", id: "regime-rules", path: "/regime-rules/status", title: "Regime rules (log only)", what: "The rule table, counters since boot and the last verdicts." },
  { svc: "rt", id: "kronos", path: "/kronos/status", title: "Kronos gate", what: "Config, counters since boot and the last verdicts (off unless proven)." },
  { svc: "rt", id: "timesfm", path: "/timesfm/status", title: "TimesFM gate (Gate 4g)", what: "Config, counters since boot and the last verdicts (log only)." },
  { svc: "rt", id: "write-behind", path: "/write-behind/status", title: "Write-behind queue", what: "Queue depth, rows written or dropped and the back-off left for evidence rows." },
  { svc: "rt", id: "db-latency", path: "/ops/db-latency", title: "Database latency", what: "Connect and execute times, the last slow statement and the pool state." },
  { svc: "ps", id: "short-plain", path: "/short/plain", title: "Plain-order short fallback", what: "The switch, the day breaker, counters and SELLs under watch. Places nothing." },
  { svc: "ps", id: "short-probe", path: "/short/probe", title: "Last Dhan SELL probe", what: "The last probe on record (the probe itself is started from the Run tab with its confirm box)." },
  { svc: "ps", id: "board-status", path: "/board/status", title: "Market board status", what: "Board switches, bar recorder and parity counters." },
];

export const probesFor = (svc: "rt" | "ps"): StatusProbe[] => STATUS_PROBES.filter((p) => p.svc === svc);

const MAX_CHARS = 8000;

/** The JSON as indented text, cut at 8,000 characters with a note saying how much was cut. Never throws (a circular or odd value becomes its String()). */
export function probeText(v: unknown): string {
  let t: string;
  try { t = JSON.stringify(v, null, 2) ?? String(v); } catch { t = String(v); }
  return t.length <= MAX_CHARS ? t : `${t.slice(0, MAX_CHARS)}\n… (${t.length - MAX_CHARS} more characters not shown)`;
}

/** One line for the card header: "error" when the service answered {error: ...}, otherwise the number of top-level keys. */
export function probeBadge(v: unknown): { tone: "good" | "bad" | undefined; text: string } {
  if (v && typeof v === "object" && !Array.isArray(v)) {
    const o = v as Record<string, unknown>;
    if (typeof o.error === "string" && o.error) return { tone: "bad", text: `error: ${o.error.slice(0, 80)}` };
    return { tone: "good", text: `${Object.keys(o).length} fields` };
  }
  if (Array.isArray(v)) return { tone: "good", text: `${v.length} rows` };
  return { tone: undefined, text: v == null ? "empty answer" : typeof v };
}
