// "F&O Options" page: Nifty / Sensex index options (buy-only CE / PE). DEMO by default, REAL locked. Backend: fno-options-service (nginx /fno-api/).
import { useState } from "react";
import { PageHero } from "../ui/PageHero";
import { SubTabs } from "../ui/SubTabs";
import FnoOverview from "./FnoOverview";
import FnoChain from "./FnoChain";
import FnoBacktests from "./FnoBacktests";
import { FnoHistory, FnoLog, FnoPositions } from "./FnoJournal";

type Section = "overview" | "chain" | "positions" | "history" | "backtest" | "log";
const SECTIONS: { id: Section; label: string }[] = [
  { id: "overview", label: "Overview" }, { id: "chain", label: "Option chain" }, { id: "positions", label: "Positions" },
  { id: "history", label: "History & charges" }, { id: "backtest", label: "Backtests" }, { id: "log", label: "Decision log" },
];

export default function FnoTab() {
  const [s, setS] = useState<Section>("overview");
  return (
    <div className="v6-page v6-flow space-y-3">
      <PageHero icon="trend" title="F&O Options" kicker="Nifty · Sensex · buy CE / PE" subtitle="Option buying on the two indices. Starts OFF and in DEMO: nothing real happens until you switch it on in Real Money Calibration." />
      <SubTabs items={SECTIONS} value={s} onChange={setS} label="Section" />
      {s === "overview" && <FnoOverview />}
      {s === "chain" && <FnoChain />}
      {s === "positions" && <FnoPositions />}
      {s === "history" && <FnoHistory />}
      {s === "backtest" && <FnoBacktests />}
      {s === "log" && <FnoLog />}
    </div>
  );
}
