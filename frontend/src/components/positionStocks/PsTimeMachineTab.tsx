// E538 (redesign round 5b): the "timemachine" sub-tab of Position Stocks, moved out of PositionStocksTab.tsx (JSX unchanged).
// All state and handlers come from usePositionStocks() through `s`, so the ten sub-tabs still share one set of state.
import TimeMachinePanel from "../TimeMachinePanel";
import type { PsState } from "./usePositionStocks";

export function PsTimeMachineTab({ s }: { s: PsState }) {
  const { subTab } = s;
  return <TimeMachinePanel active={subTab === "timemachine"} />;
}
