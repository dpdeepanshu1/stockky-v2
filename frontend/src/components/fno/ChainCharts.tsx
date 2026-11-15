// round 552 (P3): two small charts under the option chain table: the change in open interest by strike (calls and puts, up = added, down = removed since the previous close)
// and the implied volatility by strike (the "smile"). Plain SVG from pure helpers (fnoChainView); no call, nothing stored. A missing value is a gap, never a zero.
import { ivPaths, ivPoints, oiBars, oiChangePoints, signedN0, type ChainRow } from "../../fnoChainView";

const W = 600, H = 150;

function labels(rows: ChainRow[]): { x: number; text: string }[] {
  const n = rows.length;
  if (n === 0) return [];
  const pad = 8, step = Math.max(1, Math.ceil(n / 6));
  return rows.flatMap((r, i) => (i % step === 0 ? [{ x: n <= 1 ? W / 2 : pad + ((W - 2 * pad) * i) / (n - 1), text: String(r.strike) }] : []));
}

export default function ChainCharts({ rows }: { rows: ChainRow[] }) {
  if (!rows.length) return null;
  const oi = oiBars(oiChangePoints(rows), W, H), iv = ivPaths(ivPoints(rows), W, H), lab = labels(rows);
  const noOi = oi.bars.length === 0, noIv = iv.lo == null;
  return (
    <div className="oc-charts">
      <figure className="oc-chart">
        <figcaption><b>Change in OI by strike</b> <span>since the previous close; up = OI added, down = removed</span></figcaption>
        {noOi ? <p className="text-mist">No change in OI reported.</p> : (
          <svg viewBox={`0 0 ${W} ${H + 14}`} role="img" aria-label={`Change in open interest by strike, largest move ${signedN0(oi.max)}`}>
            <line className="oc-zero" x1="0" x2={W} y1={oi.zeroY} y2={oi.zeroY} />
            {oi.bars.map((b) => <rect key={`${b.side}${b.strike}`} className={`oc-bar ${b.side}`} x={b.x} y={b.y} width={b.w} height={b.h}><title>{`${b.strike} ${b.side === "ce" ? "call" : "put"}: ${signedN0(b.v)}`}</title></rect>)}
            {lab.map((l) => <text key={l.text} className="oc-ax" x={l.x} y={H + 11} textAnchor="middle">{l.text}</text>)}
          </svg>
        )}
      </figure>
      <figure className="oc-chart">
        <figcaption><b>IV by strike</b> <span>{noIv ? "" : `${iv.lo!.toFixed(1)}% to ${iv.hi!.toFixed(1)}%, call and put`}</span></figcaption>
        {noIv ? <p className="text-mist">No implied volatility reported.</p> : (
          <svg viewBox={`0 0 ${W} ${H + 14}`} role="img" aria-label={`Implied volatility by strike from ${iv.lo!.toFixed(1)} to ${iv.hi!.toFixed(1)} percent`}>
            {iv.atmX != null && <line className="oc-zero atm" x1={iv.atmX} x2={iv.atmX} y1="0" y2={H} />}
            {iv.ce && <path className="oc-line ce" d={iv.ce} />}
            {iv.pe && <path className="oc-line pe" d={iv.pe} />}
            {lab.map((l) => <text key={l.text} className="oc-ax" x={l.x} y={H + 11} textAnchor="middle">{l.text}</text>)}
          </svg>
        )}
      </figure>
      <p className="oc-legend">
        <span><i className="oc-sw ce" aria-hidden="true" /> calls</span>
        <span><i className="oc-sw pe" aria-hidden="true" /> puts</span>
        <span>the ATM strike is marked on the IV chart; a gap means that strike has no value</span>
      </p>
    </div>
  );
}
