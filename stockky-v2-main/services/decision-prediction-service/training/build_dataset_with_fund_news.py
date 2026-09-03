"""
Helper to build a training dataset that includes technical + fundamental + news
features with proper point-in-time alignment.

Usage (from training code):

    from build_dataset_with_fund_news import build_point_in_time_dataset

    X, y, meta = build_point_in_time_dataset(
        symbols=["TCS", "RELIANCE", "INFY", ...],
        history_loader=fetch_history_fn,          # symbol -> OHLCV DataFrame
        fundamental_loader=fetch_fund_history_fn, # symbol -> list of snapshots
        news_loader=fetch_news_fn,                # symbol -> list of news items
        events_loader=fetch_events_fn,            # symbol -> list of events
        sample_every_n_days=5,
        min_history_days=220,
    )

Then pass X, y into your existing walk-forward + calibration training code.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from feature_builder import (
    FEATURE_COLUMNS,
    build_training_row,
    build_training_dataset,
    make_label_from_forward_return,
)

logger = logging.getLogger("build_dataset")


def build_point_in_time_dataset(
    symbols: List[str],
    history_loader: Callable[[str], Optional[pd.DataFrame]],
    fundamental_loader: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    news_loader: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    events_loader: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    sample_every_n_days: int = 5,
    min_history_days: int = 220,
    horizon_days: int = 10,
    threshold_pct: float = 5.0,
) -> Tuple[pd.DataFrame, Optional[pd.Series], pd.DataFrame]:
    """
    Build a point-in-time dataset for the expanded feature set.

    Returns
    -------
    X : DataFrame of FEATURE_COLUMNS
    y : Series of labels (1/0) or None
    meta : DataFrame with symbol, as_of for debugging
    """
    rows: List[Dict[str, Any]] = []

    for symbol in symbols:
        try:
            hist = history_loader(symbol)
            if hist is None or len(hist) < min_history_days:
                logger.warning("Skipping %s — insufficient history", symbol)
                continue

            if not isinstance(hist.index, pd.DatetimeIndex):
                if "date" in hist.columns:
                    hist = hist.copy()
                    hist["date"] = pd.to_datetime(hist["date"])
                    hist = hist.set_index("date")
                else:
                    logger.warning("Skipping %s — no date index", symbol)
                    continue

            hist = hist.sort_index()
            fund_hist = fundamental_loader(symbol) if fundamental_loader else []
            news_items = news_loader(symbol) if news_loader else []
            events = events_loader(symbol) if events_loader else []

            # Sample dates (skip the most recent horizon_days so label is known)
            dates = hist.index.tolist()
            if len(dates) < min_history_days + horizon_days:
                continue

            usable_dates = dates[min_history_days : -horizon_days : sample_every_n_days]

            for as_of in usable_dates:
                if not isinstance(as_of, datetime):
                    as_of = pd.Timestamp(as_of).to_pydatetime()

                label = make_label_from_forward_return(
                    hist, as_of, horizon_days=horizon_days, threshold_pct=threshold_pct
                )
                if label is None:
                    continue

                row = build_training_row(
                    symbol=symbol,
                    as_of=as_of,
                    history_df=hist,
                    fundamental_history=fund_hist,
                    news_items=news_items,
                    events=events,
                    label=label,
                )
                if row:
                    rows.append(row)

            logger.info("Built %s samples for %s", sum(1 for r in rows if r["symbol"] == symbol), symbol)

        except Exception as e:
            logger.exception("Failed building samples for %s: %s", symbol, e)

    if not rows:
        logger.warning("No training rows generated")
        return pd.DataFrame(columns=FEATURE_COLUMNS), None, pd.DataFrame()

    X, y = build_training_dataset(rows)
    meta = pd.DataFrame([{"symbol": r["symbol"], "as_of": r["as_of"]} for r in rows])
    logger.info("Total training samples: %s, features: %s", len(X), list(X.columns))
    return X, y, meta
