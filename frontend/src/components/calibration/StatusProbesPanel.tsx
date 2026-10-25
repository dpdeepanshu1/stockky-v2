// E144: the "Status probes" section of the Real Money Calibration page: the read-only status routes of the two trading services that had no screen (E142).
// Each card has one "Read now" button; nothing is fetched on a timer (some probes read the database). The JSON is shown as the service sent it.
// The catalog lives in statusProbes.ts; statusProbes.test.mjs checks each path is a real GET route.
import { useState } from "react";
import type { Svc } from "../../calibrationApi";
import { positionStocksApi } from "../../positionStocksApi";
import { realTradeApi } from "../../realTradeApi";
import { probeBadge, probesFor, probeText, type StatusProbe } from "../../statusProbes";
import { Btn, Card, Chip, Msg } from "./ui";

type Result = { at: string; value?: unknown; error?: string };

export default function StatusProbesPanel({ svc }: { svc: Svc }) {
  const [res, setRes] = useState<Record<string, Result>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const key = (p: StatusProbe) => `${p.svc}:${p.id}`;

  const read = async (p: StatusProbe) => {
    setBusy(key(p));
    const at = new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false });
    try {
      const value = p.svc === "rt" ? await realTradeApi.probe(p.path) : await positionStocksApi.probe(p.path);
      setRes((r) => ({ ...r, [key(p)]: { at, value } }));
    } catch (e: any) {
      setRes((r) => ({ ...r, [key(p)]: { at, error: e?.message || String(e) } }));
    } finally { setBusy(null); }
  };

  return (
    <div className="space-y-3" data-testid="status-probes">
      <Msg kind="info">Read-only. Each button reads one status route of {svc === "rt" ? "Real Trade" : "Position Stocks"} once, now. Nothing here changes a switch, a position or an order.</Msg>
      {probesFor(svc).map((p) => {
        const r = res[key(p)];
        const b = r && r.error == null ? probeBadge(r.value) : null;
        return (
          <Card key={p.id} title={p.title} tone={r?.error || b?.tone === "bad" ? "bad" : undefined}
            right={<>
              {b && <Chip tone={b.tone}>{b.text}</Chip>}
              {r && <Chip title="IST time of the last read">{r.at} IST</Chip>}
              <Btn small disabled={busy !== null} title={`GET ${p.path}`} onClick={() => void read(p)}>{busy === key(p) ? "Reading…" : r ? "Read again" : "Read now"}</Btn>
            </>}>
            <p className="text-[11px] text-mist">{p.what} <span className="opacity-70">GET {p.path}</span></p>
            {r?.error && <div className="mt-2"><Msg kind="err">{r.error}</Msg></div>}
            {r && r.error == null && (
              <pre className="mt-2 max-h-72 overflow-auto rounded-lg border border-slate p-2 text-[11px] leading-snug" data-testid={`probe-${p.id}`}>{probeText(r.value)}</pre>
            )}
          </Card>
        );
      })}
    </div>
  );
}
