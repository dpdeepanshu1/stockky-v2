"""engine.py - one forecast request end to end, with the model injected (a fake in the tests). No torch, no FastAPI here."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional, Protocol

import forecast_stats as fs


class _Forecaster(Protocol):
    def sample_paths(self, df, future_ts, n_paths: int, seed_key: str): ...


def run_forecast(forecaster: _Forecaster, candles: Any, ref_price: float, stop_pct: float, target_pct: float, *, horizon: int = 5,
                 n_paths: int = 10, lookback: int = 128, today: Optional[date] = None, symbol: str = "") -> Dict[str, Any]:
    """Raises forecast_stats.BadHistory when the candles are unusable, ValueError on bad parameters."""
    horizon = max(1, min(int(horizon), 20))
    n_paths = max(2, min(int(n_paths), 50))
    df = fs.clean_history(candles, today=today, lookback=lookback)
    last_close = float(df["close"].iloc[-1])
    last_date = df["date"].iloc[-1]
    if not (0.5 <= ref_price / last_close <= 1.5):
        raise fs.BadHistory(f"ref price {ref_price:.2f} is far from the last close {last_close:.2f} (split / bad data)")
    fut = fs.future_dates(last_date, horizon)
    seed_key = f"{symbol.upper()}|{last_date.date().isoformat()}|{horizon}|{n_paths}"
    closes, highs, lows = forecaster.sample_paths(df, fut, n_paths, seed_key)
    stats = fs.summarize_paths(closes, highs, lows, last_close, float(ref_price), float(stop_pct), float(target_pct))
    stats.update(symbol=symbol.upper(), last_bar=last_date.date().isoformat(), last_close=round(last_close, 4), ref_price=float(ref_price),
                 stop_pct=float(stop_pct), target_pct=float(target_pct), bars_used=int(len(df)))
    return stats
