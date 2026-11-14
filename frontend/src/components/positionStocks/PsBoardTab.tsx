// E538 (redesign round 5b): the "board" sub-tab of Position Stocks, moved out of PositionStocksTab.tsx (JSX unchanged).
// All state and handlers come from usePositionStocks() through `s`, so the ten sub-tabs still share one set of state.
import { positionStocksApi } from "../../positionStocksApi";
import MarketBoard from "../board/MarketBoard";
import BoardLegend from "../board/BoardLegend";   // E538: how to read the tiles
import JournalPanel from "../board/JournalPanel";
import ShortlistPanel from "../board/ShortlistPanel";
import TopPicks from "../board/TopPicks";
import SessionHealthPanel from "../board/SessionHealthPanel";
import PremarketPlanCard from "../session/PremarketPlanCard";
import type { PsState } from "./usePositionStocks";

export function PsBoardTab({ s }: { s: PsState }) {
  const { subTab } = s;
  return (
        <div className="space-y-4">
          <TopPicks active={subTab === "board"} fetcher={() => positionStocksApi.topPicks()}
            title="Top picks · best 3 to 5 for the scalp pool" note="Display only. The same ranking drives which stock the scalper tries first; every entry gate still decides. Board-preview cards have not been through those gates." />
          <ShortlistPanel active={subTab === "board"} kind="scalp" fetcher={() => positionStocksApi.shortlist()}
            title="Best picks · next entries (scalp pool)" note="Display only. The entry gates still decide; a pick that fails one is skipped and the next pick is tried." />
          <PremarketPlanCard active={subTab === "board"} fetcher={() => positionStocksApi.premarketPlan()} />
          <SessionHealthPanel active={subTab === "board"} />
          <MarketBoard active={subTab === "board"} title="Position Stocks · Market Board" legend={<BoardLegend />} />
          <JournalPanel active={subTab === "board"} />
        </div>
      );
}
