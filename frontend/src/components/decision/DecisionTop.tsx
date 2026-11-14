// E536 (redesign round 4): the top of the stock decision card - signal pill, price, conviction ring, and entry / stop / target / reward-to-risk as four Stats.
// Presentation only: the decision, the live price and every handler (Trade This, Call Alert, Watchlist) stay in DecisionCard and arrive as props.
import type { Decision } from "../../api";
import { ScoreRing } from "../ui/ScoreRing";
import { TradePlanBar } from "../ui/TradePlanBar";
import { Stat, Stats } from "../kit/Stat";
import { Button } from "../kit/Button";
import { HorizonStrip } from "./Bits";
import { isLongIdea, rupees, signalWord, signedPct, tradePlan } from "./planView";

export interface DecisionTopProps {
  data: Decision;
  displayClose: number;
  hasPrice: boolean;
  calling: boolean;
  isAddingWatchlist: boolean;
  onTrade: () => void;
  onCallAlert: () => void;
  onWatchlist: () => void;
}

export function DecisionTop({ data, displayClose, hasPrice, calling, isAddingWatchlist, onTrade, onCallAlert, onWatchlist }: DecisionTopProps) {
  const sig = signalWord(data.decision);
  const long = isLongIdea(data.decision);
  const plan = long
    ? tradePlan({ entryLow: data.entry_range?.low, entryHigh: data.entry_range?.high, stop: data.stop_loss, target: data.target, price: hasPrice ? displayClose : null })
    : null;
  const adj = data.market_sentiment_adjustment;
  const q = data.data_quality;

  return (
    <section className="dt-top k-card" data-testid="decision-top" aria-label={`${data.symbol} decision`}>
      <div className="dt-head">
        <div className="dt-id">
          <div className="dt-sym-row">
            <h2 className="dt-sym">{data.symbol}</h2>
            {data.sector && <span className="dt-meta">{data.sector}</span>}
            {data.valuation && <span className="dt-meta">· {data.valuation}</span>}
          </div>
          <div className="dt-signal-row">
            <span className={`dt-signal ${sig.tone}`} title={data.decision}>
              <span aria-hidden="true">{sig.glyph}</span> {sig.word}
            </span>
            <span className="dt-conf">{data.confidence} confidence</span>
          </div>
          {q && (
            <p className="dt-quality">
              Data quality: <b>{q.level || "unknown"}</b>
              {q.note ? ` · ${q.note}` : ""}
              {q.flags && q.flags.length > 0 ? ` · ${q.flags.slice(0, 2).join(" · ")}` : ""}
            </p>
          )}
        </div>

        <div className="dt-price">
          <div className="dt-price-num num">
            {hasPrice ? rupees(displayClose) : data.data_insufficient ? "Awaiting data" : "Syncing…"}
          </div>
          <div className="dt-price-sub">
            Combined {data.combined_score}/100
            {adj !== undefined && adj !== 0 && <span className={adj > 0 ? "up" : "down"}> ({adj > 0 ? `+${adj}` : adj} market)</span>}
          </div>
          <HorizonStrip data={data} />
        </div>

        <div className="dt-ring"><ScoreRing score={data.combined_score} /></div>
      </div>

      {data.event_risk && (
        <p className="dt-risk" role="alert">
          <span aria-hidden="true">⚠</span> {data.reasons.event?.[0] || "Upcoming corporate event — elevated near-term risk"}
        </p>
      )}

      {long && plan && (
        <>
          <Stats>
            <Stat label="Entry" size="sm" value={rupees(plan.entry)} sub={data.entry_range?.low != null && data.entry_range?.high != null ? `Range ${rupees(data.entry_range.low)} – ${rupees(data.entry_range.high)}` : "At the live price"} />
            <Stat label="Stop" size="sm" value={rupees(plan.stop)} tone="down" delta={signedPct(-plan.riskPct)} sub="Protective exit level" />
            <Stat label="Target" size="sm" value={rupees(plan.target)} tone="up" delta={signedPct(plan.rewardPct)} sub="Profit-taking level" />
            <Stat label="Reward : risk" size="sm" value={plan.rrText} tone={plan.rr >= 2 ? "up" : plan.rr >= 1 ? "flat" : "warn"} sub={plan.rr >= 2 ? "Reward at least twice the risk" : plan.rr >= 1 ? "Reward larger than the risk" : "Reward smaller than the risk"} />
          </Stats>
          <TradePlanBar stop={data.stop_loss} entryLow={data.entry_range?.low} entryHigh={data.entry_range?.high} target={data.target} price={hasPrice ? displayClose : null} />
        </>
      )}
      {long && !plan && (
        <p className="dt-note">No complete trade plan: the entry, stop and target numbers are missing or not in order, so none is shown.</p>
      )}

      <div className="dt-actions">
        {long && <Button variant="soft" size="sm" onClick={onTrade}>Trade this</Button>}
        {long && <Button variant="soft" size="sm" onClick={onCallAlert} loading={calling}>Call alert</Button>}
        <Button variant="ghost" size="sm" onClick={onWatchlist} loading={isAddingWatchlist}>+ Watchlist</Button>
      </div>
    </section>
  );
}
