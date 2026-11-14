// Round 512: the Market research card. The "Stale trading thresholds" Telegram warning said "re-run market research" but nothing could do it; this is the button.
// It calls real-trade-service POST /adaptive/research (admin session): the service reviews the five regime constants against the index picture and the
// recent closed real trades, stamps the review date (which clears the stale warning), and sends ONE Telegram message. It changes no constant, switch or order.
// The same research also runs by itself every Saturday 09:00 IST (the service's own loop, and the runner's "Weekly market research" pack).
import { useCallback, useEffect, useState } from "react";
import { researchApi, type ResearchRecord, type ResearchState } from "../../calibrationApi";
import { Btn, Card, Chip, Msg } from "./ui";

const LABEL: Record<string, string> = {
  ENTRY_REGIME_MIN_SCORE: "Entry regime score (min)",
  ENTRY_MIN_REWARD_RISK: "Min reward : risk",
  CANDIDATE_MIN_CONVICTION: "Min conviction",
  CANDIDATE_DOWNTREND_6M_PCT: "6-month downtrend limit %",
  CANDIDATE_MIN_BULLISH_TF: "Min bullish timeframes",
};
const num = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? "-" : String(Math.round(v * 100) / 100));
const pct = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? "-" : `${v > 0 ? "+" : ""}${num(v)}%`);
const TRIGGER: Record<string, string> = { manual: "button", weekly: "weekly (automatic)", runner: "runner timetable" };

export default function MarketResearchPanel() {
  const [st, setSt] = useState<ResearchState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [info, setInfo] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setSt(await researchApi.latest()); setErr(null); } catch (e: any) { setErr(e?.message || String(e)); }
  }, []);
  useEffect(() => { void load(); const id = setInterval(() => void load(), 60000); return () => clearInterval(id); }, [load]);

  const run = async () => {
    if (!window.confirm("Run market research now? It reads the index history and your recent closed real trades, stamps today as the review date and sends one Telegram report. It changes no constant, switch or order.")) return;
    setBusy(true); setInfo(null);
    try {
      const r = await researchApi.run();
      setInfo(`Done: ${r.verdict ?? "ok"} (${r.at_ist} IST). Telegram report sent.`);
      setErr(null);
      await load();
    } catch (e: any) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };

  const rec: ResearchRecord | null = st?.last_ok ?? null;
  const lastFailed = st?.last && !st.last.ok ? st.last : null;
  const tone = lastFailed ? "bad" : rec?.verdict === "ATTENTION" ? "warn" : rec ? "good" : "warn";
  const pic = rec?.picture;

  return (
    <div className="space-y-3" data-testid="market-research">
      {err && <Msg kind="err">{err}</Msg>}
      {info && <Msg kind="ok">{info}</Msg>}
      <Card
        title="Market research"
        tone={tone}
        right={<Btn primary disabled={busy} onClick={() => void run()} title="Review the five regime constants now and send the Telegram report">{busy ? "Running…" : "Run market research now"}</Btn>}
      >
        <p className="text-mist">
          Reviews the five regime constants (entry score, reward : risk, conviction, 6-month downtrend, bullish timeframes) against the market picture and your recent
          real trades, and stamps the review date. That stamp is what clears the &quot;Stale trading thresholds&quot; Telegram warning. It changes no setting: the adaptive rules keep adjusting on their own.
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {rec ? <Chip tone={rec.verdict === "ATTENTION" ? "warn" : "good"}>{rec.verdict}</Chip> : <Chip tone="warn">never run</Chip>}
          {rec && <Chip title="the date the stale-threshold clock restarted from">reviewed {st?.reviewed_date ?? rec.reviewed_date}</Chip>}
          {rec && <Chip title="when the last good run happened">last run {rec.at_ist} IST · {TRIGGER[rec.trigger] ?? rec.trigger}</Chip>}
          {st && <Chip tone={st.weekly ? "good" : "bad"} title="MARKET_RESEARCH_WEEKLY in the real-trade-service environment">{st.weekly ? `weekly ON · next ${st.next_weekly} IST` : "weekly OFF"}</Chip>}
        </div>
        {lastFailed && (
          <div className="mt-2"><Msg kind="warn">The last attempt ({lastFailed.at_ist} IST, {TRIGGER[lastFailed.trigger] ?? lastFailed.trigger}) failed: {lastFailed.error}. No review date was stamped. Check the market-data service and press the button again.</Msg></div>
        )}
        {!rec && !lastFailed && <p className="mt-2 text-mist">No research has run yet. Press the button; the weekly run also does it by itself.</p>}
      </Card>

      {rec && (
        <Card title="Last research: the five constants">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12px]" data-testid="research-constants">
              <thead className="text-mist"><tr><th className="py-1 pr-3">Constant</th><th className="pr-3">Your value</th><th className="pr-3">Research suggests</th><th className="pr-3">Guard-rail band</th><th>Status</th></tr></thead>
              <tbody>
                {(rec.constants ?? []).map((r) => (
                  <tr key={r.name} className="border-t border-slate">
                    <td className="py-1 pr-3" title={r.name}>{LABEL[r.name] ?? r.name}</td>
                    <td className="pr-3">{num(r.static)}</td>
                    <td className="pr-3">{num(r.suggested)}{r.changed ? " *" : ""}</td>
                    <td className="pr-3">{num(r.band[0])} to {num(r.band[1])}</td>
                    <td>{r.in_band === false ? <Chip tone="bad">outside band</Chip> : <Chip tone="good">inside band</Chip>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-1 text-[11px] text-mist">* the adaptive rules {rec.adaptive_live ? "are already applying this shift every 15 minutes" : "are OFF (REGIME_ADAPTIVE_MODE=off), so this is only what they would set"}. The entry regime score also follows the 90-day history percentile.</p>
          {pic && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Chip title="-1 weak .. +1 strong, from 7 horizons of the index">tone {pic.tone > 0 ? "+" : ""}{num(pic.tone)}</Chip>
              <Chip title="0 calm .. 1 much louder than its own year">stress {num(pic.stress)}</Chip>
              <Chip>1m {pct(pic.returns?.["1m"])}</Chip>
              <Chip>3m {pct(pic.returns?.["3m"])}</Chip>
              <Chip>6m {pct(pic.returns?.["6m"])}</Chip>
              <Chip>1y {pct(pic.returns?.["1y"])}</Chip>
              {rec.feedback?.trades ? <Chip title="closed REAL trades read for the results feedback">{rec.feedback.trades} trades · win {num((rec.feedback.win_rate ?? 0) * 100)}%</Chip> : null}
            </div>
          )}
          {(rec.reasons ?? []).length > 0 && (
            <ul className="mt-2 list-disc space-y-0.5 pl-4 text-signal-hold" data-testid="research-reasons">
              {(rec.reasons ?? []).map((x, i) => <li key={i}>{x}</li>)}
            </ul>
          )}
        </Card>
      )}
    </div>
  );
}
