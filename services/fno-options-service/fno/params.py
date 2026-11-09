"""fno/params.py - one plain dataclass with every tunable the pure modules need. The live engine builds it from `config` on every cycle (so a switch change on
the Calibration page applies at once); the backtests build it directly, with no environment and no config module."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Dict


@dataclass(frozen=True)
class Params:
    # signal
    min_score: float = 45.0
    vix_max: float = 25.0
    max_spread_pct: float = 3.0
    min_premium: float = 20.0
    max_data_age_s: int = 25
    target_delta: float = 0.52
    delta_min: float = 0.35
    delta_max: float = 0.65
    strikes_each_side: int = 3
    min_oi: float = 50000.0
    min_volume: float = 1000.0
    risk_free_rate: float = 0.065
    # gates
    entry_start: str = "09:30"
    entry_end: str = "14:30"
    squareoff_at: str = "15:15"
    block_expiry_day: bool = True
    min_days_to_expiry: int = 1
    # risk
    risk_per_trade_pct: float = 1.0
    max_lots: int = 2
    max_open_positions: int = 2
    max_trades_per_day: int = 3
    max_daily_loss_pct: float = 3.0
    strategy_max_per_day: int = 1          # entries per day for each NON-core strategy (CORE is limited only by max_trades_per_day)
    # named-strategy thresholds (round 237): were constants in strategies.py. Defaults = the old values, so nothing changes until a calibration run says otherwise
    strat_surge_range_mult: float = 1.5    # MOMENTUM_SURGE: latest bar's range >= this x the session median range
    strat_expiry_range_mult: float = 1.3   # EXPIRY_MOMENTUM: same test on expiry day
    strat_drift_closes: int = 10           # DRIFT_EDGE: closes on the right side of VWAP, out of the last 60 minutes (12 five-minute bars)
    strat_drift_slope_pct: float = 0.04    # DRIFT_EDGE: VWAP slope over 30 minutes, % of price
    strat_pivot_break_pct: float = 0.03    # BEAR_PIVOT: close this far (% of price) beyond the pivot
    strat_trend_d: float = 40.0    # daily-trend score for a trend-day strategy; and the 'not against a strong trend' limit of the surge / expiry strategies
    strat_trend_i: float = 15.0    # TREND_RIDER: intraday score that must agree
    strat_chain_veto: float = 15.0    # the option chain vetoes a signal when it leans this far against it
    strat_sweep_max_d: float = 50.0    # BEAR_SWEEP: not above this daily score
    strat_pivot_max_d: float = 15.0    # BEAR_PIVOT: not above this daily score
    strat_opener_d: float = 15.0    # WEEKLY_OPENER: daily score needed in the break direction
    strat_surge_body: float = 0.6     # MOMENTUM_SURGE: body as a fraction of the bar range
    strat_surge_close: float = 0.7     # MOMENTUM_SURGE: close in the outer (1 - this) of the bar
    strat_surge_vol_mult: float = 1.3     # MOMENTUM_SURGE: volume vs the session average (only when volume exists)
    strat_expiry_body: float = 0.5     # EXPIRY_MOMENTUM: body as a fraction of the bar range
    strat_alpha_move: float = 0.5     # EXPIRY_ALPHA: session range as a fraction of the daily range
    strat_alpha_pause: float = 0.4     # EXPIRY_ALPHA: the pause range as a fraction of the session range
    strat_drift_touch_pct: float = 0.08    # DRIFT_EDGE: how close (% of VWAP) a pullback must come to the VWAP
    # expiry-day strategies (round 236; EXPIRY_MOMENTUM / EXPIRY_ALPHA only. Every other strategy still cannot enter on expiry day)
    expiry_entry_end: str = "13:30"        # last entry time on the contract's expiry day
    expiry_squareoff_at: str = "14:45"     # an expiry-day position is closed by then, before the last-30-minute VWAP settlement window (15:00-15:30) starts on 7 Dec 2026
    expiry_risk_mult: float = 0.5          # risk per trade on expiry day = risk_per_trade_pct x this (gamma: a premium can lose most of its value in minutes)
    expiry_real_ok: bool = False           # expiry-day entries in REAL mode need this ON (env FNO_EXPIRY_REAL only: deliberately NOT a Calibration-page switch)
    capital: float = 500000.0
    # exits
    sl_pct: float = 25.0
    target_pct: float = 50.0
    trail_arm_pct: float = 30.0
    trail_pct: float = 20.0
    slippage_pct: float = 0.5
    # costs
    brokerage_per_order: float = 20.0
    brokerage_pct_cap: float = 100.0
    stt_sell_pct: float = 0.15
    exch_txn_pct_nse: float = 0.03553
    exch_txn_pct_bse: float = 0.0325
    sebi_pct: float = 0.0001
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0

    def with_(self, **kw: Any) -> "Params":
        bad = set(kw) - {f.name for f in fields(self)}
        if bad:
            raise TypeError(f"unknown Params field(s): {sorted(bad)}")
        return replace(self, **kw)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_config(cls, cfg: Any) -> "Params":
        """Read the live values off the config module (call it every cycle: the page changes them at runtime)."""
        g = lambda n, d: getattr(cfg, n, d)  # noqa: E731
        return cls(
            min_score=float(g("FNO_SIGNAL_MIN_SCORE", 45.0)), vix_max=float(g("FNO_VIX_MAX", 25.0)), max_spread_pct=float(g("FNO_MAX_SPREAD_PCT", 3.0)),
            min_premium=float(g("FNO_MIN_PREMIUM", 20.0)), max_data_age_s=int(g("FNO_MAX_DATA_AGE_S", 25)), target_delta=float(g("TARGET_DELTA", 0.52)),
            delta_min=float(g("DELTA_MIN", 0.35)), delta_max=float(g("DELTA_MAX", 0.65)), strikes_each_side=int(g("STRIKES_EACH_SIDE", 3)),
            min_oi=float(g("MIN_OI", 50000.0)), min_volume=float(g("MIN_VOLUME", 1000.0)), risk_free_rate=float(g("RISK_FREE_RATE", 0.065)),
            entry_start=str(g("FNO_ENTRY_START", "09:30")), entry_end=str(g("FNO_ENTRY_END", "14:30")), squareoff_at=str(g("FNO_SQUAREOFF_AT", "15:15")),
            block_expiry_day=bool(g("FNO_BLOCK_EXPIRY_DAY", True)), min_days_to_expiry=int(g("FNO_MIN_DAYS_TO_EXPIRY", 1)),
            risk_per_trade_pct=float(g("FNO_RISK_PER_TRADE_PCT", 1.0)), max_lots=int(g("FNO_MAX_LOTS", 2)),
            max_open_positions=int(g("FNO_MAX_OPEN_POSITIONS", 2)), max_trades_per_day=int(g("FNO_MAX_TRADES_PER_DAY", 3)),
            max_daily_loss_pct=float(g("FNO_MAX_DAILY_LOSS_PCT", 3.0)), strategy_max_per_day=int(g("FNO_STRATEGY_MAX_PER_DAY", 1)), capital=float(g("FNO_DEMO_CAPITAL", 500000.0)),
            strat_trend_d=float(g("FNO_STRAT_TREND_D", 40.0)), strat_trend_i=float(g("FNO_STRAT_TREND_I", 15.0)), strat_chain_veto=float(g("FNO_STRAT_CHAIN_VETO", 15.0)), strat_sweep_max_d=float(g("FNO_STRAT_SWEEP_MAX_D", 50.0)), strat_pivot_max_d=float(g("FNO_STRAT_PIVOT_MAX_D", 15.0)), strat_opener_d=float(g("FNO_STRAT_OPENER_D", 15.0)), strat_surge_body=float(g("FNO_STRAT_SURGE_BODY", 0.6)), strat_surge_close=float(g("FNO_STRAT_SURGE_CLOSE", 0.7)), strat_surge_vol_mult=float(g("FNO_STRAT_SURGE_VOL_MULT", 1.3)), strat_expiry_body=float(g("FNO_STRAT_EXPIRY_BODY", 0.5)), strat_alpha_move=float(g("FNO_STRAT_ALPHA_MOVE", 0.5)), strat_alpha_pause=float(g("FNO_STRAT_ALPHA_PAUSE", 0.4)), strat_drift_touch_pct=float(g("FNO_STRAT_DRIFT_TOUCH_PCT", 0.08)),
            strat_surge_range_mult=float(g("FNO_STRAT_SURGE_RANGE_MULT", 1.5)), strat_expiry_range_mult=float(g("FNO_STRAT_EXPIRY_RANGE_MULT", 1.3)),
            strat_drift_closes=int(g("FNO_STRAT_DRIFT_CLOSES", 10)), strat_drift_slope_pct=float(g("FNO_STRAT_DRIFT_SLOPE_PCT", 0.04)),
            strat_pivot_break_pct=float(g("FNO_STRAT_PIVOT_BREAK_PCT", 0.03)),
            expiry_entry_end=str(g("FNO_EXPIRY_ENTRY_END", "13:30")), expiry_squareoff_at=str(g("FNO_EXPIRY_SQUAREOFF_AT", "14:45")),
            expiry_risk_mult=float(g("FNO_EXPIRY_RISK_MULT", 0.5)), expiry_real_ok=bool(g("FNO_EXPIRY_REAL", False)),
            sl_pct=float(g("FNO_SL_PCT", 25.0)), target_pct=float(g("FNO_TARGET_PCT", 50.0)), trail_arm_pct=float(g("FNO_TRAIL_ARM_PCT", 30.0)),
            trail_pct=float(g("FNO_TRAIL_PCT", 20.0)), slippage_pct=float(g("FNO_SLIPPAGE_PCT", 0.5)),
            brokerage_per_order=float(g("BROKERAGE_PER_ORDER", 20.0)), brokerage_pct_cap=float(g("BROKERAGE_PCT_CAP", 100.0)),
            stt_sell_pct=float(g("STT_SELL_PCT", 0.15)), exch_txn_pct_nse=float(g("EXCH_TXN_PCT_NSE", 0.03553)),
            exch_txn_pct_bse=float(g("EXCH_TXN_PCT_BSE", 0.0325)), sebi_pct=float(g("SEBI_PCT", 0.0001)), stamp_buy_pct=float(g("STAMP_BUY_PCT", 0.003)),
            gst_pct=float(g("GST_PCT", 18.0)),
        )
