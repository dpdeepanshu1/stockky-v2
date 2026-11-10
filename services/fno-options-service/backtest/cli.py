"""backtest/cli.py - command-line entry for the three backtests (scripts/fno_*_backtest.py call main()). Saves <DATA_DIR>/backtests/<name>_<time>.json and prints the text report.

Data files (CSV with a header; the date column may be YYYY-MM-DD or 'YYYY-MM-DD HH:MM', IST): columns date, open, high, low, close, volume (volume optional).
  --daily    index daily candles          --intraday  index 5-minute candles          --vix  daily India VIX close (date, close)
  --bhav-dir a folder of NSE F&O bhavcopy CSVs (or .zip), one per day (what services/decision-prediction-service/training/fo_download.py saves)
  --snapshots <DATA_DIR>   the root that holds snapshots/<day>/<UNDERLYING>.jsonl
Examples (run from the repo root):
  python3 scripts/fno_sim_backtest.py --underlying NIFTY --daily nifty_d.csv --intraday nifty_5m.csv --vix vix.csv --capital 500000
  python3 scripts/fno_eod_backtest.py --daily nifty_d.csv --bhav-dir ./bhav
  python3 scripts/fno_replay_backtest.py --snapshots /data/fno --daily nifty_d.csv --underlyings NIFTY"""
from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import os
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fno.params import Params
from fno.provider import IST, label_to_epoch
from fno import strategies
from . import eod, portfolio, replay, report, rolling_sim, sim


def load_candles(path: str) -> List[Dict[str, float]]:
    out: List[Dict[str, float]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            t = label_to_epoch(r.get("date") or r.get("datetime") or r.get("timestamp") or "")
            try:
                o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            except (KeyError, ValueError):
                continue
            if t is not None and c > 0:
                out.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": float(r.get("volume") or 0)})
    return sorted(out, key=lambda x: x["t"])


def load_vix(path: str) -> Dict[str, float]:
    return {datetime.fromtimestamp(c["t"], IST).strftime("%Y-%m-%d"): c["c"] for c in load_candles(path)} if path else {}


def load_bhav_dir(folder: str, symbol: str = "NIFTY") -> Dict[date, List[Dict[str, Any]]]:
    by: Dict[date, List[Dict[str, Any]]] = {}
    for path in sorted(glob.glob(os.path.join(folder, "*"))):
        texts: List[str] = []
        if path.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                texts = [z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.lower().endswith(".csv")]
        elif path.lower().endswith(".csv"):
            texts = [open(path, encoding="utf-8", errors="ignore").read()]
        for t in texts:
            for row in eod.parse_bhavcopy(t, symbol):
                by.setdefault(row["day"], []).append(row)
    return by


def _params(a: argparse.Namespace) -> Params:
    p = Params(capital=a.capital)
    over = {}
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        over[k.strip()] = type(getattr(p, k.strip()))(v) if not isinstance(getattr(p, k.strip()), bool) else v.lower() in ("1", "true", "yes", "on")
    return p.with_(**over)


def _out(a: argparse.Namespace, name: str, res: Dict[str, Any]) -> int:
    root = a.out or os.getenv("FNO_DATA_DIR") or "."
    path = report.save(root, name, res)
    text = report.render_text(res)
    txt = path[:-5] + ".txt"
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    print(f"\nsaved: {path}\n       {txt}")
    return 0


def _default_holidays() -> str:
    """Round 276: the CLI runs used no holidays at all unless you typed them; default to the same official list the live engine uses."""
    try:
        import config
        return ",".join(config.HOLIDAYS)
    except Exception:  # noqa: BLE001
        return ""


def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--capital", type=float, default=500000.0)
    ap.add_argument("--out", default="", help="folder for the result files (default $FNO_DATA_DIR or .)")
    ap.add_argument("--set", action="append", help="override a Params field, e.g. --set sl_pct=20 --set min_score=40")
    ap.add_argument("--from", dest="start", default=None)
    ap.add_argument("--to", dest="end", default=None)
    ap.add_argument("--holidays", default=_default_holidays(), help="comma-separated YYYY-MM-DD (default: the official 2026 NSE list in config.py; pass \"\" for none)")


def main_sim(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="SIMULATED intraday F&O backtest (Black-Scholes premiums)")
    ap.add_argument("--underlying", default="NIFTY", choices=["NIFTY", "SENSEX"])
    ap.add_argument("--daily", required=True); ap.add_argument("--intraday", required=True); ap.add_argument("--vix", default="")
    ap.add_argument("--iv-default", type=float, default=0.14); ap.add_argument("--spread-pct", type=float, default=0.6); ap.add_argument("--expiry-weekday", type=int, default=None)
    ap.add_argument("--strategy", default="CORE", choices=list(strategies.ORDER) + ["ALL"], help="one strategy (default CORE = the original signal) or ALL = every strategy side by side")
    _common(ap)
    a = ap.parse_args(argv)
    daily, intraday, vix = load_candles(a.daily), load_candles(a.intraday), load_vix(a.vix)
    hol = [h for h in a.holidays.split(",") if h]
    if a.strategy == "ALL":
        res = portfolio.run(a.underlying, daily, intraday, vix, _params(a), iv_default=a.iv_default, spread_pct=a.spread_pct, holidays=hol, expiry_weekday=a.expiry_weekday, start=a.start, end=a.end)
        return _out(a, "fno_sim_" + a.underlying.lower() + "_portfolio", res)
    res = sim.run(a.underlying, daily, intraday, vix, _params(a), a.iv_default, a.spread_pct, hol, None, 5, a.expiry_weekday, a.start, a.end, strategy=a.strategy)
    return _out(a, "fno_sim_" + a.underlying.lower() + ("" if a.strategy == "CORE" else "_" + a.strategy.lower()), res)


def main_eod(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="EOD F&O backtest on real NSE bhavcopy option prices (NIFTY)")
    ap.add_argument("--daily", required=True); ap.add_argument("--bhav-dir", required=True); ap.add_argument("--lot", type=int, default=65); ap.add_argument("--expiry-weekday", type=int, default=None)
    ap.add_argument("--underlying", default="NIFTY", help="NIFTY only: the BSE Sensex bhavcopy layout is not read, so SENSEX is refused instead of returning an empty result that looks like 'no signal'")
    _common(ap)
    a = ap.parse_args(argv)
    if a.underlying.strip().upper() != "NIFTY":
        print(f"error: the EOD backtest reads the NSE bhavcopy (NIFTY) only; '{a.underlying}' is not supported. For SENSEX use the sim backtest (scripts/fno_sim_backtest.py --underlying SENSEX) "
              "or the replay of recorded chains.", file=sys.stderr)
        return 2
    res = eod.run(load_candles(a.daily), load_bhav_dir(a.bhav_dir), _params(a), "NIFTY", a.lot, [h for h in a.holidays.split(",") if h], a.expiry_weekday, a.start, a.end)
    return _out(a, "fno_eod_nifty", res)


def main_replay(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Replay recorded option-chain snapshots through the live engine")
    ap.add_argument("--snapshots", required=True); ap.add_argument("--underlyings", default="NIFTY,SENSEX")
    ap.add_argument("--daily", default="", help="NIFTY daily CSV"); ap.add_argument("--daily-sensex", default="")
    ap.add_argument("--vix-default", type=float, default=15.0)
    _common(ap)
    a = ap.parse_args(argv)
    from fno import recorder
    days = [d for d in recorder.list_days(a.snapshots) if (not a.start or d >= a.start) and (not a.end or d <= a.end)]
    over = {k: v for k, v in (("FNO_DEMO_CAPITAL", a.capital),)}
    p = _params(a)
    for field, cfg in (("min_score", "FNO_SIGNAL_MIN_SCORE"), ("sl_pct", "FNO_SL_PCT"), ("target_pct", "FNO_TARGET_PCT"), ("trail_arm_pct", "FNO_TRAIL_ARM_PCT"), ("trail_pct", "FNO_TRAIL_PCT"),
                         ("risk_per_trade_pct", "FNO_RISK_PER_TRADE_PCT"), ("max_lots", "FNO_MAX_LOTS"), ("vix_max", "FNO_VIX_MAX")):
        over[cfg] = getattr(p, field)
    und = [u for u in a.underlyings.upper().split(",") if u]
    daily = {"NIFTY": load_candles(a.daily) if a.daily else [], "SENSEX": load_candles(a.daily_sensex) if a.daily_sensex else []}
    res = replay.run(a.snapshots, days, daily, over, und, [h for h in a.holidays.split(",") if h], a.vix_default)
    return _out(a, "fno_replay", res)


def main_rolling(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Intraday F&O backtest on REAL premiums (Dhan expired-options candles fetched by scripts/fno_rolling_fetch.py)")
    ap.add_argument("--underlying", default="NIFTY", choices=["NIFTY", "SENSEX"])
    ap.add_argument("--daily", required=True); ap.add_argument("--intraday", required=True, help="index candles of the SAME interval as the rolling files"); ap.add_argument("--vix", default="")
    ap.add_argument("--rolling-dir", default="data/fno_rolling"); ap.add_argument("--interval", type=int, default=5); ap.add_argument("--expiry-code", type=int, default=1)
    ap.add_argument("--allow-partial", action="store_true", help="accept files with failed windows (holes): the result then says nothing about those days")
    ap.add_argument("--spread-pct", type=float, default=0.6); ap.add_argument("--expiry-weekday", type=int, default=None)
    ap.add_argument("--strategy", default="CORE", choices=list(strategies.ORDER))
    _common(ap)
    a = ap.parse_args(argv)
    try:
        book = rolling_sim.load_book(a.rolling_dir, a.underlying, a.interval, "WEEK", a.expiry_code, a.allow_partial)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    hol = [h for h in a.holidays.split(",") if h]
    res = rolling_sim.run(a.underlying, load_candles(a.daily), load_candles(a.intraday), book, load_vix(a.vix), _params(a), a.spread_pct, hol, None, a.expiry_weekday, a.start, a.end, a.strategy)
    return _out(a, "fno_rolling_" + a.underlying.lower() + ("" if a.strategy == "CORE" else "_" + a.strategy.lower()), res)
