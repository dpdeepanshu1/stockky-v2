// Real Money Calibration > F&O: switches of fno-options-service, its trades, its backtests, and the written run plan. The runner-based tests of the
// two equity services do not apply here; the F&O backtests run inside the service (or as scripts, see the plan).
import { useState } from "react";
import { SubTabs } from "../ui/SubTabs";
import SwitchesPanel from "./SwitchesPanel";
import FnoBacktests from "../fno/FnoBacktests";
import { FnoHistory, FnoLog } from "../fno/FnoJournal";
import { Card } from "./ui";
import { getFnoApiUrl, setFnoApiUrl } from "../../fnoApi";

type Section = "switches" | "trades" | "backtests" | "plan" | "log";
const SECTIONS: { id: Section; label: string }[] = [
  { id: "switches", label: "Switches" }, { id: "trades", label: "Trades" }, { id: "backtests", label: "Backtests" }, { id: "plan", label: "Run plan" }, { id: "log", label: "Decision log" },
];

const PLAN: { when: string; what: string }[] = [
  { when: "Day 0 (once)", what: "Deploy the service. Check Overview > Pipeline check: market-data reachable, option contracts loaded, expiries and India VIX present. Compare the lot sizes with the exchange circular." },
  { when: "Day 0 (once)", what: "Place one real trade by hand in Dhan and compare its contract note with F&O Options > History > Charges. Correct FNO_* charge rates if they differ." },
  { when: "Week 1-2", what: "Switch FNO_ENGINE_ENABLED ON (mode stays DEMO). Leave FNO_RECORD_SNAPSHOTS ON so chains are recorded every minute. Do not change other switches." },
  { when: "Every trading day", what: "After 15:30: read the Decision log and the day's trades. A skipped trade states why (gate, score, spread). Do not tune after one day." },
  { when: "Weekly", what: "Run the Replay backtest on the recorded days and the end-of-day script (scripts/fno_eod_backtest.py). Compare the three: a SIMULATED result alone proves nothing." },
  { when: "After 4+ weeks and 60+ DEMO trades", what: "Only if replay and DEMO agree, positive after charges, with a drawdown you can live with: consider a first REAL week with 1 lot, ARM each morning by hand. The verdict never says 'safe'." },
  { when: "Never", what: "Do not raise FNO_MAX_LOTS or the risk % to recover a loss. Do not trade REAL on an expiry day. Static-IP whitelisting on Dhan is required for API orders." },
];

export default function FnoCalibration() {
  const [s, setS] = useState<Section>("switches");
  const [url, setUrl] = useState(getFnoApiUrl());
  return (
    <div className="space-y-3">
      <SubTabs items={SECTIONS} value={s} onChange={setS} label="F&O section" />
      {s === "switches" && <SwitchesPanel svc="fno" />}
      {s === "trades" && <FnoHistory />}
      {s === "backtests" && <FnoBacktests />}
      {s === "log" && <FnoLog />}
      {s === "plan" && (
        <Card title="F&O run plan">
          <ol className="list-decimal space-y-2 pl-4">{PLAN.map((p, i) => <li key={i}><b>{p.when}:</b> {p.what}</li>)}</ol>
        </Card>
      )}
      <details className="rounded-2xl border border-slate bg-graphite p-3 text-[12px]">
        <summary className="cursor-pointer text-[11px] font-bold uppercase tracking-widest text-mist">F&amp;O connection</summary>
        <label className="mt-2 block text-[11px]"><span className="block text-mist">F&amp;O URL (fno-options-service)</span>
          <input value={url} onChange={(e) => setUrl(e.target.value)} onBlur={() => setFnoApiUrl(url)} className="w-full rounded-lg border border-slate bg-transparent px-2 py-1 text-[12px] sm:w-72" /></label>
      </details>
    </div>
  );
}
