// frontend/src/components/ui/TradePlanBar.tsx  (design v5 - entry / stop / target at a glance; styles in design-v5.css)
// Presentation only: draws numbers the decision already carries. Renders nothing unless stop < entry < target (a long plan) and all are present.
type Props = { stop: number | null | undefined; entryLow: number | null | undefined; entryHigh: number | null | undefined; target: number | null | undefined; price: number | null | undefined };
const rs = (n: number) => `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
const ok = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n) && n > 0;

export function TradePlanBar({ stop, entryLow, entryHigh, target, price }: Props) {
  if (!ok(stop) || !ok(target) || !ok(entryLow) || !ok(entryHigh)) return null;
  const lo = Math.min(entryLow, entryHigh), hi = Math.max(entryLow, entryHigh), mid = (lo + hi) / 2;
  if (!(stop < lo && hi < target)) return null;
  const span = target - stop;
  const pos = (v: number) => Math.max(0, Math.min(100, ((v - stop) / span) * 100));
  const risk = mid - stop, reward = target - mid;
  const rr = risk > 0 ? reward / risk : null;
  const px = ok(price) ? price : null;
  const outside = px != null && (px < stop || px > target);
  return (
    <div className="v5-plan" role="group" aria-label="Trade plan: stop loss, entry zone and target">
      <div className="v5-plan-head">
        <span className="v5-eyebrow">TRADE PLAN</span>
        {rr != null && <span className="v5-plan-rr">Risk : reward <b className="num">1 : {rr.toFixed(1)}</b></span>}
      </div>
      <div className="v5-plan-rail">
        <span className="v5-plan-risk" style={{ left: 0, width: `${pos(lo)}%` }} />
        <span className="v5-plan-zone" style={{ left: `${pos(lo)}%`, width: `${Math.max(2, pos(hi) - pos(lo))}%` }} />
        <span className="v5-plan-gain" style={{ left: `${pos(hi)}%`, right: 0 }} />
        {px != null && <span className="v5-plan-now" style={{ left: `${pos(px)}%` }} title={`Now ${rs(px)}`}><i>{outside ? (px < stop ? "◀" : "▶") : "▼"}</i></span>}
      </div>
      <div className="v5-plan-legend">
        <span className="sell"><small>Stop loss</small><b className="num">{rs(stop)}</b></span>
        <span className="acc"><small>Buy zone</small><b className="num">{rs(lo)}{hi !== lo ? ` – ${rs(hi)}` : ""}</b></span>
        <span className="buy"><small>Target</small><b className="num">{rs(target)}</b></span>
      </div>
    </div>
  );
}
