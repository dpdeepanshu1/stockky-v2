"""pit_universe.py - D23: a training universe that does not forget the stocks that failed.

Training on "the symbols that exist today" silently removes every company that was delisted, suspended or merged away, so the model only ever sees
survivors and looks better than it will live. This builds the universe from the bhavcopy archive instead (bhavcopy_archive.py writes
`SYMBOL_YYYY_MM.parquet`, or `.csv.gz` when pyarrow is missing; any long frame with date, symbol, close, volume works):

  load_archive(dir)                      read every monthly file (.parquet needs pyarrow; .csv and .csv.gz also accepted). The date comes from a
                                         `date` / `timestamp` column, else from the index. The symbol comes from the file name when there is no column.
  add_membership(panel, ...)             adds `in_universe` per (symbol, date), decided only from bars BEFORE that date:
                                           >= min_history prior bars, trailing `adv_window`-bar average traded value (close x volume, lagged one day) >= min_adv,
                                           optionally only the top_n by that value on the date
  survivorship_report(panel, ...)        how many symbols stopped trading before the panel ended, and how many in-universe rows they supply
Pure pandas. A symbol rename (old ticker -> new ticker) shows up as one symbol ending and another starting; this does not link them.
Prices are as archived (not split / bonus adjusted): adjust before computing returns across a corporate action.
"""
from __future__ import annotations

import glob
import os
from typing import Any, Dict, List

import pandas as pd

_PATTERNS = ("*.parquet", "*.csv", "*.csv.gz")


def _symbol_from_name(path: str) -> str:
    base = os.path.basename(path)
    for ext in (".csv.gz", ".parquet", ".csv"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    parts = base.rsplit("_", 2)
    return (parts[0] if len(parts) == 3 else base).upper()


def _read_one(path: str) -> pd.DataFrame:
    if path.endswith(".parquet"):
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path)
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if "date" not in df.columns:
        if "timestamp" in df.columns:
            df = df.rename(columns={"timestamp": "date"})
        else:
            df = df.reset_index()
            df.columns = [str(c).lower() for c in df.columns]
            first = df.columns[0]
            df = df.rename(columns={first: "date"})
    if "symbol" not in df.columns:
        df["symbol"] = _symbol_from_name(path)
    df["symbol"] = df["symbol"].astype(str).str.upper()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    return df


def load_archive(directory: str) -> pd.DataFrame:
    files: List[str] = []
    for pat in _PATTERNS:
        files += glob.glob(os.path.join(directory, pat))
    frames = []
    for path in sorted(set(files)):
        try:
            frames.append(_read_one(path))
        except ImportError:
            raise
        except Exception:  # noqa: BLE001 - one unreadable month must not hide the rest
            continue
    if not frames:
        return pd.DataFrame(columns=["date", "symbol", "close", "volume"])
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.dropna(subset=["date"]).drop_duplicates(subset=["symbol", "date"], keep="last")
    return panel.sort_values(["symbol", "date"]).reset_index(drop=True)


def add_membership(
    panel: pd.DataFrame,
    min_history: int = 60,
    adv_window: int = 20,
    min_adv: float = 1e7,
    top_n: int = 0,
) -> pd.DataFrame:
    """Adds `n_prior`, `adv_prior` and `in_universe`. Every figure uses bars strictly before the row's date, so a stock enters the
    universe only after it has proven liquid, and stays in (with its bars) until it stops trading."""
    df = panel.sort_values(["symbol", "date"]).reset_index(drop=True).copy()
    df["_tv"] = df["close"].astype(float) * df["volume"].astype(float)
    g = df.groupby("symbol", sort=False)
    df["n_prior"] = g.cumcount()  # bars before this one
    df["adv_prior"] = g["_tv"].transform(lambda s: s.shift(1).rolling(adv_window, min_periods=adv_window).mean())
    ok = (df["n_prior"] >= min_history) & (df["adv_prior"] >= min_adv)
    if top_n and top_n > 0:
        rank = df.assign(_a=df["adv_prior"].where(ok)).groupby("date")["_a"].rank(ascending=False, method="first")
        ok = ok & (rank <= top_n)
    df["in_universe"] = ok.fillna(False).astype(bool)
    return df.drop(columns=["_tv"])


def survivorship_report(panel: pd.DataFrame, tolerance_days: int = 5) -> Dict[str, Any]:
    """Symbols whose last bar is more than `tolerance_days` before the panel's last date stopped trading early. Needs `in_universe` for the row counts."""
    if len(panel) == 0:
        return {"symbols": 0, "ended_early_symbols": [], "ended_early_rows": 0, "ended_early_share": 0.0, "last_date": None}
    last = panel["date"].max()
    ends = panel.groupby("symbol")["date"].max()
    dead = sorted(ends[ends < last - pd.Timedelta(days=tolerance_days)].index.tolist())
    if "in_universe" in panel.columns:
        sel = panel[panel["in_universe"]]
    else:
        sel = panel
    rows = int(sel["symbol"].isin(dead).sum())
    share = float(rows / len(sel)) if len(sel) else 0.0
    return {
        "symbols": int(ends.shape[0]),
        "ended_early_symbols": dead,
        "ended_early_rows": rows,
        "ended_early_share": round(share, 4),
        "last_date": str(last.date()),
    }
