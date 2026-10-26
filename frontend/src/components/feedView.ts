// E154: summary tiles for the Data Feed tab (pure, no React: tested by plain node in feedView.test.mjs).
// Everything is read from the job / meta / live-feed state the tab already holds. A count of exactly 0 stays 0;
// only an absent or non-numeric value is shown as missing ("—"). A bad or absent date is never shown as a time.
export type FeedKpi = { label: string; value: string; sub?: string; tone?: "up" | "down" | "warn" | "info" | "flat" };

type Job = { status?: string; message?: string; ok_count?: number; checkpoint?: { done?: string[] } };
type Meta = { last_success_at?: string | null; last_count?: number; universe_size?: number };
type Ws = { connected?: boolean; subscribed_count?: number; live_symbols_count?: number };
export type FeedInput = {
  job?: Job | null; meta?: Meta | null; wsFeed?: Ws | null;
  status: string; processed: number; total: number; pct: number; partial: boolean;
};

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const STALE_HOURS = 24; // the page text says real-time tasks reuse this cache for 12-24h

export function feedAge(ms: number): string {
  if (!Number.isFinite(ms) || ms < 60_000) return "just now";
  const m = Math.floor(ms / 60_000);
  if (m < 60) return `${m}m ago`;
  const h = ms / 3_600_000;
  if (h < 24) return `${h.toFixed(1)}h ago`;
  return `${(h / 24).toFixed(1)}d ago`;
}

export function feedKpis(i: FeedInput | null | undefined, nowMs: number): FeedKpi[] {
  if (!i) return [];
  const { job, meta, wsFeed, status, processed, total, pct, partial } = i;
  const stored = num(meta?.last_count) ?? num(job?.ok_count) ?? (Array.isArray(job?.checkpoint?.done) ? job!.checkpoint!.done!.length : null);
  const uni = num(meta?.universe_size);

  const raw = meta?.last_success_at;
  const t = raw ? new Date(raw).getTime() : NaN;
  let last: FeedKpi;
  if (!raw) last = { label: "Last success", value: "Never", sub: "no completed feed yet", tone: "warn" };
  else if (!Number.isFinite(t)) last = { label: "Last success", value: "—", sub: "date unreadable", tone: "warn" };
  else {
    const age = nowMs - t;
    const stale = age > STALE_HOURS * 3_600_000;
    last = {
      label: "Last success", value: feedAge(age),
      sub: stale ? `Stale - older than ${STALE_HOURS}h - run a feed` : new Date(t).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" }),
      tone: stale ? "warn" : "info",
    };
  }

  const bad = status === "error" || status === "stalled" || status === "stopped";
  const word = status ? status.charAt(0).toUpperCase() + status.slice(1) : "Idle";
  const msg = (job?.message || "").trim();
  const out: FeedKpi[] = [
    { label: "Stocks in feed", value: stored != null ? String(stored) : "—", sub: uni != null ? `of ${uni} in the universe` : "symbols stored", tone: "info" },
    last,
    { label: "Job", value: word, sub: msg ? (msg.length > 60 ? `${msg.slice(0, 57)}...` : msg) : "no message", tone: bad ? "warn" : status === "running" || status === "stopping" ? "info" : "flat" },
    {
      label: "Progress", value: total > 0 ? `${processed}/${total}` : "—",
      sub: total > 0 ? (partial && status !== "running" ? `${pct}% - partial, press Resume` : `${pct}%`) : "no run yet",
      tone: total > 0 && partial && status !== "running" ? "warn" : "flat",
    },
  ];
  if (wsFeed) {
    const sub = num(wsFeed.subscribed_count), live = num(wsFeed.live_symbols_count);
    out.push(wsFeed.connected
      ? { label: "Live quotes", value: live != null ? `${live} ticking` : "—", sub: sub != null ? `of ${sub} subscribed` : "subscribed count unknown", tone: "info" }
      : { label: "Live quotes", value: "Reconnecting", sub: "falling back to REST", tone: "warn" });
  }
  return out;
}

// E163: Price Alerts card (pure). A target that is not a positive number is refused before it is sent (Number("abc") is NaN and
// went to the backend as null); a row with a missing or non-numeric target shows a dash, never "₹undefined" / "₹NaN".
export type AlertTargetCheck = { ok: true; symbol: string; target: number } | { ok: false; reason: string };
export function checkAlertTarget(symbol: unknown, target: unknown): AlertTargetCheck {
  const sym = typeof symbol === "string" ? symbol.trim().toUpperCase() : "";
  if (!sym) return { ok: false, reason: "Enter a symbol." };
  const raw = typeof target === "string" ? target.replace(/[₹,\s]/g, "") : target;
  const n = typeof raw === "number" ? raw : raw === "" || raw == null ? NaN : Number(raw);
  if (!Number.isFinite(n) || n <= 0) return { ok: false, reason: "Target price must be a number above 0." };
  return { ok: true, symbol: sym, target: n };
}
export type AlertRowView = { symbol: string; glyph: "▲" | "▼" | ""; word: "above" | "below" | ""; target: string; fired: string | null };
export function alertRowView(a: { symbol?: unknown; direction?: unknown; target_price?: unknown; trigger_count?: unknown } | null | undefined): AlertRowView {
  const dir = a?.direction === "above" ? "above" : a?.direction === "below" ? "below" : "";
  const t = num(a?.target_price), f = num(a?.trigger_count);
  return {
    symbol: typeof a?.symbol === "string" && a.symbol ? a.symbol : "—",
    glyph: dir === "above" ? "▲" : dir === "below" ? "▼" : "",
    word: dir,
    target: t == null ? "—" : `₹${t}`,
    fired: f != null && f > 0 ? `fired ${f}×` : null,
  };
}
