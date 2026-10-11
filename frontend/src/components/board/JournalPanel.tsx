// group310 (+309 display): P&L calendar heatmap, win rate by scan window, and the shadow-report summary. Read-only; all of it is
// derived from GET /trades/history and GET /board/shadow-report. Small samples are labelled as such, never dressed up.
import { useEffect, useMemo, useState } from "react";
import { positionStocksApi } from "../../positionStocksApi";
import { byDay, byTag, type JTrade } from "./journalStats";
import { Card } from "./boardCard";

export default function JournalPanel({ active }: { active: boolean }) {
  const [trades, setTrades] = useState<JTrade[]>([]);
  const [rep, setRep] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    (async () => {
      try { const h = await positionStocksApi.tradeHistory(500); if (alive) setTrades(h.trades as unknown as JTrade[]); } catch (e: any) { if (alive) setErr(e?.message || "history failed"); }
      try { const r = await positionStocksApi.boardShadowReport(30); if (alive) setRep(r); } catch { /* shadow report is optional */ }
    })();
    return () => { alive = false; };
  }, [active]);
  const days = useMemo(() => byDay(trades), [trades]);
  const tags = useMemo(() => byTag(trades), [trades]);
  const maxAbs = Math.max(1, ...days.map(d => Math.abs(d.pnl)));
  return (
    <div className="space-y-3 text-[11px]">
      <Card title="P&L by day (IST)">
        {err && <div className="text-signal-hold">{err}</div>}
        {!days.length ? <div className="text-mist">No closed trades with a final P&L yet.</div> : (
          <div className="flex flex-wrap gap-1">
            {days.map(d => (
              <div key={d.day} title={`${d.day}: ₹${d.pnl.toFixed(2)} · ${d.trades} trades · ${d.wins} wins`} className="w-8 h-8 rounded flex items-center justify-center text-[8px] tabular-nums text-paper"
                   style={{ background: d.pnl >= 0 ? "var(--buy)" : "var(--sell)", opacity: 0.25 + 0.75 * (Math.abs(d.pnl) / maxAbs) }}>{d.day.slice(8)}</div>
            ))}
          </div>
        )}
      </Card>
      <Card title="Win rate by scan window">
        {tags.map(t => (
          <div key={t.tag} className="flex justify-between tabular-nums text-mist py-0.5">
            <span>{t.tag}</span><span>{t.trades} trades · {t.win_rate == null ? "-" : `${Math.round(t.win_rate * 100)}%`} wins{t.trades < 30 ? " (small sample)" : ""} · ₹{t.pnl.toFixed(0)}</span>
          </div>
        ))}
        {!tags.length && <div className="text-mist">Nothing to show yet.</div>}
      </Card>
      {rep && (
        <Card title={`Board shadow study (${rep.days}d, gate mode: ${rep.gate_mode})`}>
          {Object.entries(rep.rules as Record<string, any>).map(([k, r]) => (
            <div key={k} className="py-0.5 text-mist"><b className="text-paper">{k}</b>: {r.verdict} <span className="text-[9px]">({r.about})</span></div>
          ))}
          <div className="text-[9px] text-mist mt-1">Needs {rep.min_obs} observations over {rep.min_sessions} sessions before any rule can be promoted. Research output, not advice.</div>
        </Card>
      )}
    </div>
  );
}
