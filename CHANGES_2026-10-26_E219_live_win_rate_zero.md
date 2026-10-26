# E219 - a live win rate of 0.0 was read as "missing" (the closed-loop bar-raise was lost exactly when the live edge is worst)

Includes E218 (and E217).

**Why:** three `or` chains treated a real 0.0 as absent: `(training).get("live_win_rate") or (training).get("win_rate")` twice in `decision/main.py::_decide_impl`
(the `flags` for `multi_horizon_decide`, and the `live_wr` for `_decide`) and `st.get("live_win_rate") or st.get("win_rate") or st.get("overall_win_rate")` in
`training/app.py::/training-score`. 0.0 means every evaluated outcome lost; `_live_win_rate_threshold_shift` and `horizons._threshold_offsets` turn that into the
strongest "raise the bar" shift (+8) once n >= 8. The value never reached them: the training service answered `null`, and the decision service chain gave `None`
(or, for `_decide`, fell through to `t1_success_probability`, a different and usually much higher number).
**Change:** first value that is present (not None) wins, in all three places (new `_first_present` helper in decision/main.py; a `next(...)` expression in training/app.py).
Behaviour with a non-zero rate, a missing rate, or the `t1_success_probability` fallback is unchanged.
**Records:** E189 and E190 both state they read `_decide_impl` in full; REMAINING_TASKS' "still unread" line for it was out of date. This pass found E219 in it anyway.
**Not changed (small, noted):** `extras["rs_vs_nifty"]` and `extras["quality_score"]` still use `or`; horizons.py's `_first` already recovers the real value for RS, and a quality_score of 0 falls back to fundamental_score.
**Tests:** `decision/tests/test_e219_live_win_rate_zero.py` (12 tests; the training-service part executes the real source line). Run in a stand-in runner (no pytest here): 12/12 pass on the new code, 6/12 fail on the E218 code.
Neighbouring suites (e148, e189, e190, e192, group353, group368) give identical results before and after; in this runner e189 (2) and e190 (1) fail on both the old and the new code (runner limitation, not E219) - check them under real pytest.

    bash scripts/docker_test.sh decision-prediction-service
