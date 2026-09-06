"""
Financial metrics for evaluating strategy performance.
"""
import numpy as np
import pandas as pd
from typing import Union, Dict

def calculate_sharpe(returns, rf=0.0, periods=252):
    ret = np.array(returns)
    excess = ret - rf/periods
    if np.std(excess) == 0:
        return 0.0
    return np.mean(excess) / np.std(excess) * np.sqrt(periods)

def calculate_sortino(returns, rf=0.0, periods=252):
    ret = np.array(returns)
    excess = ret - rf/periods
    downside = excess[excess < 0]
    if len(downside) == 0 or np.std(downside) == 0:
        return 0.0
    return np.mean(excess) / np.std(downside) * np.sqrt(periods)

def max_drawdown(equity):
    eq = np.array(equity)
    running_max = np.maximum.accumulate(eq)
    dd = (eq - running_max) / running_max
    return np.min(dd)

def max_drawdown_duration(equity):
    eq = np.array(equity)
    running_max = np.maximum.accumulate(eq)
    max_dur = 0; cur = 0
    for i in range(len(eq)):
        if eq[i] < running_max[i]:
            cur += 1
            max_dur = max(max_dur, cur)
        else:
            cur = 0
    return max_dur

def cumulative_return(returns):
    return np.prod(1 + np.array(returns)) - 1

def win_rate(returns):
    ret = np.array(returns)
    active = ret[ret != 0]
    if len(active) == 0:
        return 0.0
    return np.mean(active > 0)

def profit_factor(returns):
    ret = np.array(returns)
    gross_profit = np.sum(ret[ret > 0])
    gross_loss = -np.sum(ret[ret < 0])
    if gross_loss == 0:
        return np.inf
    return gross_profit / gross_loss

def directional_accuracy(pred, actual):
    pred = np.array(pred); actual = np.array(actual)
    mask = (pred != 0) & (actual != 0)
    if not np.any(mask):
        return 0.0
    return np.mean(np.sign(pred[mask]) == np.sign(actual[mask]))

def compute_all_metrics(pred_returns, actual_returns, strategy_returns):
    equity = np.cumprod(1 + np.array(strategy_returns))
    return {
        'RMSE': np.sqrt(np.mean((pred_returns - actual_returns)**2)),
        'MAE': np.mean(np.abs(pred_returns - actual_returns)),
        'DirectionalAccuracy': directional_accuracy(pred_returns, actual_returns),
        'WinRate': win_rate(strategy_returns),
        'ProfitFactor': profit_factor(strategy_returns),
        'SharpeRatio': calculate_sharpe(strategy_returns),
        'SortinoRatio': calculate_sortino(strategy_returns),
        'MaximumDrawdown': max_drawdown(equity),
        'MaximumDrawdownDuration': max_drawdown_duration(equity),
        'CumulativeReturn': cumulative_return(strategy_returns)
    }