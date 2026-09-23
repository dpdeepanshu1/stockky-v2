"""
Reinforcement learning exploration for Stockky (research scaffold).

Honest scope on free tier:
  - Full deep RL (PPO/SAC on ticks) is NOT feasible with Yahoo latency, 512MB, and sparse labels.
  - What *is* useful: a contextual bandit / epsilon-greedy layer over discrete actions
    {BUY_NOW, PREPARE, WAIT, AVOID} using T+1 reward from PredictionOutcome.

This module:
  1) Defines action space and reward from stored T+1 outcomes
  2) Tracks simple per-context action values in Redis or memory
  3) Can suggest an exploratory override for *paper* policy experiments

It does NOT auto-place live trades. Wire into paper trading only after review.
"""
from __future__ import annotations

import logging
import math
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("rl-explore")

ACTIONS = ("BUY_NOW", "PREPARE", "WAIT", "AVOID")
# Map engine decisions into bandit actions
_DECISION_MAP = {
    "BUY NOW": "BUY_NOW",
    "PREPARE TO BUY": "PREPARE",
    "PREPARE": "PREPARE",
    "WAIT": "WAIT",
    "HOLD": "WAIT",
    "DO NOT BUY": "AVOID",
    "AVOID / WAIT": "AVOID",
    "SELL": "AVOID",
}

# Reward: T+1 return for BUY/PREPARE; inverse for AVOID if market rose hard
def reward_from_t1(action: str, return_pct: float, success: bool) -> float:
    r = float(return_pct or 0.0) / 100.0
    a = (action or "").upper()
    if a in ("BUY_NOW", "PREPARE"):
        # Prefer successful upside; mild penalty for losses
        base = r * (1.2 if a == "BUY_NOW" else 1.0)
        return base + (0.02 if success else -0.01)
    if a in ("WAIT", "AVOID"):
        # Good if we avoided a down move; bad if we missed a strong up move
        if r <= 0:
            return 0.01 - r * 0.3
        return -min(0.05, r * 0.5)
    return 0.0


class EpsilonGreedyBandit:
    """
    Tiny contextual bandit: context bucket = quality + score band.
    Stores action counts/values in-process; optional Redis later.
    """

    def __init__(self, epsilon: float = 0.08):
        self.epsilon = float(os.getenv("RL_EPSILON", str(epsilon)))
        self._n: Dict[str, Dict[str, int]] = {}
        self._q: Dict[str, Dict[str, float]] = {}

    def _ctx(self, quality: str, score: float, provisional: bool) -> str:
        band = "h" if score >= 70 else "m" if score >= 55 else "l"
        return f"{quality or 'low'}:{band}:{'p' if provisional else 'f'}"

    def suggest(
        self,
        engine_decision: str,
        combined_score: float,
        quality: str = "medium",
        provisional: bool = False,
        explore: bool = True,
    ) -> Dict[str, Any]:
        """
        Returns {action, explore, engine_action, note}.
        Never upgrades WAIT→BUY_NOW when provisional=True.
        """
        engine_action = _DECISION_MAP.get((engine_decision or "").upper(), "WAIT")
        ctx = self._ctx(quality, float(combined_score or 0), provisional)
        self._n.setdefault(ctx, {a: 0 for a in ACTIONS})
        self._q.setdefault(ctx, {a: 0.0 for a in ACTIONS})

        chosen = engine_action
        did_explore = False
        if explore and random.random() < self.epsilon:
            # Explore among safer set if provisional
            pool = list(ACTIONS)
            if provisional:
                pool = [a for a in pool if a != "BUY_NOW"]
            chosen = random.choice(pool)
            did_explore = True

        # Policy safety: provisional cannot produce BUY_NOW
        if provisional and chosen == "BUY_NOW":
            chosen = "PREPARE" if engine_action in ("BUY_NOW", "PREPARE") else "WAIT"

        return {
            "action": chosen,
            "engine_action": engine_action,
            "explore": did_explore,
            "context": ctx,
            "epsilon": self.epsilon,
            "note": "paper-only bandit; not live brokerage",
        }

    def update(self, context: str, action: str, reward: float) -> None:
        action = (action or "").upper()
        if action not in ACTIONS:
            return
        self._n.setdefault(context, {a: 0 for a in ACTIONS})
        self._q.setdefault(context, {a: 0.0 for a in ACTIONS})
        n = self._n[context][action] + 1
        self._n[context][action] = n
        q = self._q[context][action]
        self._q[context][action] = q + (reward - q) / n

    def snapshot(self) -> Dict[str, Any]:
        return {"n": self._n, "q": self._q, "epsilon": self.epsilon}


# Process singleton for optional paper experiments
bandit = EpsilonGreedyBandit()


def explore_note() -> Dict[str, Any]:
    return {
        "feasible_on_free_tier": [
            "Contextual bandit / epsilon-greedy over decision actions",
            "Reward = realized T+1 return from evaluate.py outcomes",
            "Paper-trading policy experiments only",
        ],
        "not_feasible_now": [
            "Tick-level deep RL (PPO/SAC) with continuous orders",
            "Low-latency execution against NSE",
            "Large replay buffers on 512MB dynos",
        ],
        "recommended_path": (
            "Accumulate T+1 outcomes → update bandit Q-values offline nightly → "
            "compare paper PnL vs rule engine before any live use"
        ),
    }
