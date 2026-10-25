// E159: summary tiles for the Service Manager sheet (pure, no React: tested by plain node in serviceView.test.mjs).
// Replaces four tiles that were fixed text ("Upstash Redis: Connected", "Supabase Postgres: Online", a cron window) and so could
// never show a problem. Every figure here comes from the last real health answer. A missing latency is a dash, never "<100ms".
export type ServiceKpi = { label: string; value: string; sub?: string; tone?: "up" | "down" | "warn" | "info" | "flat" };
type Svc = { ok?: boolean; required?: boolean; latency_ms?: number | null } | null | undefined;
export type ServiceInput = { services: Record<string, Svc>; loading: boolean; failed: boolean; checkedAt: number | null };

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const list = (names: string[]) => (names.length <= 3 ? names.join(", ") : `${names.slice(0, 3).join(", ")} +${names.length - 3} more`);

export function checkAge(ms: number): string {
  if (!Number.isFinite(ms) || ms < 5_000) return "just now";
  if (ms < 60_000) return `${Math.floor(ms / 1000)}s ago`;
  const m = Math.floor(ms / 60_000);
  return m < 60 ? `${m}m ago` : `${(ms / 3_600_000).toFixed(1)}h ago`;
}

export function serviceKpis(i: ServiceInput | null | undefined, nowMs: number): ServiceKpi[] {
  if (!i) return [];
  const entries = Object.entries(i.services || {});
  const none = entries.length === 0;
  const down = entries.filter(([, s]) => !s?.ok).map(([n]) => n);

  const services: ServiceKpi = none
    ? { label: "Services", value: "—", sub: i.loading ? "waiting for the first check" : i.failed ? "the health check did not answer" : "no services reported", tone: i.failed && !i.loading ? "warn" : "flat" }
    : down.length
      ? { label: "Services", value: `${entries.length - down.length}/${entries.length}`, sub: `Down: ${list(down)}`, tone: "warn" }
      : { label: "Services", value: `${entries.length}/${entries.length}`, sub: "all answering", tone: "info" };

  const req = entries.filter(([, s]) => s?.required === true);
  const reqDown = req.filter(([, s]) => !s?.ok).map(([n]) => n);
  const required: ServiceKpi = req.length
    ? { label: "Required", value: `${req.length - reqDown.length}/${req.length}`, sub: reqDown.length ? `Required and down: ${list(reqDown)}` : "every required service answering", tone: reqDown.length ? "warn" : "info" }
    : { label: "Required", value: "—", sub: none ? "no data yet" : "not reported", tone: "flat" };

  let slow: { n: string; ms: number } | null = null;
  for (const [n, s] of entries) { const ms = num(s?.latency_ms); if (ms != null && (slow == null || ms > slow.ms)) slow = { n, ms }; }
  const latency: ServiceKpi = slow
    ? { label: "Slowest", value: `${Math.round(slow.ms)}ms`, sub: slow.n, tone: "flat" }
    : { label: "Slowest", value: "—", sub: none ? "no data yet" : "no latency reported", tone: "flat" };

  const last: ServiceKpi = i.checkedAt == null
    ? { label: "Last check", value: "Never", sub: i.loading ? "checking now" : "no answer yet", tone: i.failed && !i.loading ? "warn" : "flat" }
    : { label: "Last check", value: checkAge(nowMs - i.checkedAt), sub: i.failed ? "latest check failed - showing the earlier answer" : "refreshes every 45s while visible", tone: i.failed ? "warn" : "flat" };

  return [services, required, latency, last];
}
