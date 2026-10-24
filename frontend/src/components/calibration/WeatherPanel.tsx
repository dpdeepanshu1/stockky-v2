// Part 1 steps 5-6: the Market Weather card. Reads /weather/status on calibration-runner-service. In SHADOW (the default) the runner computes, logs and
// messages and sends NO dial. LIVE shows only when the runner's apply switch is on AND it has an applier; even then a dial goes out only through the
// three locks written on the card. The card never talks to a trading service itself. States are driven by weatherView() (weather.test.mjs).
import { useCallback, useEffect, useState } from "react";
import { runnerApi } from "../../calibrationApi";
import { DIAL_LABEL, DIAL_ORDER, applyLines, gateLine, lastWiringLine, STATE_PLAIN, STATE_TEXT, STATE_TONE, WEATHER_STAGES, hm, weatherView, type WeatherStage, type WeatherStatus } from "../../weatherView";
import { shortExposureLine } from "../../shortShadowView";
import { Btn, Card, Chip, Msg } from "./ui";

const istToday = () => new Date(Date.now() + 5.5 * 3600 * 1000).toISOString().slice(0, 10);
const STAGE_TEXT: Record<WeatherStage, string> = { morning: "08:00 morning", refine: "08:50 refine", lock: "09:14 lock", tick: "5-min tick" };

export default function WeatherPanel() {
  const [st, setSt] = useState<WeatherStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [info, setInfo] = useState<string | null>(null);
  const [wiring, setWiring] = useState<string>("");

  const load = useCallback(async () => {
    try { setSt(await runnerApi.weatherStatus()); setErr(null); } catch (e: any) { setErr(e?.message || String(e)); }
    try { setWiring(lastWiringLine((await runnerApi.runs(30, "wiring_check")).runs)); } catch { /* the line is optional */ }
  }, []);
  useEffect(() => { void load(); const id = setInterval(() => void load(), 30000); return () => clearInterval(id); }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); setErr(null); await load(); } catch (e: any) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };
  if (!st) return <div className="text-mist" data-testid="weather">{err ? <Msg kind="err">{err}</Msg> : "Loading Market Weather…"}</div>;

  const v = weatherView(st, istToday());
  const cur = st.current;
  const tone = v.kind === "active" && v.state ? STATE_TONE[v.state] : v.kind === "off" ? undefined : "warn";
  const run = (stage: WeatherStage) => {
    if (!window.confirm(`Run the ${STAGE_TEXT[stage]} stage now? It logs a row and sends its Telegram message. It changes no switch.`)) return;
    void act(async () => { const r = await runnerApi.weatherRun(stage); setInfo(r.ok ? `${stage}: ${r.state ?? "-"}${r.note ? ` (${r.note})` : ""}` : `${stage} failed: ${r.error ?? "no data"}`); });
  };

  return (
    <div className="space-y-3" data-testid="weather">
      {err && <Msg kind="err">{err}</Msg>}
      <Card
        title="Market Weather"
        tone={tone}
        right={<Btn small disabled={busy} onClick={() => void act(() => runnerApi.weatherSettings({ enabled: !st.enabled }))}>{st.enabled ? "Switch OFF" : "Switch ON"}</Btn>}
      >
        <div className="flex flex-wrap items-center gap-2">
          {st.mode === "live"
            ? <Chip tone="bad" title="Dials are sent only while the evidence gate passes AND the service itself has MARKET_STATE_MODE=live. Tighten-only, ends 15:30 IST.">LIVE · tighten-only</Chip>
            : <Chip tone="warn" title="The runner only logs and messages. It sends no dial to Real Trade or Position Stocks.">SHADOW · changes nothing</Chip>}
          <Chip tone={st.enabled ? "good" : "bad"}>{st.enabled ? "ON" : "OFF"}</Chip>
          <Chip title="classifier + profile version stamped on trades">{st.version}</Chip>
        </div>
        <p className="mt-2 text-[13px]" data-testid="weather-headline">{v.headline}</p>
        {v.failed && <div className="mt-2"><Msg kind="warn">The latest stage today did not get its data. The last good state is kept.</Msg></div>}
        {v.kind === "active" && v.state && cur && (
          <>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Chip tone={STATE_TONE[v.state]}>{STATE_TEXT[v.state].toUpperCase()}</Chip>
              {cur.score != null && <Chip title="blended score (higher = more stressed)">score {Math.round(cur.score * 100) / 100}</Chip>}
              {cur.gap_pct != null && <Chip>open gap {cur.gap_pct > 0 ? "+" : ""}{cur.gap_pct.toFixed(2)}%</Chip>}
              {cur.data_ok === false && <Chip tone="warn" title="history was thin or missing: the state fell back to Normal">thin data</Chip>}
            </div>
            <p className="mt-1 text-mist">{STATE_PLAIN[v.state]}</p>
            <p className="mt-1 text-[12px]" data-testid="weather-short-line">{shortExposureLine(v.state, st.profile.find((p) => p.state === v.state)?.dials)}</p>
            {(cur.reasons.length > 0 || cur.floor_reasons.length > 0) && (
              <ul className="mt-2 list-disc space-y-0.5 pl-4 text-[12px]" data-testid="weather-reasons">
                {cur.reasons.map((r, i) => <li key={`r${i}`}>{r}</li>)}
                {cur.floor_reasons.map((r, i) => <li key={`f${i}`} className="text-signal-hold">{r}</li>)}
              </ul>
            )}
          </>
        )}
      </Card>

      {v.kind === "active" && (
        <Card title={st.mode === "live" ? "Dials for this state (sent only if the gates allow)" : "Dials it would tighten (not applied)"}>
          {v.dials.length === 0
            ? <p className="text-mist">None. In this state the saved values stay as they are; trades are only stamped with the state.</p>
            : (
              <div className="space-y-1" data-testid="weather-dials">
                {v.dials.map((d) => (
                  <div key={d.key} className="flex items-center justify-between gap-2 rounded-lg border border-slate px-2 py-1">
                    <span>{DIAL_LABEL[d.key]}</span><b>{d.value}</b>
                  </div>
                ))}
              </div>
            )}
          <p className="mt-2 text-[11px] text-mist">These are the profile's asks. In live mode a service would still refuse any value looser than its saved one. Going live is a later step and needs the shadow report to pass.</p>
        </Card>
      )}

      <Card title="Today's stages">
        {v.today.length === 0 ? <p className="text-mist">No stage has run yet today.</p> : (
          <div className="space-y-1" data-testid="weather-today">
            {v.today.map((r, i) => (
              <div key={`${r.at}${i}`} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate px-2 py-1">
                <span className="tabular-nums text-mist">{hm(r.at)}</span>
                <span>{r.stage}</span>
                {r.ok && r.state ? <Chip tone={STATE_TONE[r.state]}>{STATE_TEXT[r.state]}</Chip> : <Chip tone="bad">NO DATA</Chip>}
                {r.changed && <Chip tone="warn">changed from {r.previous_state ? STATE_TEXT[r.previous_state] : "-"}</Chip>}
                {(r.note || r.error) && <span className="text-[11px] text-mist">{r.note || r.error}</span>}
              </div>
            ))}
          </div>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {WEATHER_STAGES.map((s) => <Btn key={s} small disabled={busy || !st.enabled} title={!st.enabled ? "switch Market Weather ON first" : "runs this stage now and sends its Telegram message"} onClick={() => run(s)}>Run {STAGE_TEXT[s]}</Btn>)}
        </div>
        {info && <div className="mt-2"><Msg kind="info">{info}</Msg></div>}
      </Card>

      <Card title="Live dials" tone={st.mode === "live" ? "bad" : undefined}>
        <div className="flex flex-wrap items-center gap-2">
          <Btn small disabled={busy} title="Off by default. Even when ON, nothing is sent unless the gate is open (a fresh USEFUL replay; the shadow report too in strict mode) and the service's own MARKET_STATE_MODE is live."
            onClick={() => { if (!st.apply && !window.confirm("Let Market Weather send tighten-only dials? It still sends nothing unless the gate is open (a fresh USEFUL history replay; strict mode also needs a PASS shadow report) and the service's MARKET_STATE_MODE is live. Dials end at 15:30 IST.")) return; void act(() => runnerApi.weatherSettings({ apply: !st.apply })); }}>
            Send dials: {st.apply ? "ON" : "off"}
          </Btn>
        </div>
        {gateLine(st.gate) && <div className={`mt-2 text-[12px] ${st.gate?.open ? "" : "text-mist"}`} data-testid="weather-gate">{gateLine(st.gate)}</div>}
        {wiring && <div className={`mt-2 text-[12px] ${/FAIL/.test(wiring) ? "text-signal-sell" : "text-mist"}`} data-testid="weather-wiring">{wiring}</div>}
        {applyLines(st.last_apply).length > 0 && (
          <div className="mt-2 space-y-0.5 text-[12px]" data-testid="weather-apply">{applyLines(st.last_apply).map((l, i) => <div key={i}>{l}</div>)}</div>
        )}
        <p className="mt-2 text-[11px] text-mist">Three locks: this switch, the evidence gate (WEATHER_GATE_MODE: a fresh USEFUL replay, plus a PASS shadow report in strict mode), and each trading service's own MARKET_STATE_MODE=live (set by you on the Switches page). The service refuses anything that is not tighter than its saved value. To stop at once: switch this off and set MARKET_STATE_MODE back to shadow, which clears today's overlay.</p>
      </Card>

      <Card title="Messages and trade stamp">
        <div className="flex flex-wrap items-center gap-2">
          <Btn small disabled={busy} onClick={() => void act(() => runnerApi.weatherSettings({ notify: !st.notify }))}>Telegram: {st.notify ? "ON" : "off"}</Btn>
          <Btn small disabled={busy} title="Posts a stamp-only overlay (no dials) so each trade records the market state it ran under"
            onClick={() => void act(() => runnerApi.weatherSettings({ stamp: !st.stamp }))}>Trade stamp: {st.stamp ? "ON" : "off"}</Btn>
        </div>
        <p className="mt-2 text-[11px] text-mist">The stamp only labels trades with the state. It sets no dial. It is off by default; a service that is down just misses its stamp.</p>
      </Card>

      <details className="rounded-2xl border border-slate bg-graphite p-3 text-[12px]">
        <summary className="cursor-pointer text-[11px] font-bold uppercase tracking-widest text-mist">All states and their dials</summary>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-left text-[11px]" data-testid="weather-table">
            <thead><tr><th className="pr-2 text-mist">Dial</th>{st.profile.map((p) => <th key={p.state} className="px-1 text-mist">{STATE_TEXT[p.state]}</th>)}</tr></thead>
            <tbody>
              {DIAL_ORDER.map((k) => (
                <tr key={k} className="border-t border-slate">
                  <td className="pr-2 py-0.5">{DIAL_LABEL[k]}</td>
                  {st.profile.map((p) => <td key={p.state} className={`px-1 ${v.state === p.state ? "font-bold" : ""}`}>{p.dials[k] ?? "·"}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-1 text-[11px] text-mist">· = the saved value is left alone. First-guess values; the replay and shadow weeks decide whether they stay.</p>
      </details>
    </div>
  );
}
