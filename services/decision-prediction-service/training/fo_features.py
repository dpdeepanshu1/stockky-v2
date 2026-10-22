"""fo_features.py - D24: F&O ban list and open-interest build-up as model features. Offline / training side only; nothing here trades.

  parse_secban(text, trade_date)        parse an NSE `fo_secban.csv` body ("1,SYMBOL" lines after a header) into rows (date, symbol)
  load_ban_history(dir)                 read files named `fo_secban_YYYYMMDD.csv` (or `.txt`) into one frame (date, symbol)
  add_ban_flags(panel, ban)             `in_fo_ban` (symbol is banned on that trading date) and `ban_prev` (banned on the previous session),
                                        `ban_days_20` (sessions banned in the 20 before this one, this one excluded)
  add_oi_features(panel)                needs `open_interest` per (symbol, date): `oi_chg_1d`, `oi_chg_5d` (fractions), `oi_buildup` and its one-hot flags:
                                          price up + OI up   = long_buildup      (+2)
                                          price up + OI down = short_covering    (+1)
                                          price down + OI down = long_unwinding  (-1)
                                          price down + OI up = short_buildup     (-2)      flat price or flat OI = 0
OI for a date is the end-of-day figure, so these features describe the row's own close: use them for a signal taken at that close (entry the next open),
as the triple-barrier labels do. The ban list for a date is public the evening before, so `in_fo_ban` is safe for an entry decided at that date's close.
Stocks outside F&O have no OI: their OI features stay NaN (never 0), so a model can tell "no derivatives" from "no change".
Pure pandas.
"""
from __future__ import annotations

import glob
import os
import re
from typing import List

import numpy as np
import pandas as pd

OI_FEATURE_COLUMNS = (
    "oi_chg_1d", "oi_chg_5d", "oi_buildup",
    "oi_long_buildup", "oi_short_covering", "oi_long_unwinding", "oi_short_buildup",
)
BAN_FEATURE_COLUMNS = ("in_fo_ban", "ban_prev", "ban_days_20")

_SYM = re.compile(r"^[A-Z0-9&\-]{1,20}$")


def parse_secban(text: str, trade_date) -> pd.DataFrame:
    """Lines look like `1,RBLBANK`; the header line and blanks are skipped. 'No Securities in Ban' gives an empty frame."""
    d = pd.Timestamp(trade_date).normalize()
    syms: List[str] = []
    for line in (text or "").splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and _SYM.match(parts[1].upper()):
            syms.append(parts[1].upper())
    return pd.DataFrame({"date": [d] * len(syms), "symbol": syms})


def load_ban_history(directory: str) -> pd.DataFrame:
    frames = []
    for path in sorted(glob.glob(os.path.join(directory, "fo_secban_*.csv")) + glob.glob(os.path.join(directory, "fo_secban_*.txt"))):
        m = re.search(r"(\d{8})", os.path.basename(path))
        if not m:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                frames.append(parse_secban(fh.read(), pd.to_datetime(m.group(1), format="%Y%m%d")))
        except OSError:
            continue
    if not frames:
        return pd.DataFrame(columns=["date", "symbol"])
    return pd.concat(frames, ignore_index=True).drop_duplicates()


def add_ban_flags(panel: pd.DataFrame, ban: pd.DataFrame) -> pd.DataFrame:
    df = panel.sort_values(["symbol", "date"]).reset_index(drop=True).copy()
    if ban is None or len(ban) == 0:
        df["in_fo_ban"] = 0
    else:
        key = pd.MultiIndex.from_frame(ban[["symbol", "date"]].drop_duplicates())
        df["in_fo_ban"] = pd.MultiIndex.from_frame(df[["symbol", "date"]]).isin(key).astype(int)
    g = df.groupby("symbol", sort=False)["in_fo_ban"]
    df["ban_prev"] = g.shift(1).fillna(0).astype(int)
    df["ban_days_20"] = g.transform(lambda s: s.shift(1).rolling(20, min_periods=1).sum()).fillna(0).astype(int)
    return df


def add_oi_features(panel: pd.DataFrame) -> pd.DataFrame:
    df = panel.sort_values(["symbol", "date"]).reset_index(drop=True).copy()
    if "open_interest" not in df.columns:
        for c in OI_FEATURE_COLUMNS:
            df[c] = np.nan
        return df
    oi = df["open_interest"].astype(float).where(lambda s: s > 0)
    g_oi = oi.groupby(df["symbol"], sort=False)
    g_px = df["close"].astype(float).groupby(df["symbol"], sort=False)
    df["oi_chg_1d"] = oi / g_oi.shift(1) - 1.0
    df["oi_chg_5d"] = oi / g_oi.shift(5) - 1.0
    px_chg = g_px.pct_change()
    up, dn = px_chg > 0, px_chg < 0
    oi_up, oi_dn = df["oi_chg_1d"] > 0, df["oi_chg_1d"] < 0
    code = pd.Series(np.nan, index=df.index)
    known = df["oi_chg_1d"].notna() & px_chg.notna()
    code[known] = 0.0
    code[known & up & oi_up] = 2.0
    code[known & up & oi_dn] = 1.0
    code[known & dn & oi_dn] = -1.0
    code[known & dn & oi_up] = -2.0
    df["oi_buildup"] = code
    for name, val in (("long_buildup", 2.0), ("short_covering", 1.0), ("long_unwinding", -1.0), ("short_buildup", -2.0)):
        df[f"oi_{name}"] = np.where(code.isna(), np.nan, (code == val).astype(float))
    return df
