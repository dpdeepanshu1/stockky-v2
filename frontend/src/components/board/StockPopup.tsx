// group307/311/324: popup for one stock - hero (initials, price, change, day sparkline), rank / conviction / VWAP / zone / zone score / R:R
// key-value grid, rule badge, ZoneChart, levels. Opens in the existing BottomSheet (bottom sheet on a phone, centred dialog on desktop);
// the card inside is the board's dark terminal island. Host content (`extra`) and the footnotes stay outside it, in the app's own theme.
// Data: GET /board/{symbol}, refreshed every 5 s. Opened from a tile (full ranked row) or from the command palette (just a symbol:
// rank-type fields show "-", header from the symbol view).
import { useEffect, useState } from "react";
import "./board.css";
import BottomSheet from "../BottomSheet";
import { setVisibleInterval } from "../../visibleInterval";
import { positionStocksApi } from "../../positionStocksApi";
import ZoneChart from "./ZoneChart";
import { zoneScoreLabel } from "./riskCap";
import { convictionDots } from "./boardState";
import { computeMomentum, fmtPct } from "./momentum";
import type { BoardRowLite, SymbolView } from "./boardTypes";

type ChartMode = "candle" | "line";
const MODE_KEY = "stockky.board.chartMode";
const readMode = (): ChartMode => { try { return localStorage.getItem(MODE_KEY) === "line" ? "line" : "candle"; } catch { return "candle"; } };

const f2 = (v: number | null | undefined) => (v == null || !Number.isFinite(v) ? "-" : v.toFixed(2));
const num = (v: number | null | undefined) => (v != null && Number.isFinite(v) ? v : null);
const initials = (s: string) => s.replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase() || "?";

export default function StockPopup({ row, onClose, reduced, extra }: { row: BoardRowLite | null; onClose: () => void; reduced: boolean; extra?: React.ReactNode }) {
  const [v, setV] = useState<SymbolView | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setModeState] = useState<ChartMode>(readMode);
  const setMode = (m: ChartMode) => { setModeState(m); try { localStorage.setItem(MODE_KEY, m); } catch { /* storage blocked: the choice just is not remembered */ } };
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
  const dayAbs = ltp != null && ref ? ltp - ref : null;
  const sector = v?.sector ?? row?.sector ?? "";
  const fno = v?.is_fno ?? row?.is_fno;
  const rr = num(row?.rr) ?? num(v?.rr?.rr);
  const chase = num(row?.chase_atr) ?? num(v?.chase_atr);
  const zs = zoneScoreLabel(num(row?.zone_score) ?? num(v?.zone_score));
  const zoneStatus = row?.zone_status ?? v?.zone?.status ?? null;
  const zoneValid = row?.zone_valid ?? v?.zone?.valid ?? null;
  const extended = row?.extended ?? v?.extended;
  const hasRank = row != null && num(row.percentile) != null;
  const conv = num(row?.conviction);
  const upDown = (chg ?? 0) >= 0 ? "var(--ob-g)" : "var(--ob-r)";
  const sideCol = extended ? "var(--ob-amber)" : bull ? "var(--ob-g)" : "var(--ob-r)";
  const spark = (v?.candles ?? []).slice(-60).map(k => k[4]);
  let sparkPts = "";
  if (spark.length > 2) {
    const lo = Math.min(...spark), hi = Math.max(...spark), span = hi - lo || 1;
    sparkPts = spark.map((p, i) => `${((i / (spark.length - 1)) * 86).toFixed(1)},${(2 + (1 - (p - lo) / span) * 24).toFixed(1)}`).join(" ");
  }
  const mom = v ? computeMomentum(v.candles, ltp, num(v.vwap) ?? num(row?.vwap), v.tf_min) : null;
  const ob = v?.zone ?`${f2(v.zone.bottom)}-${f2(v.zone.top)}` : "-";
  const sr = v ? `${f2(v.levels.supports[0]?.price)} / ${f2(v.levels.resistances[0]?.price)}` : "-";
  return (
    <BottomSheet isOpen={!!row} onClose={onClose} desktopMaxWidth="max-w-2xl"
      title={row ? <span>{row.symbol} <span className="text-[11px] text-mist">{sector}{fno ? " · F&O" : ""}</span></span> : ""}
      subtitle={row ? <span className="tabular-nums">₹{f2(ltp)} <span style={{ color: (chg ?? 0) >= 0 ? "var(--buy)" : "var(--sell)" }}>{chg == null ? "" : `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`}</span></span> : ""}>
      {row && (
        <div className="space-y-3 text-[11px]">
          <div className="ob ob-card" data-testid="stock-popup-card">
            <div className="ob-hero">
              <span className={`ob-avatar ${bull ? "g" : "r"}`}>{initials(row.symbol)}</span>
              <div className="ob-hero-id"><b>{row.symbol}</b><span>{sector || "-"}{fno ? " · F&O" : ""}</span></div>
              {sparkPts && <svg width={86} height={28} viewBox="0 0 86 28" aria-hidden="true" style={{ marginLeft: "auto" }}><polyline points={sparkPts} fill="none" stroke={upDown} strokeWidth={1.4} strokeLinejoin="round" /></svg>}
              <div className="ob-hero-px" style={sparkPts ? undefined : { marginLeft: "auto" }}>
                <span className="ob-px">₹{f2(ltp)}</span>
                <span style={{ color: upDown, fontSize: 11, fontWeight: 700 }}>{chg == null ? "" : `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`}{dayAbs == null ? "" : `  ${dayAbs >= 0 ? "+" : ""}${dayAbs.toFixed(2)}`}</span>
              </div>
            </div>
            <div className="ob-kv">
              <div><span>Rank</span><b>{hasRank ? `p${num(row.percentile)!.toFixed(1)} · #${row.rank}` : "-"}</b></div>
              <div><span>Conv</span><b>{conv != null ? conv : "-"}<span className={`ob-dotsrow ${bull ? "g" : ""}`} aria-hidden="true">{[0, 1, 2, 3, 4].map(i => <i key={i} className={i < convictionDots(conv) ? "on" : ""} />)}</span></b></div>
              <div><span>VWAP</span><b>{f2(num(v?.vwap) ?? num(row.vwap))}</b></div>
              <div><span>OB</span><b>{ob}</b></div>
              <div><span>Zone</span><b>{zoneStatus ? `${zoneStatus}${zoneValid ? " ✓" : ""}` : "-"}</b></div>
              <div><span>S1 / R1</span><b>{sr}</b></div>
              <div><span>Zone score</span><b style={{ color: zs.tone === "good" ? "var(--ob-g)" : zs.tone === "weak" ? "var(--ob-r)" : undefined }}>{zs.text}</b></div>
              <div><span>R:R</span><b>{rr != null ? `1:${rr}` : "-"}</b></div>
              <div><span>Chase</span><b>{chase == null ? "-" : `${chase.toFixed(2)} ATR`}</b></div>
              <div><span>Day open</span><b>{f2(num(v?.day?.open) ?? num(row.open))}</b></div>
            </div>
            {row.rule ? <span className="ob-rule" style={{ color: sideCol, borderColor: sideCol, background: "rgba(255,255,255,.03)" }}>{row.rule}</span> : null}
            {err && !v && <div className="ob-note" style={{ color: "var(--ob-amber)" }}>{err}</div>}
            {mom && (
              <div className={`ob-mom ${mom.tone}`} data-testid="stock-momentum">
                <div className="ob-mom-head"><span>Live momentum</span><b>{mom.label}{mom.score != null ? ` · ${mom.score > 0 ? "+" : ""}${mom.score}` : ""}</b></div>
                {mom.score != null && (
                  <div className="ob-mom-bar" aria-hidden="true"><i style={{ left: `${(50 + mom.score / 2).toFixed(1)}%` }} /></div>
                )}
                <div className="ob-mom-kv">
                  <div><span>Last {mom.shortBars} bars</span><b>{fmtPct(mom.shortPct)}</b></div>
                  <div><span>Last {mom.longBars} bars</span><b>{fmtPct(mom.longPct)}</b></div>
                  <div><span>vs VWAP</span><b>{fmtPct(mom.vwapPct)}</b></div>
                  <div><span>Green of 5</span><b>{mom.greenOf5 == null ? "-" : `${mom.greenOf5}/5`}</b></div>
                  <div><span>Day range</span><b>{mom.dayPos == null ? "-" : `${Math.round(mom.dayPos * 100)}%`}</b></div>
                  <div><span>Volume</span><b>{mom.volRatio == null ? "-" : `${mom.volRatio.toFixed(1)}x`}</b></div>
                </div>
                <div className="ob-note">{mom.tfMin}-minute bars, refreshed every 5 s{bull ? "" : " · this stock is on the bearish side of the board; the engines only buy long"}.</div>
              </div>
            )}
            {v && (
              <div className="ob-modes" role="group" aria-label="Chart type">
                <button type="button" className="ob-chip" aria-pressed={mode === "candle"} onClick={() => setMode("candle")}>Candles</button>
                <button type="button" className="ob-chip" aria-pressed={mode === "line"} onClick={() => setMode("line")}>Line</button>
              </div>
            )}
            {v ? <div className="ob-chart"><ZoneChart v={v} reduced={reduced} mode={mode} /></div> : !err && <div className="ob-note" style={{ padding: 12 }}>Loading chart…</div>}
            {v && (
              <div className="ob-levels">
                <div>{v.levels.resistances.map(l => <div key={l.label}>{l.label} <b>{l.price.toFixed(2)}</b> <span style={{ fontSize: 9 }}>{l.source}</span></div>)}</div>
                <div>{v.levels.supports.map(l => <div key={l.label}>{l.label} <b>{l.price.toFixed(2)}</b> <span style={{ fontSize: 9 }}>{l.source}</span></div>)}</div>
              </div>
            )}
            {row.or_low != null && row.or_high != null && <div className="ob-note">Opening range {row.or_low.toFixed(2)} – {row.or_high.toFixed(2)}</div>}
            {v?.backfilled && <div className="ob-note">Earlier candles were filled in after a restart; the day open is the first 1-minute candle's open when the exchange open was not available.</div>}
          </div>
          {extra}
          <div className="text-[10px] text-mist">Zones and scores are hypotheses, not a proven edge or advice.</div>
        </div>
      )}
    </BottomSheet>
  );
}
