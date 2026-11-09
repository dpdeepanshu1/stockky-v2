"""backtest/eod.py - end-of-day backtest on REAL option prices from the NSE F&O bhavcopy (NIFTY options; the BSE / SENSEX bhavcopy layout is not read yet).

Signal at the CLOSE of day D from the index's daily candles up to D (no look-ahead); the trade is taken on day D+1 at the option's OPEN (+ slippage) in the ATM strike of the weekly
expiry that is `min_days` away, and managed with that day's single OHLC bar of the option: the stop is assumed to hit first when the day's range touches both stop and target
(pessimistic), a gap through the stop fills at the open, anything still open is closed at the day's close (INTRADAY product: nothing is carried). Reliability: real prices, coarse path.

bhavcopy rows (UDiFF): TradDt, FinInstrmTp (IDO = index option), TckrSymb, FininstrmActlXpryDt, StrkPric, OptnTp, OpnPric, HghPric, LwPric, ClsPric, OpnIntrst, TtlTradgVol.
The old layout (INSTRUMENT OPTIDX, SYMBOL, EXPIRY_DT, STRIKE_PR, OPTION_TYP, OPEN, HIGH, LOW, CLOSE, OPEN_INT, TIMESTAMP) is read too. Option bars with zero volume are not tradable and are skipped."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fno import expiry as xp, gates, paper, signals, sizing
from fno.models import ContractRef
from fno.params import Params
from . import metrics

IST = timezone(timedelta(hours=5, minutes=30))


def _f(x: Any) -> Optional[float]:
    try:
        v = float(str(x).strip())
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _date(s: str) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_bhavcopy(text: str, symbol: str = "NIFTY") -> List[Dict[str, Any]]:
    """Index-option rows of ONE day's bhavcopy -> [{"day","expiry","strike","ot","o","h","l","c","vol","oi"}]. Bad rows are skipped."""
    out: List[Dict[str, Any]] = []
    for raw in csv.DictReader(io.StringIO(text or "")):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        inst = r.get("fininstrmtp") or r.get("instrument") or ""
        if inst not in ("IDO", "OPTIDX"):
            continue
        if (r.get("tckrsymb") or r.get("symbol") or "").upper() != symbol.upper():
            continue
        ot = (r.get("optntp") or r.get("option_typ") or "").upper()
        day = _date(r.get("traddt") or r.get("timestamp") or "")
        exp = _date(r.get("fininstrmactlxprydt") or r.get("xprydt") or r.get("expiry_dt") or "")
        k = _f(r.get("strkpric") or r.get("strike_pr"))
        o, h, l, c = (_f(r.get(a) or r.get(b)) for a, b in (("opnpric", "open"), ("hghpric", "high"), ("lwpric", "low"), ("clspric", "close")))
        if ot not in ("CE", "PE") or not (day and exp and k) or None in (o, h, l, c):
            continue
        out.append({"day": day, "expiry": exp, "strike": k, "ot": ot, "o": o, "h": h, "l": l, "c": c, "vol": _f(r.get("ttltradgvol") or r.get("contracts")) or 0.0,
                    "oi": _f(r.get("opnintrst") or r.get("open_int")) or 0.0})
    return out


def run(daily: Sequence[dict], bhav_by_day: Dict[date, List[Dict[str, Any]]], p: Optional[Params] = None, underlying: str = "NIFTY", lot_size: int = 65,
        holidays: Sequence[str] = (), expiry_weekday: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    p = p or Params()
    days = sorted(bhav_by_day)
    idx = {d: i for i, d in enumerate(days)}
    daily_sorted = sorted(daily, key=lambda c: c["t"])
    trades: List[paper.Position] = []
    first = last = None
    state = gates.DayState()
    run_days = 0
    for i, d in enumerate(days[:-1]):
        nd = days[i + 1]
        if (start and nd.isoformat() < start) or (end and nd.isoformat() > end):
            continue
        hist = [c for c in daily_sorted if datetime.fromtimestamp(c["t"], IST).date() <= d]
        if len(hist) < 30:
            continue
        run_days += 1
        first, last = first or nd.isoformat(), nd.isoformat()
        b = signals.index_bias(hist, [], None, p)
        if b["direction"] == signals.FLAT:
            continue
        ot = "CE" if b["direction"] == signals.UP else "PE"
        spot = hist[-1]["c"]
        exp = xp.expiry_for_entry(underlying, nd, p.min_days_to_expiry, p.block_expiry_day, holidays, expiry_weekday)
        rows = [r for r in bhav_by_day[nd] if r["expiry"] == exp and r["ot"] == ot and r["vol"] > 0]
        if not rows:
            continue
        row = min(rows, key=lambda r: abs(r["strike"] - spot))
        if row["o"] < p.min_premium:
            continue
        fill = paper.buy_fill(row["o"], row["o"], p)
        lots = sizing.lots_for(fill, lot_size, p) if fill else None      # round 250: size on the fill, as the live engine does
        if not lots or not fill:
            continue
        pos = paper.open_position(ContractRef(underlying, exp.isoformat(), row["strike"], ot, lot_size, None, "NSE_FNO", 0.05), lots, fill,
                                  datetime.combine(nd, datetime.min.time(), IST).timestamp() + 9.25 * 3600, p, mode="EOD", score=b["score"], spot=spot)
        hit = paper.evaluate_bar(pos, row["o"], row["h"], row["l"], row["c"], p, squareoff=False)
        reason, ref = hit if hit else (paper.SQUAREOFF, row["c"])
        out_fill = paper.sell_fill(ref, ref, p) or ref
        paper.close_position(pos, out_fill, datetime.combine(nd, datetime.min.time(), IST).timestamp() + 15.25 * 3600, reason, p)
        trades.append(pos)
    m = metrics.summarize(trades, p.capital)
    return {"kind": "EOD", "underlying": underlying, "from": first, "to": last, "days": run_days, "capital": p.capital, "params": p.to_json(), "metrics": m,
            "verdict": metrics.verdict(m), "trades": [t.to_json() for t in trades],
            "notes": ["one OHLC bar per option per day: when a day's range touches both stop and target, the stop is assumed first",
                      "entry is the option's day-open (+ slippage): a real fill at the open of a thin contract can be worse",
                      "NIFTY only (NSE bhavcopy); the BSE Sensex bhavcopy layout is not read yet"]}
