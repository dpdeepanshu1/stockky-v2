"""ml_wiring.py - E73: D22 / D23 / D24 wired into the OFFLINE OHLCV training run (`python train.py --legacy-ohlcv --config cfg.json`).

Everything here is opt-in through ONE config block, `ml_extras`. Without it (and with `target_type` left at Log_Return) `train.py` behaves exactly as before -
nothing in this module is imported by a live decision, gate or the /train endpoint. Pure numpy / pandas, no I/O besides reading the archive / ban / OI files you point it at.

    "ml_extras": {
        "source": "archive",              # "yfinance" (default) | "archive": build the dataset from the bhavcopy archive (keeps delisted symbols)
        "archive_dir": "archive_parquet", # SYMBOL_YYYY_MM.parquet / .csv / .csv.gz (bhavcopy_archive.py)
        "archive_symbols": [],            # [] = every symbol in the archive
        "universe": {"enabled": true, "min_history": 60, "adv_window": 20, "min_adv": 1e7, "top_n": 0},   # D23 point-in-time membership, rows outside are dropped
        "fo": {"enabled": true, "ban_dir": "fo_data", "oi_dir": "fo_data"},                                # D24 ban flags / open-interest build-up as features
        "barrier_params": {"up_atr": 2.0, "dn_atr": 1.0, "cost_pct": 0.3, "slip_pct": 0.1},               # D22, with target_type TripleBarrier_Net | TripleBarrier_Class
        "purge": true,                    # D22 purge: drop training rows whose label window reaches the validation block (by DATE, the frame is pooled)
        "long_threshold": null            # null = 0.5 for TripleBarrier_Class, trading.long_threshold otherwise
    }

Honest limits: archive prices are not split / bonus adjusted; a ticker rename looks like one symbol ending and another starting; ban / OI flags are NaN (never 0)
on dates the ban / OI files do not cover. A "yfinance" source cannot build a point-in-time universe (it only knows today's survivors), so that combination is refused.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

import calibration as cal
from fo_features import (BAN_FEATURE_COLUMNS, OI_FEATURE_COLUMNS, add_ban_flags, add_oi_features, load_ban_history, load_oi_history)
from pit_universe import add_membership, load_archive, survivorship_report
from triple_barrier import TB_COLUMNS

TB_TARGETS = ("TripleBarrier_Net", "TripleBarrier_Class")
OHLCV = ("open", "high", "low", "close", "volume")

EXTRAS_DEFAULTS: Dict[str, Any] = {
    "source": "yfinance",
    "archive_dir": "",
    "archive_symbols": [],
    "universe": {"enabled": False, "min_history": 60, "adv_window": 20, "min_adv": 1e7, "top_n": 0},
    "fo": {"enabled": False, "ban_dir": "", "oi_dir": ""},
    "barrier_params": {},
    "purge": True,
    "long_threshold": None,
}


def is_triple_barrier(target_type: Any) -> bool:
    return str(target_type) in TB_TARGETS


def resolve_extras(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """EXTRAS_DEFAULTS with config['ml_extras'] merged over them (one level deep). A wrong type / value raises ValueError: this is an offline tool, a silent
    fallback would train on something other than what the config says."""
    out = copy.deepcopy(EXTRAS_DEFAULTS)
    raw = (config or {}).get("ml_extras")
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise ValueError("ml_extras must be an object")
    unknown = sorted(set(raw) - set(out))
    if unknown:
        raise ValueError(f"ml_extras: unknown keys {unknown}")
    for k, v in raw.items():
        if isinstance(out[k], dict):
            if not isinstance(v, dict):
                raise ValueError(f"ml_extras.{k} must be an object")
            bad = sorted(set(v) - set(out[k])) if k in ("universe", "fo") else []
            if bad:
                raise ValueError(f"ml_extras.{k}: unknown keys {bad}")
            out[k].update(v)
        else:
            out[k] = v
    if out["source"] not in ("yfinance", "archive"):
        raise ValueError("ml_extras.source must be 'yfinance' or 'archive'")
    if not isinstance(out["archive_symbols"], (list, tuple)):
        raise ValueError("ml_extras.archive_symbols must be a list")
    if out["source"] == "archive" and not str(out["archive_dir"] or "").strip():
        raise ValueError("ml_extras.source 'archive' needs archive_dir")
    if out["universe"]["enabled"] and out["source"] != "archive":
        raise ValueError("ml_extras.universe needs source 'archive': yfinance only knows today's survivors, so the universe would not be point-in-time")
    fo = out["fo"]
    if fo["enabled"] and not (str(fo["ban_dir"] or "").strip() or str(fo["oi_dir"] or "").strip()):
        raise ValueError("ml_extras.fo enabled needs ban_dir and / or oi_dir")
    lt = out["long_threshold"]
    if lt is not None:
        try:
            out["long_threshold"] = float(lt)
        except (TypeError, ValueError):
            raise ValueError("ml_extras.long_threshold must be a number or null")
    if not isinstance(out["barrier_params"], dict):
        raise ValueError("ml_extras.barrier_params must be an object")
    out["purge"] = bool(out["purge"])
    return out


def active(config: Optional[Dict[str, Any]]) -> bool:
    """True when the run asks for any of D22 / D23 / D24: a ml_extras block or a triple-barrier target."""
    c = config or {}
    return c.get("ml_extras") is not None or is_triple_barrier(c.get("target_type"))


def extra_feature_columns(extras: Dict[str, Any]) -> List[str]:
    """Model features this block adds (on top of features.FEATURE_COLUMNS)."""
    fo = extras["fo"]
    cols: List[str] = []
    if fo["enabled"]:
        if str(fo["ban_dir"] or "").strip():
            cols += list(BAN_FEATURE_COLUMNS)
        if str(fo["oi_dir"] or "").strip():
            cols += list(OI_FEATURE_COLUMNS)
    return cols


def long_threshold(config: Dict[str, Any], extras: Dict[str, Any]) -> float:
    if extras.get("long_threshold") is not None:
        return float(extras["long_threshold"])
    if config.get("target_type") == "TripleBarrier_Class":
        return 0.5
    return float(((config.get("trading") or {}).get("long_threshold", 0.0)))


def trading_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """The `trading` block, with the simulator's own bps costs zeroed for a triple-barrier target: tb_net_return is already net of costs + slippage, so
    charging them again would count them twice."""
    t = dict(config["trading"])
    if is_triple_barrier(config.get("target_type")):
        t["transaction_cost_bps"] = 0.0
        t["slippage_bps"] = 0.0
    return t


# ------------------------------------------------------------------ dataset
def _raw_panel_from_archive(extras: Dict[str, Any]) -> pd.DataFrame:
    panel = load_archive(str(extras["archive_dir"]))
    missing = [c for c in OHLCV if c not in panel.columns]
    if len(panel) == 0 or missing:
        raise ValueError(f"archive {extras['archive_dir']!r}: no usable rows" if len(panel) == 0 else f"archive is missing columns {missing}")
    want = {str(s).upper() for s in (extras.get("archive_symbols") or [])}
    if want:
        panel = panel[panel["symbol"].isin(want)]
    keep = ["date", "symbol", *OHLCV]
    return panel[keep].dropna(subset=list(OHLCV)).sort_values(["symbol", "date"]).reset_index(drop=True)


def _attach_fo(panel: pd.DataFrame, extras: Dict[str, Any]) -> pd.DataFrame:
    fo = extras["fo"]
    if not fo["enabled"]:
        return panel
    df = panel
    if str(fo["ban_dir"] or "").strip():
        ban = load_ban_history(str(fo["ban_dir"]))
        df = add_ban_flags(df, ban)
        # a date the ban files do not cover is "unknown", not "not banned": NaN, so the model can tell them apart
        covered = _covered_dates(str(fo["ban_dir"]), "fo_secban_")
        off = ~df["date"].isin(covered) if covered is not None else pd.Series(True, index=df.index)
        for c in BAN_FEATURE_COLUMNS:
            df[c] = df[c].astype(float).where(~off, np.nan)
    if str(fo["oi_dir"] or "").strip():
        oi = load_oi_history(str(fo["oi_dir"]))
        if len(oi):
            df = df.drop(columns=[c for c in ("open_interest",) if c in df.columns]).merge(oi, on=["date", "symbol"], how="left")
        df = add_oi_features(df)
    return df


def _covered_dates(directory: str, prefix: str) -> Optional[pd.DatetimeIndex]:
    import glob
    import re
    days = []
    for path in glob.glob(os.path.join(directory, prefix + "*")):
        m = re.search(r"(\d{8})", os.path.basename(path))
        if m:
            days.append(pd.to_datetime(m.group(1), format="%Y%m%d"))
    return pd.DatetimeIndex(sorted(set(days))) if days else None


def build_dataset(symbols: Sequence[str], config: Dict[str, Any], extras: Dict[str, Any], period: str, fetch_default: Callable[..., pd.DataFrame],
                  compute_features: Callable[[pd.DataFrame], pd.DataFrame], feature_columns: Sequence[str]) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """The feature frame (columns: open..volume, features, symbol, date, + in_universe / ban / OI columns), and a small report.
    yfinance source: train.build_multi_symbol_dataset as before, then the F&O features. archive source: the bhavcopy archive, point-in-time membership, F&O
    features, then the same indicator frame per symbol. Labels are NOT made here - they need the bars after each row, so the universe filter is applied after them
    (see finalize)."""
    rep: Dict[str, Any] = {"source": extras["source"], "extra_features": extra_feature_columns(extras)}
    if extras["source"] == "yfinance":
        df = fetch_default(symbols, period=period)
        df = _attach_fo(df, extras)
        return df, rep
    panel = _raw_panel_from_archive(extras)
    rep["archive_rows"] = int(len(panel))
    rep["archive_symbols"] = int(panel["symbol"].nunique())
    u = extras["universe"]
    if u["enabled"]:
        panel = add_membership(panel, min_history=int(u["min_history"]), adv_window=int(u["adv_window"]), min_adv=float(u["min_adv"]), top_n=int(u["top_n"]))
        rep["survivorship"] = survivorship_report(panel)
    panel = _attach_fo(panel, extras)
    frames = []
    for sym, g in panel.groupby("symbol", sort=True):
        g = g.sort_values("date").reset_index(drop=True)
        f = compute_features(g)
        f = f.dropna(subset=list(feature_columns))
        if len(f) < 50:
            continue
        frames.append(f)
    if not frames:
        raise ValueError("archive dataset: no symbol has enough rows")
    out = pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)
    rep["symbols_used"] = int(out["symbol"].nunique())
    return out, rep


def label_group(group: pd.DataFrame, generated: pd.DataFrame, ma_fast: int = 20, ma_slow: int = 50) -> pd.DataFrame:
    """Called per symbol inside train.apply_targets: copies the TB columns, adds `tb_end_date` (the DATE of the exit bar, for the purge) and `ma_cross`
    (the dumb baseline's entry flag). `generated` is TargetGenerator.generate()'s frame for the same group."""
    for col in TB_COLUMNS:
        group[col] = generated[col]
    dates = pd.to_datetime(group["date"]).to_numpy()
    end = generated["tb_end_idx"].to_numpy(dtype=float)
    out = np.full(len(group), np.datetime64("NaT"), dtype="datetime64[ns]")
    ok = np.isfinite(end)
    out[ok] = dates[end[ok].astype(int)]
    group["tb_end_date"] = pd.Series(out, index=group.index)
    try:
        group["ma_cross"] = cal.ma_cross_signals(group.reset_index(drop=True), ma_fast, ma_slow).to_numpy()
    except Exception:  # noqa: BLE001
        group["ma_cross"] = False
    return group


def finalize(df: pd.DataFrame, extras: Dict[str, Any]) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """After the labels exist: keep only point-in-time universe members (D23). Returns (frame, report)."""
    rep: Dict[str, Any] = {"rows_before_universe": int(len(df))}
    if extras["universe"]["enabled"] and "in_universe" in df.columns:
        df = df[df["in_universe"].astype(bool)]
    rep["rows_after_universe"] = int(len(df))
    return df.reset_index(drop=True), rep


def purge_train(train: pd.DataFrame, val: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
    """D22 purge: drop training rows whose label window ends on / after the first validation date (a row without tb_end_date cannot be trusted either)."""
    if "tb_end_date" not in train.columns or len(val) == 0:
        return train, 0
    first_val = pd.to_datetime(val["date"]).min()
    end = pd.to_datetime(train["tb_end_date"])
    keep = end.notna() & (end < first_val)
    return train[keep], int((~keep).sum())


# ------------------------------------------------------------------ evaluation
def evaluate_picks(oos: pd.DataFrame, thr: float, boot: int = 1000) -> Dict[str, Any]:
    """`oos` has pred, tb_y, tb_net_return, ma_cross (one row per validation row). Brier + reliability of the probabilities, and the model's picks (pred > thr)
    vs the MA-cross entries on the same cost-aware labels (calibration.compare_to_baseline)."""
    out: Dict[str, Any] = {"rows": int(len(oos))}
    if len(oos) == 0:
        return out
    p, y = oos["pred"].to_numpy(float), oos["tb_y"].to_numpy(float)
    out["brier"] = cal.brier(p, y)
    out["reliability"] = cal.reliability_table(p, y)
    picks = oos.loc[oos["pred"] > thr, "tb_net_return"].to_numpy(float)
    base = oos.loc[oos["ma_cross"].astype(bool), "tb_net_return"].to_numpy(float)
    out["vs_ma_cross"] = cal.compare_to_baseline(picks, base, boot=boot)
    return out


def describe(extras: Dict[str, Any]) -> Dict[str, Any]:
    """What the report records about the run's wiring (config echo)."""
    return {"source": extras["source"], "universe": dict(extras["universe"]), "fo": dict(extras["fo"]), "barrier_params": dict(extras["barrier_params"]),
            "purge": extras["purge"], "extra_features": extra_feature_columns(extras)}
