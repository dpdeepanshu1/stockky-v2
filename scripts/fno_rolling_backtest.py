#!/usr/bin/env python3
"""F&O options backtest on REAL premiums (Dhan expired-options candles). Thin wrapper: the code lives in services/fno-options-service/backtest/rolling_sim.py.
First run scripts/fno_rolling_fetch.py (at least --strikes ATM,ATM+1,ATM-1,ATM+2,ATM-2), then:
  python3 scripts/fno_rolling_backtest.py --underlying NIFTY --daily nifty_d.csv --intraday nifty_5m.csv --rolling-dir data/fno_rolling --vix vix.csv
Writes <FNO_DATA_DIR>/backtests/fno_rolling_*.json and .txt."""
import os
import sys

_SVC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "fno-options-service")
sys.path.insert(0, os.path.abspath(_SVC))

from backtest.cli import main_rolling  # noqa: E402

if __name__ == "__main__":
    sys.exit(main_rolling())
