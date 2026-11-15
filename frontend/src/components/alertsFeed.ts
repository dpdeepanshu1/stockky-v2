// E556 (round 539, B1): the Alerts feed - what the notification service sent, grouped by day (pure, no React: tested by plain node in alertsFeed.test.mjs).
// Days are Indian market days: the grouping uses a fixed +05:30 offset, never the browser's zone. Text is shown as the service stored it (already key-redacted).
export type AlertItem = { id: string; ts: number; title: string; message: string; channel: string; urgency: string; delivered: boolean; note?: string };
export type AlertHistory = { items: AlertItem[]; count: number; unread: number; read_at: number | null };
export type AlertRow = { id: string; time: string; title: string; preview: string; tone: "neutral" | "warn" | "bad"; symbol: string; label: string; channel: string; unread: boolean };
export type AlertDay = { key: string; label: string; unreadCount: number; rows: AlertRow[] };

const IST_MIN = 330;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad = (n: number) => String(n).padStart(2, "0");
const istParts = (ms: number) => { const d = new Date(ms + IST_MIN * 60000); return { y: d.getUTCFullYear(), m: d.getUTCMonth(), d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes() }; };
const dayKey = (ms: number) => { const p = istParts(ms); return `${p.y}-${pad(p.m + 1)}-${pad(p.d)}`; };
const dayNo = (ms: number) => Math.floor((ms + IST_MIN * 60000) / 86400000);

export function dayLabel(ms: number, nowMs: number): string {
  const diff = dayNo(nowMs) - dayNo(ms);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  const p = istParts(ms);
  return `${pad(p.d)} ${MONTHS[p.m]}${p.y !== istParts(nowMs).y ? " " + p.y : ""}`;
}

const URGENT = new Set(["high", "urgent", "critical"]);

export function alertRow(it: AlertItem, readAt: number | null): AlertRow {
  const p = istParts(it.ts * 1000);
  const failed = it.delivered === false;
  const urgent = URGENT.has((it.urgency || "").toLowerCase());
  const msg = (it.message || "").replace(/\s+/g, " ").trim();
  return {
    id: it.id,
    time: `${pad(p.h)}:${pad(p.mi)}`,
    title: (it.title || "").trim() || "(no title)",
    preview: msg.length > 140 ? msg.slice(0, 139) + "…" : msg,
    tone: failed ? "bad" : urgent ? "warn" : "neutral",
    symbol: failed ? "✕" : urgent ? "!" : "",
    label: failed ? "Not delivered" : urgent ? "Urgent" : "",
    channel: it.channel || "all",
    unread: it.ts > (readAt ?? 0),
  };
}

/** Newest first in, newest first out. Rows without a finite timestamp are dropped (a broken row must not break the feed). */
export function groupAlertsByDay(h: Pick<AlertHistory, "items" | "read_at"> | null | undefined, nowMs: number): AlertDay[] {
  const out: AlertDay[] = [];
  const items = (h?.items ?? []).filter((i) => i && Number.isFinite(i.ts)).sort((a, b) => b.ts - a.ts);
  for (const it of items) {
    const key = dayKey(it.ts * 1000);
    let day = out[out.length - 1];
    if (!day || day.key !== key) { day = { key, label: dayLabel(it.ts * 1000, nowMs), unreadCount: 0, rows: [] }; out.push(day); }
    const row = alertRow(it, h?.read_at ?? null);
    if (row.unread) day.unreadCount++;
    day.rows.push(row);
  }
  return out;
}

/** The line under the heading. */
export function alertsSummary(h: Pick<AlertHistory, "count" | "unread"> | null | undefined): string {
  if (!h || !h.count) return "Nothing sent yet";
  return `${h.count} kept (last 200)${h.unread ? ` · ${h.unread} new` : " · all read"}`;
}
