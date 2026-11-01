// frontend/src/components/session/PremarketPlanCard.tsx  (E277)
// The 08:30 pre-market plan at a glance: overseas risk-off flag, India VIX label, typed good / bad news counts and the last build time.
// Read-only (GET /premarket/plan on Position Stocks). Polls only while its tab is visible. All wording comes from premarketPlanView() (sessionView.test.mjs).
import { useEffect, useState } from "react";
import { setVisibleInterval } from "../../visibleInterval";
import { Card } from "../board/boardCard";
import { premarketPlanView, type PremarketPlanDoc } from "../../sessionView";

const TONE: Record<string, string> = { good: "text-signal-buy", bad: "text-signal-sell", warn: "text-signal-hold" };

export default function PremarketPlanCard({ active, fetcher }: { active: boolean; fetcher: () => Promise<PremarketPlanDoc> }) {
  const [doc, setDoc] = useState<PremarketPlanDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = async () => {
      try { const d = await fetcher(); if (alive) { setDoc(d); setErr(null); } }
      catch (e: any) { if (alive) setErr(e?.message || "pre-market plan unavailable"); }
    };
    void load();
    const stop = setVisibleInterval(load, 60000);
    return () => { alive = false; stop(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);
  const v = premarketPlanView(doc, new Date());
  return (
    <Card title="Pre-market plan" right={<span className="text-[10px] text-mist">08:30 to 09:20 IST</span>}>
      {err && <p className="text-[11px] text-signal-sell">{err}</p>}
      <p className={`text-[12px] ${v.tone ? TONE[v.tone] : "text-mist"}`} data-testid="plan-headline">{v.headline}</p>
      {v.tiles.length > 0 && (
        <ul className="v8-tiles mt-2" data-testid="plan-tiles">
          {v.tiles.map((t) => (
            <li key={t.label} title={t.hint}>
              <small>{t.label}</small>
              <b className={t.tone ? TONE[t.tone] : ""}>{t.value}</b>
            </li>
          ))}
        </ul>
      )}
      {v.notes.length > 0 && <ul className="mt-2 list-disc pl-4 text-[11px] text-mist">{v.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}
    </Card>
  );
}
