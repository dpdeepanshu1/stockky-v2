"""
Trading simulation with costs and signals.
"""
import numpy as np
from typing import Optional, Tuple

class TradingSimulator:
    def __init__(self, long_threshold=0.0, short_threshold=0.0,
                 transaction_cost_bps=5.0, slippage_bps=2.0,
                 allow_short=False, initial_capital=1.0):
        self.long_threshold = long_threshold
        self.short_threshold = short_threshold
        self.cost_bps = transaction_cost_bps + slippage_bps
        self.allow_short = allow_short

    def generate_signals(self, pred_returns):
        sig = np.zeros_like(pred_returns)
        sig[pred_returns > self.long_threshold] = 1
        if self.allow_short:
            sig[pred_returns < self.short_threshold] = -1
        return sig

    def simulate(self, pred_returns, actual_returns, prev_positions=None):
        pred = np.array(pred_returns)
        actual = np.array(actual_returns)
        signals = self.generate_signals(pred)
        if prev_positions is None:
            prev_positions = np.zeros_like(signals)
        # Costs per change
        cost_per_trade = self.cost_bps / 10000.0
        pos_changes = np.abs(signals - prev_positions)
        costs = pos_changes * cost_per_trade
        strategy_returns = signals * actual - costs
        return signals, costs, strategy_returns