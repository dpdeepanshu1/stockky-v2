// E537 (redesign round 5): the always-visible status header of the two trading pages - mode, armed, today's P&L, capital in use, idle capital and a pause button.
// Presentation only: every number comes from state the page already holds, and the pause button calls the handler the page already had.
import { Card } from "../kit/Card";
import { Stat, Stats } from "../kit/Stat";
import { StatusPill } from "../kit/StatusPill";
import { Button } from "../kit/Button";
import { headerView, type HeaderInput } from "./tradingGroups";

export function TradingHeader({ input, onPause, pauseLabel, pauseConfirm, busy }: {
  input: HeaderInput; onPause?: () => unknown; pauseLabel: string; pauseConfirm?: string; busy?: boolean;
}) {
  const v = headerView(input);
  return (
    <Card className={`tr-head ${v.real ? "real" : "demo"}`} aria-label="Trading status" data-testid="trading-header">
      <div className="tr-head-top">
        <StatusPill state={v.real ? "armed" : "info"}>{v.modeText}</StatusPill>
        {v.armedState && <StatusPill state={v.armedState}>{v.armedText}</StatusPill>}
        {onPause && (
          <span className="tr-head-pause">
            <Button variant="danger" size="sm" onClick={() => { void onPause(); }} disabled={!v.canPause} loading={busy} confirmText={pauseConfirm}>{pauseLabel}</Button>
          </span>
        )}
      </div>
      <Stats>
        <Stat label="Today's P&L" size="sm" value={v.pnlText ?? "—"} tone={v.pnlTone} />
        <Stat label="Capital in use" size="sm" value={v.inUseText ?? "—"} />
        <Stat label="Idle capital" size="sm" value={v.idleText ?? "—"} />
      </Stats>
    </Card>
  );
}
