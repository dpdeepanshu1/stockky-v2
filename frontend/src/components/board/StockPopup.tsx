// group307: popup for one stock - rank / conviction / VWAP / zone / R:R header, ZoneChart, levels and the rule label. Opens in the
// existing BottomSheet (bottom sheet on a phone, centred dialog on desktop). Data: GET /board/{symbol}, refreshed every 5 s.
import { useEffect, useState } from "react";
import BottomSheet from "../BottomSheet";
import { setVisibleInterval } from "../../visibleInterval";
import { positionStocksApi } from "../../positionStocksApi";
import ZoneChart from "./ZoneChart";
import type { BoardRow, SymbolView } from "./boardTypes";

const f2 = (v: number | null | undefined) => (v == null ? "-" : v.toFixed(2));

export default function StockPopup({ row, onClose, reduced, extra }: { row: BoardRow | null; onClose: () => void; reduced: boolean; extra?: React.ReactNode }) {
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
  const bull = row?.side === "bull";
  return (
    <BottomSheet isOpen={!!row} onClose={onClose} desktopMaxWidth="max-w-2xl"
      title={row ? <span>{row.symbol} <span className="text-[11px] text-mist">{row.sector}{row.is_fno ? " · F&O" : ""}</span></span> : ""}
      subtitle={row ? <span className="tabular-nums">₹{f2(v?.ltp ?? row.ltp)} <span style={{ color: (row.change_pct ?? 0) >= 0 ? "var(--buy)" : "var(--sell)" }}>{row.change_pct == null ? "" : `${row.change_pct >= 0 ? "+" : ""}${row.change_pct.toFixed(2)}%`}</span></span> : ""}>
      {row && (
        <div className="space-y-3 text-[11px]">
          <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 tabular-nums">
            {([["RANK", `p${row.percentile.toFixed(1)} · #${row.rank}`], ["CONV", String(row.conviction)], ["VWAP", f2(v?.vwap ?? row.vwap)],
               ["ZONE", row.zone_status ? `${row.zone_status}${row.zone_valid ? " ✓" : ""}` : "-"], ["R:R", row.rr ? `1:${row.rr}` : (v?.rr ? `1:${v.rr.rr}` : "-")],
               ["CHASE", row.chase_atr == null ? "-" : `${row.chase_atr.toFixed(2)} ATR`]] as [string, string][]).map(([k, val]) => (
              <div key={k} className="rounded-lg border border-slate bg-ink px-2 py-1.5"><div className="text-[9px] text-mist">{k}</div><div className="font-bold text-paper">{val}</div></div>
            ))}
          </div>
          <div className="font-display font-bold" style={{ color: row.extended ? "var(--amber)" : bull ? "var(--buy)" : "var(--sell)" }}>{row.rule}</div>
          {err && !v && <div className="text-signal-hold">{err}</div>}
          {v ? <ZoneChart v={v} reduced={reduced} /> : !err && <div className="text-mist p-3">Loading chart…</div>}
          {v && (
            <div className="grid grid-cols-2 gap-2 text-mist tabular-nums">
              <div>{v.levels.resistances.map(l => <div key={l.label}>{l.label} <b className="text-paper">{l.price.toFixed(2)}</b> <span className="text-[9px]">{l.source}</span></div>)}</div>
              <div>{v.levels.supports.map(l => <div key={l.label}>{l.label} <b className="text-paper">{l.price.toFixed(2)}</b> <span className="text-[9px]">{l.source}</span></div>)}</div>
            </div>
          )}
          {row.or_low != null && row.or_high != null && <div className="text-mist">Opening range {row.or_low.toFixed(2)} – {row.or_high.toFixed(2)}</div>}
          {extra}
          <div className="text-[10px] text-mist">Zones and scores are hypotheses logged for a shadow study, not a proven edge or advice.</div>
        </div>
      )}
    </BottomSheet>
  );
}
