#!/usr/bin/env python3
"""F&O options backtest (eod). Thin wrapper: the code lives in services/fno-options-service/backtest. Run from the repo root; see backtest/cli.py for the options.
Writes <FNO_DATA_DIR>/backtests/fno_eod_*.json and .txt (the Real Money Calibration page's F&O tab shows and offers them)."""
import os
import sys

_SVC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "fno-options-service")
sys.path.insert(0, os.path.abspath(_SVC))

from backtest.cli import main_eod  # noqa: E402

if __name__ == "__main__":
    sys.exit(main_eod())
