// group336: today's session type (expiry / pre-holiday / Friday rules) and the health of each scan window (losing-window pause), plus the
// stop/target width advice. Read-only; the advice is never applied automatically.
import { useEffect, useState } from "react";
import { positionStocksApi } from "../../positionStocksApi";
import { Card } from "./boardCard";

export default function SessionHealthPanel({ active }: { active: boolean }) {
  const [cal, setCal] = useState<any>(null);
  const [win, setWin] = useState<any>(null);
  const [tune, setTune] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!active) return;
    let alive = true;
    (async () => {
      try { const c = await positionStocksApi.calendarToday(); if (alive) setCal(c); } catch (e: any) { if (alive) setErr(e?.message || "unavailable"); }
      try { const w = await positionStocksApi.windowsHealth(); if (alive) setWin(w); } catch { /* optional */ }
      try { const t = await positionStocksApi.tuningStopTarget(14); if (alive) setTune(t); } catch { /* optional */ }
    })();
    return () => { alive = false; };
  }, [active]);

  const tags: string[] = cal?.tags || [];
  const windows: Array<[string, any]> = win?.windows ? Object.entries(win.windows) : [];
  return (
    <Card title="Session rules and scan-window health">
      <div className="space-y-2 text-[11px]">
        {err && <div className="text-signal-hold">{err}</div>}
        {cal && (
          <div className="text-mist">
            Today: {tags.length ? <b className="text-signal-hold">{tags.map(t => t === "expiry" ? `${cal.expiry} expiry` : t === "pre_holiday" ? "session before a holiday" : "Friday").join(" + ")}</b> : <b className="text-paper">ordinary session</b>}
            {!cal.enabled && " (calendar rules switched off)"}
            {cal.size_mult < 1 && ` · position size x${Number(cal.size_mult).toFixed(2)}`}
            {cal.entry_blocked_now && <div className="text-signal-hold text-[10px]">{cal.entry_blocked_now}</div>}
          </div>
        )}
        {win && (
          <div>
            <div className="text-[10px] uppercase tracking-widest text-mist mb-1">Scan windows, last {win.days} days{win.enabled ? "" : " (auto-pause off)"}</div>
            {!windows.length && <div className="text-mist">No settled trades in this period.</div>}
            {windows.map(([w, v]) => (
              <div key={w} className="flex justify-between tabular-nums text-mist py-0.5">
                <span><b className="text-paper">{w}</b>{v.disabled && <span className="ml-1.5 text-signal-hold">paused</span>}</span>
                <span>{v.trades} trades · {v.win_rate_pct}% wins · ₹{Number(v.total_pnl).toFixed(0)}{v.trades < 8 ? " (small sample)" : ""}</span>
              </div>
            ))}
            {windows.some(([, v]) => v.disabled) && <div className="text-[9px] text-mist">A paused window returns by itself when its losing trades age out of the look-back.</div>}
          </div>
        )}
        {tune && (
          <div className="text-mist">
            <div className="text-[10px] uppercase tracking-widest mb-1">Stop / target advice (not applied)</div>
            {tune.note ? <div>{tune.note}</div> : (
              <div>Target used {tune.target_pct_used_avg}% · half the trades reached {tune.suggested_target_pct}% · stop used {tune.stop_pct_used_avg}% · stop that spares 80% of winners {tune.suggested_stop_pct}%. {tune.losers_that_got_halfway_to_target} of {tune.losers} losers got halfway to target.</div>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
