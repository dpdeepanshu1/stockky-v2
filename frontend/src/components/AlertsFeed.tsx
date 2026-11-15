// E556 (round 539, B1): the Alerts feed - the last alerts the notification service sent, grouped by day, with "mark all read".
// Reads GET /notifications/history (admin login); a 401 says what to do. Read-only apart from the read marker: nothing here deletes or resends an alert.
import { useCallback, useEffect, useState } from "react";
import { api, type AlertHistory } from "../api";
import { Button, Card, CardHeader, Chip, Empty } from "./kit";
import { alertsSummary, groupAlertsByDay } from "./alertsFeed";

export default function AlertsFeed() {
  const [hist, setHist] = useState<AlertHistory | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [marking, setMarking] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    setErr(null);
    api.getNotificationHistory(100)
      .then(setHist)
      .catch((e) => setErr((e as Error).message))
      .finally(() => setBusy(false));
  }, []);
  useEffect(load, [load]);

  async function markRead() {
    setMarking(true);
    try {
      const r = await api.markAlertsRead();
      setHist((h) => (h ? { ...h, unread: 0, read_at: r.read_at } : h));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setMarking(false);
    }
  }

  const days = groupAlertsByDay(hist, Date.now());
  return (
    <Card data-testid="alerts-feed">
      <CardHeader
        title="Recent alerts"
        sub={err && !hist ? "Could not load the feed" : alertsSummary(hist)}
        action={
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={load} loading={busy}>Refresh</Button>
            <Button variant="soft" size="sm" onClick={markRead} loading={marking} disabled={!hist || hist.unread === 0}>Mark all read</Button>
          </div>
        }
      />
      {err && <p className="k-card-sub" role="alert">{err}</p>}
      {hist && days.length === 0 && !err && (
        <Empty title="No alerts yet" hint="Every message the app sends (picks, trades, agent ideas) is listed here once it goes out." />
      )}
      {days.map((d) => (
        <div key={d.key} className="alerts-day">
          <h4 className="k-card-sub" style={{ margin: "12px 0 4px" }}>{d.label}{d.unreadCount ? ` · ${d.unreadCount} new` : ""}</h4>
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {d.rows.map((r) => (
              <li key={r.id} data-testid="alert-row" style={{ padding: "8px 0", borderTop: "1px solid var(--k-line, rgba(128,128,128,.18))", fontWeight: r.unread ? 600 : 400 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
                  <span className="tabular-nums">{r.time}</span>
                  <span>{r.title}</span>
                  {r.label && <Chip as="span" tone={r.tone === "bad" ? "bad" : "warn"} symbol={r.symbol}>{r.label}</Chip>}
                  <Chip as="span">{r.channel}</Chip>
                </div>
                {r.preview && <p className="k-card-sub" style={{ margin: "2px 0 0" }}>{r.preview}</p>}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </Card>
  );
}
