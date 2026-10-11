// group307/311: popup for one stock - rank / conviction / VWAP / zone / zone score / R:R header, ZoneChart, levels and the rule label.
// Opens in the existing BottomSheet (bottom sheet on a phone, centred dialog on desktop). Data: GET /board/{symbol}, refreshed every 5 s.
// Opened from a tile (full ranked row) or from the command palette (just a symbol: rank-type fields show "-", header from the symbol view).
import { useEffect, useState } from "react";
import BottomSheet from "../BottomSheet";
import { setVisibleInterval } from "../../visibleInterval";
import { positionStocksApi } from "../../positionStocksApi";
import ZoneChart from "./ZoneChart";
import { Stat } from "./boardCard";
import { zoneScoreLabel } from "./riskCap";
import type { BoardRowLite, SymbolView } from "./boardTypes";

const f2 = (v: number | null | undefined) => (v == null || !Number.isFinite(v) ? "-" : v.toFixed(2));
const num = (v: number | null | undefined) => (v != null && Number.isFinite(v) ? v : null);

export default function StockPopup({ row, onClose, reduced, extra }: { row: BoardRowLite | null; onClose: () => void; reduced: boolean; extra?: React.ReactNode }) {
  const [v, setV] = useState<SymbolView | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setV(null); setErr(null);
    if (!row) return;
    let alive = true;
    const load = async () => {
      try { const j = (await positionStocksApi.boardSymbol(row.symbol)) as SymbolView; if (alive) { setV(j); setErr(null); } }
      catch (e: any) { if (alive) setErr(e?.message || "failed"); }
    };
    void load();
    const stop = setVisibleInterval(load, 5000);
    return () => { alive = false; stop(); };
  }, [row?.symbol]);
  const side = row?.side ?? v?.side;
  const bull = side === "bull";
  const ltp = num(v?.ltp) ?? num(row?.ltp);
  const ref = num(v?.day?.prev_close) ?? num(v?.day?.open);
  const chg = num(row?.change_pct) ?? (ltp != null && ref ? ((ltp - ref) / ref) * 100 : null);
  const sector = v?.sector ?? row?.sector ?? "";
  const fno = v?.is_fno ?? row?.is_fno;
  const rr = num(row?.rr) ?? num(v?.rr?.rr);
  const chase = num(row?.chase_atr) ?? num(v?.chase_atr);
  const zs = zoneScoreLabel(num(row?.zone_score) ?? num(v?.zone_score));
  const zoneStatus = row?.zone_status ?? v?.zone?.status ?? null;
  const zoneValid = row?.zone_valid ?? v?.zone?.valid ?? null;
  const extended = row?.extended ?? v?.extended;
  const hasRank = row != null && num(row.percentile) != null;
  return (
    <BottomSheet isOpen={!!row} onClose={onClose} desktopMaxWidth="max-w-2xl"
      title={row ? <span>{row.symbol} <span className="text-[11px] text-mist">{sector}{fno ? " · F&O" : ""}</span></span> : ""}
      subtitle={row ? <span className="tabular-nums">₹{f2(ltp)} <span style={{ color: (chg ?? 0) >= 0 ? "var(--buy)" : "var(--sell)" }}>{chg == null ? "" : `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`}</span></span> : ""}>
      {row && (
        <div className="space-y-3 text-[11px]">
          <div className="grid grid-cols-3 sm:grid-cols-4 gap-2 tabular-nums">
            <Stat label="Rank" value={hasRank ? `p${num(row.percentile)!.toFixed(1)} · #${row.rank}` : "-"} />
            <Stat label="Conv" value={row.conviction != null ? String(row.conviction) : "-"} />
            <Stat label="VWAP" value={f2(num(v?.vwap) ?? num(row.vwap))} />
            <Stat label="Zone" value={zoneStatus ? `${zoneStatus}${zoneValid ? " ✓" : ""}` : "-"} />
            <Stat label="Zone score" value={zs.text} color={zs.tone === "good" ? "text-signal-buy" : zs.tone === "weak" ? "text-signal-sell" : undefined} sub="0-100 heuristic" />
            <Stat label="R:R" value={rr != null ? `1:${rr}` : "-"} />
            <Stat label="Chase" value={chase == null ? "-" : `${chase.toFixed(2)} ATR`} />
            <Stat label="Day open" value={f2(num(v?.day?.open) ?? num(row.open))} />
          </div>
          {row.rule ? <div className="font-display font-bold" style={{ color: extended ? "var(--amber)" : bull ? "var(--buy)" : "var(--sell)" }}>{row.rule}</div> : null}
          {err && !v && <div className="text-signal-hold">{err}</div>}
          {v ? <ZoneChart v={v} reduced={reduced} /> : !err && <div className="text-mist p-3">Loading chart…</div>}
          {v && (
            <div className="grid grid-cols-2 gap-2 text-mist tabular-nums">
              <div>{v.levels.resistances.map(l => <div key={l.label}>{l.label} <b className="text-paper">{l.price.toFixed(2)}</b> <span className="text-[9px]">{l.source}</span></div>)}</div>
              <div>{v.levels.supports.map(l => <div key={l.label}>{l.label} <b className="text-paper">{l.price.toFixed(2)}</b> <span className="text-[9px]">{l.source}</span></div>)}</div>
            </div>
          )}
          {row.or_low != null && row.or_high != null && <div className="text-mist">Opening range {row.or_low.toFixed(2)} – {row.or_high.toFixed(2)}</div>}
          {v?.backfilled && <div className="text-[10px] text-mist">Earlier candles were filled in after a restart; the day open is the first 1-minute candle's open when the exchange open was not available.</div>}
          {extra}
          <div className="text-[10px] text-mist">Zones and scores are hypotheses logged for a shadow study, not a proven edge or advice.</div>
        </div>
      )}
    </BottomSheet>
  );
}
