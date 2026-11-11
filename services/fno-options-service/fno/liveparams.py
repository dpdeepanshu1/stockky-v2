"""fno/liveparams.py - the live Params, read from the `config` module at CALL time (every cycle), so a switch changed on the Calibration page applies at once.

Same values as Params.from_config(config); written with explicit `config.NAME` reads so scripts/check_control_catalog.py can prove every live switch is read
at call time and never copied at import. The backtests do NOT use this (they build Params directly, with no environment)."""
from __future__ import annotations

from typing import Dict

import config

from .params import Params


def current() -> Params:
    return Params(
        min_score=float(config.FNO_SIGNAL_MIN_SCORE), vix_max=float(config.FNO_VIX_MAX), max_spread_pct=float(config.FNO_MAX_SPREAD_PCT),
        min_premium=float(config.FNO_MIN_PREMIUM), max_data_age_s=int(config.FNO_MAX_DATA_AGE_S), target_delta=float(config.TARGET_DELTA),
        delta_min=float(config.DELTA_MIN), delta_max=float(config.DELTA_MAX), strikes_each_side=int(config.STRIKES_EACH_SIDE),
        min_oi=float(config.MIN_OI), min_volume=float(config.MIN_VOLUME), risk_free_rate=float(config.RISK_FREE_RATE),
        entry_start=str(config.FNO_ENTRY_START), entry_end=str(config.FNO_ENTRY_END), squareoff_at=str(config.FNO_SQUAREOFF_AT),
        block_expiry_day=bool(config.FNO_BLOCK_EXPIRY_DAY), min_days_to_expiry=int(config.FNO_MIN_DAYS_TO_EXPIRY),
        risk_per_trade_pct=float(config.FNO_RISK_PER_TRADE_PCT), max_lots=int(config.FNO_MAX_LOTS),
        max_open_positions=int(config.FNO_MAX_OPEN_POSITIONS), max_trades_per_day=int(config.FNO_MAX_TRADES_PER_DAY),
        max_daily_loss_pct=float(config.FNO_MAX_DAILY_LOSS_PCT), strategy_max_per_day=int(config.FNO_STRATEGY_MAX_PER_DAY), capital=float(config.FNO_DEMO_CAPITAL),
        strat_trend_d=float(config.FNO_STRAT_TREND_D), strat_trend_i=float(config.FNO_STRAT_TREND_I), strat_chain_veto=float(config.FNO_STRAT_CHAIN_VETO), strat_sweep_max_d=float(config.FNO_STRAT_SWEEP_MAX_D), strat_pivot_max_d=float(config.FNO_STRAT_PIVOT_MAX_D), strat_opener_d=float(config.FNO_STRAT_OPENER_D), strat_surge_body=float(config.FNO_STRAT_SURGE_BODY), strat_surge_close=float(config.FNO_STRAT_SURGE_CLOSE), strat_surge_vol_mult=float(config.FNO_STRAT_SURGE_VOL_MULT), strat_expiry_body=float(config.FNO_STRAT_EXPIRY_BODY), strat_alpha_move=float(config.FNO_STRAT_ALPHA_MOVE), strat_alpha_pause=float(config.FNO_STRAT_ALPHA_PAUSE), strat_drift_touch_pct=float(config.FNO_STRAT_DRIFT_TOUCH_PCT),
        strat_surge_range_mult=float(config.FNO_STRAT_SURGE_RANGE_MULT), strat_expiry_range_mult=float(config.FNO_STRAT_EXPIRY_RANGE_MULT),
        strat_drift_closes=int(config.FNO_STRAT_DRIFT_CLOSES), strat_drift_slope_pct=float(config.FNO_STRAT_DRIFT_SLOPE_PCT), strat_pivot_break_pct=float(config.FNO_STRAT_PIVOT_BREAK_PCT), core_min_intraday_bars=int(config.FNO_CORE_MIN_INTRADAY_BARS),
        expiry_entry_end=str(config.FNO_EXPIRY_ENTRY_END), expiry_squareoff_at=str(config.FNO_EXPIRY_SQUAREOFF_AT),
        expiry_risk_mult=float(config.FNO_EXPIRY_RISK_MULT), expiry_real_ok=bool(config.FNO_EXPIRY_REAL),
        sl_pct=float(config.FNO_SL_PCT), target_pct=float(config.FNO_TARGET_PCT), trail_arm_pct=float(config.FNO_TRAIL_ARM_PCT),
        trail_pct=float(config.FNO_TRAIL_PCT), slippage_pct=float(config.FNO_SLIPPAGE_PCT),
        brokerage_per_order=float(config.BROKERAGE_PER_ORDER), brokerage_pct_cap=float(config.BROKERAGE_PCT_CAP),
        stt_sell_pct=float(config.STT_SELL_PCT), exch_txn_pct_nse=float(config.EXCH_TXN_PCT_NSE),
        exch_txn_pct_bse=float(config.EXCH_TXN_PCT_BSE), sebi_pct=float(config.SEBI_PCT), stamp_buy_pct=float(config.STAMP_BUY_PCT),
        gst_pct=float(config.GST_PCT),
    )


def strategy_switches() -> Dict[str, bool]:
    """Which strategies are switched on right now (explicit `config.NAME` reads at call time, so scripts/check_control_catalog.py can prove the switches are live).
    The engine reads the same switches through strategies.enabled_keys(cfg); fno/strategies.py has the order and the rules."""
    return {"CORE": bool(config.FNO_STRAT_CORE), "TREND_RIDER": bool(config.FNO_STRAT_TREND_RIDER), "MOMENTUM_SURGE": bool(config.FNO_STRAT_MOMENTUM_SURGE),
            "BEAR_SWEEP": bool(config.FNO_STRAT_BEAR_SWEEP), "BEAR_PIVOT": bool(config.FNO_STRAT_BEAR_PIVOT), "DRIFT_EDGE": bool(config.FNO_STRAT_DRIFT_EDGE),
            "WEEKLY_OPENER": bool(config.FNO_STRAT_WEEKLY_OPENER),
            "EXPIRY_MOMENTUM": bool(config.FNO_STRAT_EXPIRY_MOMENTUM), "EXPIRY_ALPHA": bool(config.FNO_STRAT_EXPIRY_ALPHA)}
