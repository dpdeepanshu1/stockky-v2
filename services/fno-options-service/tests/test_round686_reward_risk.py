"""Round 686 (E708): the target follows the stop, so every F&O entry is reward:risk 1:2 (FNO_MIN_REWARD_RISK, default 2.0).
Before: the round-498 noise floor could widen the stop to 35 % while the target stayed 50 % (1:1.43)."""
from __future__ import annotations

from dataclasses import fields

import config
from fno import paper, sizing
from fno.params import CONFIG_NAME, Params

P = Params()


def test_default_is_one_to_two_and_the_shipped_stop_and_target_already_fit():
    assert P.min_reward_risk == 2.0 and config.FNO_MIN_REWARD_RISK == 2.0
    assert P.target_pct >= P.min_reward_risk * P.sl_pct                      # 50 >= 2 x 25
    assert sizing.with_noise_stop(P, 150.0, 0.5, 300.0) is P                 # nothing widens, nothing changes: the very same object


def test_a_widened_stop_lifts_the_target_to_twice_the_stop():
    w = sizing.with_noise_stop(P, 100.0, 0.5, 300.0)                         # noise stop 30 %
    assert w.sl_pct == 30.0 and w.target_pct == 60.0
    w = sizing.with_noise_stop(P, 60.0, 0.5, 300.0)                          # capped at 35 %
    assert w.sl_pct == 35.0 and w.target_pct == 70.0
    assert w.target_pct / w.sl_pct == 2.0


def test_a_target_already_above_the_ratio_is_never_lowered():
    q = P.with_(target_pct=90.0)
    w = sizing.with_noise_stop(q, 100.0, 0.5, 300.0)
    assert w.sl_pct == 30.0 and w.target_pct == 90.0


def test_a_target_below_the_ratio_is_raised_even_without_a_wider_stop():
    q = P.with_(target_pct=40.0)                                              # 40 / 25 = 1:1.6
    w = sizing.with_noise_stop(q, 150.0, 0.5, 300.0)
    assert w.sl_pct == 25.0 and w.target_pct == 50.0


def test_switch_off_keeps_the_flat_target():
    q = P.with_(min_reward_risk=0.0)
    w = sizing.with_noise_stop(q, 100.0, 0.5, 300.0)
    assert w.sl_pct == 30.0 and w.target_pct == 50.0
    assert sizing.with_noise_stop(q, 150.0, 0.5, 300.0) is q


def test_no_atr_means_no_change_with_the_shipped_numbers():
    assert sizing.with_noise_stop(P, 100.0, 0.5, None) is P


def test_the_param_is_wired_to_its_env_name():
    assert {f.name for f in fields(Params)} == set(CONFIG_NAME)
    assert CONFIG_NAME["min_reward_risk"] == "FNO_MIN_REWARD_RISK" and hasattr(config, "FNO_MIN_REWARD_RISK")
