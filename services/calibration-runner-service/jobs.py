"""Job and pack catalog of calibration-runner-service (group 442).

A job is a list of STEPS built from a few validated parameters. Steps are plain dicts (JSON-serialisable, no shell):
  {"kind": "cmd",  "title", "argv": [...], "env": {...}, "ok": [0], "attention": [1], "out_file": "x.txt"}
      argv tokens may be "globflag:<flag>:<pattern>" (one `<flag> file` pair per match, for append-style flags) or "glob:<pattern>" (expanded, sorted; no match -> the step is reported as missing input)
  {"kind": "http", "title", "method", "url", "auth": bool, "save": "name.json", "expect": [200], "poll": {...}}
Nothing is run through a shell, parameters are typed / bounded, and the admin token is never part of a step: `cmd` steps get it as env TOKEN
and `http` steps as a header at run time, then the output is redacted.

Verdict mapping: exit code in `ok` -> success; in `attention` -> "attention" (the script ran and its verdict says look at this: FAIL / NO_GO / NOT_ENOUGH_DATA);
anything else -> failed.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


def _heavy_timeout() -> int:
    """group 466: time limit of the heavy jobs (replays, backtests, probes). The A/B replay of 355 symbols was killed at the old 2 h limit with 290 done.
    Default 6 h; RUNNER_HEAVY_TIMEOUT_S (seconds) changes it; never below 1 h; an unreadable value falls back to 6 h."""
    try:
        v = int(float(os.environ.get("RUNNER_HEAVY_TIMEOUT_S", "").strip()))
    except ValueError:
        return 6 * 3600
    return max(3600, v)


HEAVY_S = _heavy_timeout()

GROUPS = [
    ("monday", "Monday / start of a session"),
    ("daily", "Every trading day"),
    ("weekly", "Weekly calibration"),
    ("monthly", "Monthly evidence (gates)"),
    ("model", "Models and training"),
    ("ops", "Checks and housekeeping"),
]


def P(name: str, label: str, kind: str = "int", default: Any = None, lo: Optional[float] = None, hi: Optional[float] = None,
      choices: Optional[List[str]] = None, help: str = "") -> Dict[str, Any]:
    return {"name": name, "label": label, "kind": kind, "default": default, "min": lo, "max": hi, "choices": choices or [], "help": help}


def _py(ctx: Dict[str, Any], script: str, *args: Any, title: str = "", env: Optional[Dict[str, str]] = None, out_file: str = "",
        ok=(0,), attention=(1,)) -> Dict[str, Any]:
    return {"kind": "cmd", "title": title or script, "argv": [ctx["python"], os.path.join(ctx["scripts"], script), *[str(a) for a in args]],
            "env": env or {}, "ok": list(ok), "attention": list(attention), "out_file": out_file}


def _http(title: str, method: str, url: str, auth: bool = False, save: str = "", expect=(200,), poll: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"kind": "http", "title": title, "method": method, "url": url, "auth": auth, "save": save, "expect": list(expect), "poll": poll}


def _urls_env(ctx: Dict[str, Any]) -> Dict[str, str]:
    return {"REAL_TRADE_URL": ctx["rt"], "POSITION_STOCKS_URL": ctx["ps"], "MARKET_DATA_URL": ctx["md"], "API_GATEWAY_URL": ctx["gw"]}


def _hist(ctx: Dict[str, Any], seed: int) -> str:
    return os.path.join(ctx["cal"], f"hist_{seed}")


def _ck(ctx: Dict[str, Any], name: str, seed: Any) -> List[str]:
    """group 466: --checkpoint <cal>/ckpt/<name>_<seed> (every finished symbol is saved there) and, on a Resume run, --resume (only the missing symbols are computed)."""
    return ["--checkpoint", os.path.join(ctx["cal"], "ckpt", f"{name}_{seed}")] + (["--resume"] if ctx.get("resume") else [])


# ----------------------------------------------------------------------------------------------------- builders
def b_control_snapshot(ctx, p):
    return [_py(ctx, "control_snapshot.py", "--rt-url", ctx["rt"], "--ps-url", ctx["ps"], *(["--only-changes"] if p.get("only_changes") else []),
                title="Switch snapshot (real-trade + position-stocks)", env={"TOKEN": "{token}"}, attention=(1,))]


def b_services_health(ctx, p):
    hs = [("api-gateway", ctx["gw"] + "/health"), ("market-data", ctx["md"] + "/health"), ("analysis", ctx["analysis"] + "/health"),
          ("decision/training", ctx["decision"] + "/health"), ("real-trade", ctx["rt"] + "/health"), ("position-stocks", ctx["ps"] + "/health"),
          ("notification", ctx["notify"] + "/health"), ("kronos", ctx["kronos"] + "/health"), ("timesfm", ctx["tsfm"] + "/health")]
    out = [_http(f"health: {n}", "GET", u, expect=(200,)) for n, u in hs]
    for s, (n, _u) in zip(out, hs):
        s["soft"] = True     # a service that is switched off (kronos / timesfm profile) is a note, not a failed run
        if n in ("kronos", "timesfm"):
            s["optional"] = True     # E125: no answer at all (name does not resolve) = "skipped, profile off", not a red "failed" step
    return out


def b_monday_check(ctx, p):
    env = {"GATEWAY_LOCAL_URL": ctx["gw"], "MARKET_DATA_LOCAL_URL": ctx["md"], "ANALYSIS_LOCAL_URL": ctx["analysis"], "DECISION_LOCAL_URL": ctx["decision"],
           "REAL_TRADE_LOCAL_URL": ctx["rt"], "POSITION_STOCKS_LOCAL_URL": ctx["ps"], "NOTIFICATION_LOCAL_URL": ctx["notify"]}
    return [_py(ctx, "monday_check.py", "--label", p.get("label") or "0930", "--no-docker", "--out-dir", os.path.join(ctx["run"], "monday"),
                title=f"Live checks {p.get('label') or '0930'} IST", env=env)]


def b_daily_trades(ctx, p):
    return [_py(ctx, "daily_trade_report.py", "--days", p.get("days", 10), "--rt-url", ctx["rt"], "--ps-url", ctx["ps"], "--origin", p.get("origin", "all"),
                "--out", os.path.join(ctx["run"], "daily_trades.txt"), "--csv", os.path.join(ctx["run"], "daily_trades.csv"),
                title="Daily trades: entry / exit time, P&L", env={"TOKEN": "{token}"})]


def b_service_advice(ctx, p):
    return [_py(ctx, "service_onoff_advice.py", "--days", p.get("days", 10), "--min-n", p.get("min_n", 8), "--min-days", p.get("min_days", 3),
                "--rt-url", ctx["rt"], "--ps-url", ctx["ps"], *(["--since", p["since"]] if p.get("since") else []),
                "--out", os.path.join(ctx["run"], "service_advice.txt"), "--json", os.path.join(ctx["run"], "service_advice.json"), title="Which service to keep ON / pause (recommendation only)", env={"TOKEN": "{token}"})]


def b_export_records(ctx, p):
    return [_py(ctx, "export_records.py", "--base", ctx["rt"], "--mode", p.get("mode", "REAL"), "--days", p.get("days", 60), "--include-imported",
                "--out", os.path.join(ctx["exp"], "rt_latest.json"), title="Save closed real-trade records", env={"TOKEN": "{token}"})]


def b_records_report(ctx, p):
    return [_py(ctx, "records_report.py", "--mode", p.get("mode", "REAL"), "--origin", p.get("origin", "system"), "--json", os.path.join(ctx["run"], "records_report.json"),
                os.path.join(ctx["exp"], "rt_latest.json"), title="Closed-trade report", attention=())]


def b_entry_slippage(ctx, p):
    """group 469: is paying above the signal price a material cost? Reads the saved records; changes nothing."""
    return [_py(ctx, "entry_slippage_review.py", "--mode", p.get("mode", "REAL"), "--origin", p.get("origin", "system"), "--min-n", p.get("min_n", 30),
                "--json", os.path.join(ctx["run"], "entry_slippage.json"), os.path.join(ctx["exp"], "rt_latest.json"), title="Entry slippage review", attention=(1,))]


def _snap_store(ctx):
    return os.path.join(ctx["cal"], "snapshots")


def b_calibration_snapshot(ctx, p):
    """group 469: store the switches in force (GET /control/flags of both services, effective values) plus the per-seed A/B metrics of the last run."""
    return [_py(ctx, "calibration_snapshot.py", "snapshot", "--out-dir", ctx["cal"], "--label", p.get("label") or "weekly", "--store", _snap_store(ctx),
                "--rt-url", ctx["rt"], "--ps-url", ctx["ps"], title="Calibration snapshot (switches + A/B metrics)", env={"TOKEN": "{token}"}, attention=(1,))]


def b_calibration_compare(ctx, p):
    """group 469: the two newest snapshots side by side: which switches changed, how each seed's edge / kept % / signals moved, and why the runs may not be comparable."""
    return [_py(ctx, "calibration_snapshot.py", "compare-last", "--store", _snap_store(ctx), "--json", os.path.join(ctx["run"], "snapshot_compare.json"),
                title="Compare the last two calibration snapshots", attention=(1,))]


def b_hold_bucket(ctx, p):
    return [_py(ctx, "hold_bucket_check.py", "--mode", p.get("mode", "REAL"), "--origin", p.get("origin", "system"), os.path.join(ctx["exp"], "rt_latest.json"),
                title="Hold-length buckets")]


def b_strategy_readout(ctx, p):
    return [_py(ctx, "strategy_readout.py", "--position-stocks", ctx["ps"], "--real-trade", ctx["rt"], "--days", p.get("days", 14),
                "--cutoff", p.get("cutoff", "10:30"), "--header", "Authorization: Bearer {token}", title="Opening-window verdict (entries before / after cutoff)", attention=())]


def b_opening_replay(ctx, p):
    return [_http("Save position-stocks trade ledger", "GET", ctx["ps"] + "/trades/history?limit=1000", auth=True, save=os.path.join(ctx["exp"], "ps_hist_{date}.json")),
            _py(ctx, "opening_replay.py", "--out", os.path.join(ctx["run"], "opening"), "glob:" + os.path.join(ctx["exp"], "ps_hist_*.json"),
                title="Replay the opening protection on the saved ledgers", attention=())]


def b_board_parity(ctx, p):
    return [_http("Save /board/status", "GET", ctx["ps"] + "/board/status", auth=True, save="board_status.json"),
            _py(ctx, "board_parity_check.py", "--file", os.path.join(ctx["run"], "board_status.json"), "--min-compared", p.get("min_compared", 200),
                "--min-days", p.get("min_days", 3), title="Board A/B parity (go / no-go for legacy removal)")]


def b_latency_save(ctx, p):
    return [_py(ctx, "analyze_latency_check.py", "--url", ctx["analysis"] + "/technical", "--rounds", p.get("rounds", 3), "--save", os.path.join(ctx["cal"], "latency_off.json"),
                title="G1 live: save /analyze latency baseline (run with STRUCTURE OFF)")]


def b_latency_compare(ctx, p):
    return [_py(ctx, "analyze_latency_check.py", "--url", ctx["analysis"] + "/technical", "--rounds", p.get("rounds", 3), "--compare", os.path.join(ctx["cal"], "latency_off.json"),
                "--max-increase-pct", p.get("max_increase_pct", 15), title="G1 live: compare /analyze p95 with the saved baseline")]


def _fetch(ctx, seed, p):
    # group E54: --base is the MARKET-DATA service (it serves /history/<SYM>); the gateway has no such path (only /market/history/<SYM>), so with the
    # gateway as base every symbol that was not already on disk got an instant 404 (23 Oct fetch_history: saved=0, failed 289 of 400). The symbol
    # list still comes from the gateway's /api/universe.
    args = ["--base", ctx["md"], "--universe-url", ctx["gw"] + "/api/universe", "--period", p.get("period", "5y"), "--sample", p.get("limit", 400), "--seed", seed,
            "--manifest", os.path.join(ctx["cal"], f"sample_manifest_{seed}.json"), "--sleep", "0.3", "--retries", "4"]
    return _py(ctx, "fetch_history_for_backtest.py", *args, _hist(ctx, seed), title=f"Download daily history, seed {seed} ({p.get('limit', 400)} symbols)")


def b_fetch_history(ctx, p):
    return [_fetch(ctx, int(p.get("seed", 7)), p), _py(ctx, "calibration_status.py", "--hist", _hist(ctx, int(p.get("seed", 7))), "--label", f"seed_{p.get('seed', 7)}",
                                                      title="Did the download finish?")]


def b_calibration_full(ctx, p):
    env = {"MARKET_DATA_URL": ctx["md"], "API_GATEWAY_URL": ctx["gw"], "OUT_DIR": ctx["cal"], "SEEDS": str(p.get("seeds", "7 11 13")), "LIMIT": str(p.get("limit", 400)),
           "JOBS": str(p.get("jobs", 2)), "RESUME": "1", "PS_STATUS_URL": ctx["ps"] + "/board/status", "PS_TOKEN": "{token}", "PERIOD": "5y",
           "LIVE_REPORT_URL": ctx["decision"] + "/training/api/structure/report?days=60"}
    return [{"kind": "cmd", "title": "Full structure calibration (history, A/B, G1, backtest, G2-G4, parity)", "argv": ["bash", os.path.join(ctx["scripts"], "vm_calibrate.sh")],
             "env": env, "ok": [0], "attention": [1], "out_file": ""}]


def b_calibration_status(ctx, p):
    return [_py(ctx, "calibration_status.py", "--out-dir", ctx["cal"], "--seeds", p.get("seeds", "7 11 13"), title="Did the last calibration really finish?")]


def b_ab_report(ctx, p):
    s = int(p.get("seed", 7))
    extra = ["--policy"] if p.get("policy") else []
    return [_py(ctx, "ms_ab_report.py", "--out", os.path.join(ctx["cal"], f"ab_{s}"), "--step", p.get("step", 3), "--horizon", p.get("horizon", 10), "--jobs", p.get("jobs", 2),
                *extra, *_ck(ctx, "ab", s), _hist(ctx, s), title=f"A/B replay, seed {s}", attention=())]


def b_g1_check(ctx, p):
    return [_py(ctx, "ms_g1_check.py", "--out", os.path.join(ctx["run"], "g1.json"), "--budget-ms", p.get("budget_ms", 15), _hist(ctx, int(p.get("seed", 7))),
                title="G1 offline: engine time budget")]


def b_structure_backtest(ctx, p):
    return [_py(ctx, "structure_backtest.py", "--step", p.get("step", 3), "--horizon", p.get("horizon", 10), "--jobs", p.get("jobs", 2),
                *_ck(ctx, "structure", int(p.get("seed", 7))), _hist(ctx, int(p.get("seed", 7))),
                title="Structure rules walk-forward (JSON also saved as backtest.json)", out_file=os.path.join(ctx["cal"], "backtest.json"), attention=())]


def b_gate_check(ctx, p):
    return [_http("Save live structure report (G4)", "GET", ctx["decision"] + "/training/api/structure/report?days=60", save="live_structure.json", expect=(200,)) | {"soft": True},
            _py(ctx, "structure_gate_check.py", "globflag:--ab:" + os.path.join(ctx["cal"], "ab_*", "ab_report.json"), "--live", os.path.join(ctx["run"], "live_structure.json"),
                title="G2 / G3 / G4 verdict") | {"drop_if_missing": [os.path.join(ctx["run"], "live_structure.json")]}]


def b_calibration_pack(ctx, p):
    return [_py(ctx, "calibration_pack.py", "--backtest", os.path.join(ctx["cal"], "backtest.json"), "globflag:--ab:" + os.path.join(ctx["cal"], "ab_*", "ab_report.json"),
                "--out", os.path.join(ctx["run"], "calibration_pack.md"), title="Env lines the walk-forward supports (advisory)", attention=())]


def b_legacy_gate(ctx, p):
    return [_py(ctx, "legacy_gate_backtest.py", "--out", os.path.join(ctx["run"], "legacy"), "--step", p.get("step", 3), "--jobs", p.get("jobs", 2), "--sample", p.get("sample", 200),
                *_ck(ctx, "legacy", int(p.get("seed", 7))), _hist(ctx, int(p.get("seed", 7))), title="Legacy real-trade candidate gates after costs", attention=())]


def b_entry_rules(ctx, p):
    return [_py(ctx, "entry_rules_backtest.py", "--out", os.path.join(ctx["run"], "entry_rules"), "--step", p.get("step", 3), "--jobs", p.get("jobs", 2),
                *_ck(ctx, "entry", int(p.get("seed", 7))), _hist(ctx, int(p.get("seed", 7))), title="Entry-path rules and stop / target width", attention=())]


def b_swing_backtest(ctx, p):
    # E140: --hold is a parameter now (5 = the lane's own limit). Every other hold gets its own checkpoint folder, so a 20-session run never overwrites the 5-session rows.
    hold = int(p.get("hold", 5) or 5)
    ck = "swing" if hold == 5 else f"swing_h{hold}"
    return [_py(ctx, "swing_backtest.py", "--out", os.path.join(ctx["run"], "swing"), "--step", p.get("step", 3), "--hold", hold, "--jobs", p.get("jobs", 2), "--sample", p.get("sample", 0),
                "--primary-size", p.get("primary_size", 20000), *_ck(ctx, ck, int(p.get("seed", 7))), _hist(ctx, int(p.get("seed", 7))),
                title=f"Swing momentum replay (up to {hold} sessions, delivery costs)", attention=())]


def b_swing_shadow_report(ctx, p):
    """E132 (plan S3): the screened stocks' forward returns from the service's own saved evening rankings, net of cost, against all the others. Read-only."""
    return [_py(ctx, "swing_shadow_report.py", "--service", ctx["rt"], "--out", os.path.join(ctx["run"], "swing_shadow"), "--hold", p.get("hold", 5), "--size", p.get("size", 20000),
                "--min-n", p.get("min_n", 30), "--min-days", p.get("min_days", 20), title="Swing shadow report (screened vs the rest, net of cost)", env={"TOKEN": "{token}"}, attention=(1,))]


def b_swing_universe(ctx, p):
    """E132: print SWING_UNIVERSE_SYMBOLS from the history folder the replay used (so the lane ranks the same stocks). Read-only."""
    return [_py(ctx, "swing_universe.py", _hist(ctx, int(p.get("seed", 7))), title="Swing lane universe (the replayed stocks)", attention=())]


def b_swing_probe(ctx, p):
    """E132 (plan S4): the swing probe. Places a REAL 1-share CNC order in phase open (and sells it in phase check). Needs the explicit tick; never in a pack."""
    ph = str(p.get("phase", "status"))
    args = ["--rt-url", ctx["rt"], "--phase", ph if ph in ("open", "check", "status") else "status"]
    if p.get("yes") and ph in ("open", "check"):
        args.append("--yes")
        if ph == "open" and p.get("symbol"):
            args += ["--symbol", str(p["symbol"]).strip().upper()]
    return [_py(ctx, "dhan_swing_probe.py", *args, title="Dhan swing probe (S4): 1-share CNC buy with a resting stop, read back the next morning", env={"TOKEN": "{token}"}, attention=(1,))]


def b_p8(ctx, p):
    return [_py(ctx, "p8_retest_evidence.py", "--out", os.path.join(ctx["run"], "p8"), "--jobs", p.get("jobs", 2), *_ck(ctx, "p8", int(p.get("seed", 7))), _hist(ctx, int(p.get("seed", 7))),
                title="P8 retest / CHOCH branch evidence", attention=())]


def b_ml_v2(ctx, p):
    return [_py(ctx, "ml_v2_walkforward.py", "--out", os.path.join(ctx["run"], "ml_v2"), "--jobs", p.get("jobs", 2), "--sample", p.get("sample", 200), "--no-save",
                *_ck(ctx, "ml_v2", int(p.get("seed", 7))), _hist(ctx, int(p.get("seed", 7))), title="ML V2 walk-forward (evidence only)", attention=())]


def b_intraday(ctx, p):
    return [_py(ctx, "export_intraday.py", "--out", os.path.join(ctx["run"], "intraday_rows.json"), ctx["bars"], title="Recorded 1-minute bars -> intraday rows", attention=()),
            _py(ctx, "intraday_gate_backtest.py", "--out", os.path.join(ctx["run"], "intraday"), "--horizon", p.get("horizon", 30), ctx["bars"],
                title="VWAP gate and opening gate on recorded bars", attention=())]


def b_tsfm_probe(ctx, p):
    s = int(p.get("seed", 7))
    return [_py(ctx, "tsfm_probe.py", "--out", os.path.join(ctx["cal"], f"tsfm_{s}"), "--seed", s, "--tsfm-url", ctx["tsfm"], "--max-forecasts", p.get("max_forecasts", 400),
                "--per-symbol", p.get("per_symbol", 20), _hist(ctx, s), title=f"TimesFM Gate A probe, seed {s}", attention=())]


def b_tsfm_combine(ctx, p):
    return [_py(ctx, "tsfm_probe.py", "--combine", "glob:" + os.path.join(ctx["cal"], "tsfm_*", "probe_report.json"), title="TimesFM Gate A across seeds")]


def b_tsfm_nightly(ctx, p):
    return [_py(ctx, "tsfm_nightly.py", "--url", ctx["tsfm"], "--universe-url", ctx["gw"] + "/api/universe", "--max-failed-pct", p.get("max_failed_pct", 40),
                title="Fill the TimesFM forecast cache")]


def b_kronos(ctx, p):
    s = int(p.get("seed", 7))
    return [_py(ctx, "kronos_backtest.py", "--worker", ctx["kronos"], "--seed", s, "--per-symbol", p.get("per_symbol", 5), "--max-forecasts", p.get("max_forecasts", 300),
                "--out", os.path.join(ctx["run"], "kronos"), _hist(ctx, s), title="Kronos Gate 4f backtest (bounded)", attention=())]


def b_train_model(ctx, p):
    d = ctx["decision"] + "/training"
    return [_http("Training status (before)", "GET", d + "/train/status") | {"soft": True},
            _http("Start retrain", "POST", f"{d}/train/run?label_source={p.get('label_source', 't1_outcome')}", expect=(200, 202, 409)),
            _http("Wait for the training to finish", "GET", d + "/train/status", poll={"key": "status", "until_not": ["running", "in_progress", "started", "loading_data"], "every_s": 20, "max_s": 1500}) | {"soft": True}]


def b_evaluate(ctx, p):
    d = ctx["decision"] + "/training"
    n = p.get("max_batch", 80)
    return [_http("Score T+1 outcomes", "POST", f"{d}/api/evaluate/t1?max_batch={n}"), _http("Score T+5 outcomes", "POST", f"{d}/api/evaluate/t5?max_batch={n}"),
            _http("Score structure outcomes", "POST", f"{d}/api/evaluate/structure?max_batch={n}") | {"soft": True},
            _http("Evaluation status", "GET", d + "/api/evaluate/status") | {"soft": True}]


def b_structure_refresh(ctx, p):
    q = f"force={'true' if p.get('force') else 'false'}"
    return [_http("Start structure refresh", "POST", f"{ctx['gw']}/data-feed/structure-refresh?{q}"),
            _http("Wait for it", "GET", ctx["gw"] + "/data-feed/structure-refresh/status", poll={"key": "status", "until_not": ["running"], "every_s": 30, "max_s": 1800}) | {"soft": True}]


def b_live_structure_report(ctx, p):
    return [_http("Live structure report (G4 input)", "GET", ctx["decision"] + "/training/api/structure/report?days=60", save="live_structure.json")]


def b_regime_replay(ctx, p):
    """Part 1 step 3: does the Market Weather state see wide days coming? Reads index + VIX history, changes nothing."""
    return [_py(ctx, "regime_history_replay.py", "--base", ctx["md"], "--period", p.get("period", "5y"), "--out", os.path.join(ctx["run"], "regime"),
                title="Market Weather: history replay (index + VIX)", attention=(1,))]


def b_wiring_check(ctx, p):
    """E98 P0 item 3: is Market Weather wired into both trading services right now? Reads flags and the in-force multiplier; its only write is a stamp-only
    overlay that it removes again (and it never touches a real overlay). Prints WIRING: PASS or the first failing line."""
    return [_py(ctx, "wiring_check.py", "--rt-url", ctx["rt"], "--ps-url", ctx["ps"], "--expect-mode", p.get("expect_mode", "live"),
                title="Market Weather wiring (real-trade + position-stocks)", env={"TOKEN": "{token}"}, attention=(1,))]


def b_tm_gate(ctx, p):
    """E115: is the Time Machine live gate running in both trading services? Optionally triggers a scan first (the same trigger the timer and the 09:14 weather lock use). Places nothing."""
    args = ["--rt-url", ctx["rt"], "--ps-url", ctx["ps"], "--expect-mode", p.get("expect_mode", "live"), "--min-checks", str(p.get("min_checks", 10))]
    if p.get("scan", True):
        args.append("--scan")
        if p.get("wait"):
            args.append("--wait")
    return [_py(ctx, "time_machine_gate_check.py", *args, title="Time Machine live gate (real-trade + position-stocks)", env={"TOKEN": "{token}"}, attention=(1,))]


def b_short_shadow(ctx, p):
    """E98 P0 item 7b: the shadow shorts' numbers, judged. Read-only; never touches SHORT_MODE."""
    return [_py(ctx, "short_shadow_report.py", "--ps-url", ctx["ps"], "--min-trades", str(p.get("min_trades", 100)), "--min-days", str(p.get("min_days", 20)),
                title="Short selling: shadow report (position-stocks)", env={"TOKEN": "{token}"}, attention=(1,))]


def b_short_replay(ctx, p):
    """E107 P2 item 18: the mirrored short rule replayed on recent 5-minute bars (SELL, with a BUY control). Read-only; gates nothing."""
    return [_py(ctx, "short_history_replay.py", "--base", ctx["md"], "--stop-pct", str(p.get("stop_pct", 1.0)), "--target-pct", str(p.get("target_pct", 2.0)),
                "--charges-pct", str(p.get("charges_pct", 0.08)), "--min-trades", str(p.get("min_trades", 200)), "--min-sessions", str(p.get("min_sessions", 10)),
                title="Short selling: replay on recent 5-minute bars (market-data)", attention=(1,))]


def b_sell_probe(ctx, p):
    """E108 T6: the ONLY job that places a real order. One 1-share SELL Super Order on a liquid stock, read back, cancelled, covered. Needs the explicit tick."""
    args = ["--ps-url", ctx["ps"]]
    if p.get("status_only"):
        args.append("--status")
    elif p.get("yes"):
        args.append("--yes")
        if p.get("symbol"):
            args += ["--symbol", str(p["symbol"]).strip().upper()]
    return [_py(ctx, "dhan_sell_probe.py", *args, title="Dhan SELL probe (T6): one 1-share SELL Super Order, read back and covered", env={"TOKEN": "{token}"}, attention=(1,))]


def _intraday(ctx, phase: str, title: str, extra=()):
    return [_py(ctx, "intraday_state_check.py", "--phase", phase, "--rt-url", ctx["rt"], "--ps-url", ctx["ps"], "--md-url", ctx["md"], *extra,
                title=title, env={"TOKEN": "{token}"}, attention=(1,))]


def b_shadow_shorts_check(ctx, p):
    """E126 (T5): the short shadow rows, gate counters and ORB bar counters, read-only. Skips an exchange holiday."""
    return _intraday(ctx, "shadow-shorts", "Short shadow + bar counters (T5)", ["--expect-short-mode", p.get("expect_short_mode", "any")])


def b_shorts_flat_check(ctx, p):
    """E126 (T7): no short is open after the 14:55 cover (position rows, plain-order watch list, shadow rows). Read-only."""
    return _intraday(ctx, "shorts-flat", "No short open after the 14:55 cover (T7)")


def b_overlay_clear_check(ctx, p):
    """E126: the Market Weather overlay is gone in both services after 15:30. Read-only."""
    return _intraday(ctx, "overlay-clear", "Market Weather overlay gone after 15:30")


def b_regime_shadow(ctx, p):
    """Part 1 step 6 gate: what would the Market Weather profile have done to the saved real-trade records? Reads the shadow log, changes nothing."""
    return [_py(ctx, "regime_shadow_report.py", "--log", os.path.join(os.path.dirname(ctx["exp"]), "weather", "log.jsonl"), "--out", os.path.join(ctx["run"], "regime"),
                os.path.join(ctx["exp"], "rt_latest.json"), title="Market Weather: shadow report (log + closed trades)", attention=(1,))]


def b_regime_learner(ctx, p):
    """Part 1 step 7: the slow weekly learner. PROPOSES at most one dial change from your own stamped trades; applies nothing."""
    wd = os.path.join(os.path.dirname(ctx["exp"]), "weather")
    return [_py(ctx, "regime_learner.py", "--log", os.path.join(wd, "log.jsonl"), "--history", os.path.join(wd, "learner_history.jsonl"), "--out", os.path.join(ctx["run"], "regime"),
                os.path.join(ctx["exp"], "rt_latest.json"), title="Market Weather: weekly learner (proposal only)", attention=(1,))]


def _job(id, title, group, when, what, builder, params=None, heavy=False, window="any", timeout_s=900, needs=None, after=None, auto=False, cadence=None, resumable=False):
    return {"id": id, "title": title, "group": group, "when": when, "what": what, "builder": builder, "params": params or [], "heavy": heavy,
            "window": window, "timeout_s": timeout_s, "needs": needs or [], "after": after or "", "auto_elsewhere": auto, "cadence": cadence,
            "resumable": resumable}      # resumable: the script saves each finished symbol, so the page offers Resume after a timeout / failure


SEED = P("seed", "Sample seed", "int", 7, 1, 999, help="Same seed = same random symbols. Use 7, 11, 13.")
JOBS_P = P("jobs", "Parallel workers", "int", 2, 1, 4, help="Keep low: the trading containers share this machine.")

JOB_LIST = [
    _job("control_snapshot", "Switch snapshot", "daily", "Every morning before 09:00 IST and after any switch change",
         "Writes what is ON / OFF in both services: overridden, pending restart, different from the recommended value.", b_control_snapshot,
         [P("only_changes", "Only changes", "bool", False)], timeout_s=120,
         after="PENDING RESTART lines mean a saved value is not running yet. `docker compose restart <service>` outside market hours."),
    _job("services_health", "Services health", "ops", "Any time", "GET /health of every service, including Kronos and TimesFM (those two may be switched off).", b_services_health, timeout_s=180),
    _job("monday_check_0930", "Live checks 09:30 IST", "monday", "Monday and every session at 09:30 IST",
         "Depth-20 / order-WS layout, quote age, health. Read-only.", lambda c, p: b_monday_check(c, {"label": "0930"}), window="open", timeout_s=300,
         after="Every FAIL / WARN line is one thing to look at. FAIL on quote age or session verdict: do not trust the first trades of the day."),
    _job("monday_check_1015", "Live checks 10:15 IST", "monday", "Monday and every session at 10:15 IST", "Same as 09:30, and the session verdict must read `open`.",
         lambda c, p: b_monday_check(c, {"label": "1015"}), window="open", timeout_s=300),
    _job("daily_trades", "Daily trades: entry / exit / P&L", "daily", "Every day after 15:45 IST (also useful at lunch)",
         "Every closed trade of real-trade and position-stocks per IST day: entry time, exit time, hold, P&L, reason; by entry hour, exit hour, hold, reason; best and worst.", b_daily_trades,
         [P("days", "Days", "int", 10, 1, 60), P("origin", "Entries", "choice", "all", choices=["all", "system", "imported"])], timeout_s=240,
         after="A bucket marked (few) is not a finding. Look for a bad entry hour or exit reason that repeats over several days."),
    _job("service_advice", "Service ON / OFF advice", "daily", "Every day after 15:45 IST, right after the daily trades",
         "Judges real-trade and position-stocks on their own real closed trades (net, win %, profit factor, result without the best trade, losing days in a row, overlap on the shared Dhan account) "
         "and says KEEP ON / WATCH / TURN OFF / NOT ENOUGH DATA for each. Recommends only: it never changes a switch.", b_service_advice,
         [P("days", "Days", "int", 10, 3, 60), P("min_n", "Min trades per service", "int", 8, 3, 100), P("min_days", "Min trading days", "int", 3, 1, 30),
          P("since", "Judge only from date (YYYY-MM-DD)", "str", "", help="Leave empty for the whole window. Use the day you changed switches, so the old setup does not count against the new one.")], timeout_s=240,
         after="TURN OFF means that service lost money on this window even without its best trade: pause its Auto-Pilot outside market hours, then re-run after more days. WATCH: change nothing today."),
    _job("export_records", "Save closed real-trade records", "daily", "Every day after close (records feed the weekly reports)", "Saves GET /positions/REAL/records to the exports folder.", b_export_records,
         [P("days", "Days", "int", 60, 1, 60), P("mode", "Mode", "choice", "REAL", choices=["REAL", "DEMO"])], timeout_s=240),
    _job("records_report", "Closed-trade report", "weekly", "Friday after close", "One-command report on the saved real-trade records: robust totals, outliers, groups.", b_records_report,
         [P("mode", "Mode", "choice", "REAL", choices=["REAL", "DEMO"]), P("origin", "Entries", "choice", "system", choices=["all", "system", "imported"])], timeout_s=240, needs=["export_records"]),
    _job("hold_bucket", "Hold-length buckets", "weekly", "Friday after close", "Which hold lengths make or lose money.", b_hold_bucket,
         [P("mode", "Mode", "choice", "REAL", choices=["REAL", "DEMO", "ALL"]), P("origin", "Entries", "choice", "system", choices=["all", "system", "imported"])], timeout_s=240, needs=["export_records"]),
    _job("entry_slippage", "Entry slippage review", "weekly", "Friday after close, once there are 30+ real trades",
         "Is paying above the signal price a material cost? Mean / median / worst entry slippage, rupees paid above the signal, share of gross profit, by hour and source tab, "
         "and a verdict: material / not material / too few trades. Decides whether the limit-order walk is worth building. Changes nothing.", b_entry_slippage,
         [P("mode", "Mode", "choice", "REAL", choices=["REAL", "DEMO"]), P("origin", "Entries", "choice", "system", choices=["all", "system", "imported"]),
          P("min_n", "Min trades for a verdict", "int", 30, 5, 500)], timeout_s=240, needs=["export_records"],
         after="Build the adaptive limit-order walk only if the verdict is `material` on a sample this size. `too_few_trades`: wait."),
    _job("calibration_snapshot", "Calibration snapshot", "weekly", "After a finished weekly calibration, and before / after any settings change",
         "Stores the switches in force in both services (effective values) together with the per-seed A/B verdict, edge, CI and signal counts of the last calibration, "
         "so two runs can be compared. Read-only.", b_calibration_snapshot, [P("label", "Label", "str", "weekly", help="e.g. before-chase-change")], timeout_s=180,
         needs=["calibration_full"], after="Take one before and one after a settings change, then run `Compare the last two snapshots`."),
    _job("calibration_compare", "Compare the last two snapshots", "weekly", "After the second snapshot of a before / after pair",
         "Which switches changed and how each seed's edge, kept % and signal counts moved. Warns when the seeds differ, a seed is incomplete or no switch changed. "
         "A change in edge inside the bootstrap CI is noise. Applies nothing.", b_calibration_compare, timeout_s=60),
    _job("strategy_readout", "Opening-window verdict", "weekly", "Friday after close", "Entries before the cutoff against later ones, from the live 14-day reports: SET / KEEP / NOT ENOUGH DATA.", b_strategy_readout,
         [P("days", "Days", "int", 14, 3, 60), P("cutoff", "Cutoff IST", "str", "10:30")], timeout_s=240),
    _job("opening_replay", "Opening protection replay", "weekly", "Every day after close (the ledger keeps only ~3 days)", "Saves today's position-stocks ledger and replays the opening protection over every saved ledger.", b_opening_replay, timeout_s=300),
    _job("board_parity", "Board A/B parity", "weekly", "Friday after close", "GO / NO_GO for removing the legacy Market Board maths. It changes nothing.", b_board_parity,
         [P("min_compared", "Min comparisons", "int", 200, 50, 5000), P("min_days", "Min IST days", "int", 3, 1, 30)], timeout_s=120,
         after="GO on two weeks in a row: then run scripts/retire_legacy_board.py on the VM (dry run first). NO_GO: leave it."),
    _job("latency_save", "G1 live: baseline (structure OFF)", "monthly", "Only when you re-test G1: with STRUCTURE_ENABLED=0 on analysis", "Times /analyze with the structure engine off and saves it.", b_latency_save,
         [P("rounds", "Rounds", "int", 3, 2, 6)], window="closed", timeout_s=600, heavy=True),
    _job("latency_compare", "G1 live: compare (structure ON)", "monthly", "After switching structure back on", "p95 of /analyze must be within 15% of the saved baseline.", b_latency_compare,
         [P("rounds", "Rounds", "int", 3, 2, 6), P("max_increase_pct", "Max increase %", "int", 15, 1, 100)], window="closed", timeout_s=600, heavy=True, needs=["latency_save"]),
    _job("fetch_history", "Download daily history (one seed)", "weekly", "Saturday morning", "Daily candles for a random sample of the universe. Needed by every backtest below.", b_fetch_history,
         [SEED, P("limit", "Symbols", "int", 400, 50, 800), P("period", "Period", "choice", "5y", choices=["2y", "5y", "10y"])], heavy=True, window="closed", timeout_s=3600),
    _job("calibration_full", "FULL calibration (all seeds, G1-G4, backtest, parity)", "weekly", "Saturday 08:00 IST (about 1-3 hours)",
         "Runs scripts/vm_calibrate.sh: history per seed, A/B, G1, walk-forward, G2/G3/G4, env-line pack, parity. Resumes seeds already complete.", b_calibration_full,
         [P("seeds", "Seeds", "str", "7 11 13"), P("limit", "Symbols per seed", "int", 400, 50, 800), P("jobs", "Parallel workers", "int", 2, 1, 4)], heavy=True, window="closed", timeout_s=HEAVY_S, resumable=True,
         after="Read summary.txt. A FAIL or NOT_ENOUGH_DATA is a reason to keep kill switches ready, never a tuning instruction."),
    _job("calibration_status", "Did the calibration finish?", "weekly", "After the full calibration", "Checks that every seed has enough symbols and a real A/B verdict.", b_calibration_status, [P("seeds", "Seeds", "str", "7 11 13")], timeout_s=120),
    _job("ab_report", "A/B replay (one seed)", "weekly", "After fetch_history", "Does the structure policy beat a technical-only baseline, out of sample, after costs? (G2)", b_ab_report,
         [SEED, P("step", "Step", "int", 3, 1, 10), P("horizon", "Horizon (days)", "choice", "10", choices=["1", "5", "10", "20"]), JOBS_P, P("policy", "Also replay BUY NOW rules", "bool", False)],
         heavy=True, window="closed", timeout_s=HEAVY_S, resumable=True, needs=["fetch_history"]),
    _job("g1_check", "G1 offline: engine time", "monthly", "After fetch_history", "Is the engine fast enough on real history?", b_g1_check, [SEED, P("budget_ms", "Budget ms", "int", 15, 10, 500)], heavy=True, window="closed", timeout_s=1800, needs=["fetch_history"]),
    _job("structure_backtest", "Structure rules walk-forward", "weekly", "After fetch_history", "Which gate thresholds are stable out of sample.", b_structure_backtest,
         [SEED, P("step", "Step", "int", 3, 1, 10), P("horizon", "Horizon (days)", "choice", "10", choices=["1", "5", "10", "20"]), JOBS_P], heavy=True, window="closed", timeout_s=HEAVY_S, resumable=True, needs=["fetch_history"]),
    _job("gate_check", "G2 / G3 / G4 verdict", "weekly", "After every A/B report", "Reads every seed's A/B report and the live structure report.", b_gate_check, timeout_s=300,
         after="PASS on all gates: structure rules may be trusted in REAL mode. FAIL / NOT_ENOUGH_DATA: keep the kill switches ready (STRUCTURE_POLICY_MODE / GATE_MODE / STOP_MODE off)."),
    _job("calibration_pack", "Env lines from the walk-forward", "monthly", "After the walk-forward", "Advisory env lines for the four gate thresholds. Applies nothing.", b_calibration_pack, timeout_s=120),
    _job("legacy_gate", "Legacy candidate gates after costs", "monthly", "Monthly, after fetch_history", "Did each legacy real-trade gate remove the worse entries?", b_legacy_gate, [SEED, JOBS_P, P("sample", "Symbols", "int", 200, 50, 800), P("step", "Step", "int", 3, 1, 10)],
         heavy=True, window="closed", timeout_s=HEAVY_S, resumable=True, needs=["fetch_history"]),
    _job("entry_rules", "Entry-path rules and stop width", "monthly", "Monthly, after fetch_history", "Did the entry rules and the stop / target width help after costs?", b_entry_rules, [SEED, JOBS_P, P("step", "Step", "int", 3, 1, 10)],
         heavy=True, window="closed", timeout_s=HEAVY_S, resumable=True, needs=["fetch_history"]),
    _job("swing_backtest", "Swing momentum replay (E131)", "monthly", "On demand, after fetch_history", "Would holding top-momentum stocks for up to 5 sessions (or 10-60 with the Hold parameter) have paid after delivery costs, at your trade size? Evidence only: nothing is traded or switched.", b_swing_backtest,
         [SEED, JOBS_P, P("sample", "Symbols (0 = all)", "int", 0, 0, 800), P("step", "Step", "int", 3, 1, 10), P("primary_size", "Trade size for the verdict (Rs)", "int", 20000, 1000, 500000),
          P("hold", "Hold (sessions)", "choice", "5", choices=["5", "10", "20", "40", "60"], help="5 = the swing lane's own limit. 20-60 is the research question (E140): momentum is a monthly signal, so does a longer hold beat the cost? Replay only; the lane keeps SWING_MAX_HOLD_DAYS.")],
         heavy=True, window="closed", timeout_s=HEAVY_S, resumable=True, needs=["fetch_history"],
         after="USEFUL = worth building the shadow lane (plan step S3). NO_EDGE = do not build it. INSUFFICIENT = the history folder is too small. A gross edge eaten by costs says which trade size to retest."),
    _job("swing_shadow_report", "Swing shadow report (E132)", "weekly", "Weekly while SWING_MODE is shadow or live", "Reads the service's saved evening rankings: did the screened stocks beat the rest after the screening day, net of delivery cost? Verdict USEFUL / NO_EDGE / NOT_ENOUGH_DATA. Changes nothing and never touches SWING_MODE.", b_swing_shadow_report,
         [P("hold", "Hold (sessions)", "choice", "5", choices=["1", "3", "5", "10", "20"], help="10 / 20 need 30+ / 40+ saved evening rankings before they can answer (E141)."), P("size", "Trade size (Rs)", "int", 20000, 1000, 500000), P("min_n", "Screened entries needed", "int", 30, 1, 1000), P("min_days", "Days needed", "int", 20, 1, 120)], timeout_s=180,
         after="NOT_ENOUGH_DATA: keep collecting. USEFUL: 30+ net-positive shadow trades is the gate to run the S4 probe; SWING_MODE=live stays your decision. NO_EDGE: do not go live. The exact lane trades (stop, ratchet) are at GET /swing/report."),
    _job("swing_universe", "Swing lane universe (E132)", "ops", "Once, before SWING_MODE is shadow (and after fetch_history grows the folder)", "Prints the SWING_UNIVERSE_SYMBOLS line from the history folder the replay used, so the lane ranks the same stocks the replay tested. Paste it into .env.", b_swing_universe, [SEED], timeout_s=60,
         after="Copy the SWING_UNIVERSE_SYMBOLS line into .env and restart real-trade-service."),
    _job("dhan_swing_probe", "Dhan swing probe (S4) - places 1 real share", "ops", "Open phase 09:30-14:30 IST on one trading day, check phase on the NEXT trading morning, before SWING_BROKER_STOP_ENABLED or live REAL swing", "PLACES ONE REAL 1-share CNC BUY with a resting stop in phase open; phase check reads the stop back the next morning (PASS only if it survived the night), cancels it and sells the share. Nothing is sent unless you tick the box. Selling needs the CDSL TPIN that morning.", b_swing_probe,
         [P("phase", "Phase", "choice", "status", choices=["status", "open", "check"]), P("yes", "I understand this places a real 1-share order", "bool", False), P("symbol", "Symbol (open only; blank = ITC, NTPC, ONGC, SBIN, TATASTEEL in turn)", "str", "")], window="open", timeout_s=300,
         after="PASS: a resting stop on a CNC holding survives the night: SWING_BROKER_STOP_ENABLED is now a safe switch. FAIL: read the step lines; keep it off and rely on the software stop."),
    _job("p8_retest", "P8 retest / CHOCH evidence", "monthly", "Monthly", "Is the retest branch worth switching on (STRUCTURE_POLICY retest_entry=1)?", b_p8, [SEED, JOBS_P], heavy=True, window="closed", timeout_s=HEAVY_S, resumable=True, needs=["fetch_history"]),
    _job("ml_v2", "ML V2 walk-forward", "monthly", "Monthly", "Does a model on the V2 structure features beat chance out of sample? Evidence only: nothing is trained into production.", b_ml_v2, [SEED, JOBS_P, P("sample", "Symbols", "int", 200, 50, 800)],
         heavy=True, window="closed", timeout_s=HEAVY_S, resumable=True, needs=["fetch_history"]),
    _job("intraday_gates", "Intraday VWAP / opening gates", "monthly", "Monthly, after >= 10 recorded sessions", "Backtests the VWAP gate and the opening gate on the 1-minute bars position-stocks recorded.", b_intraday, [P("horizon", "Horizon (min)", "choice", "30", choices=["15", "30", "60"])],
         heavy=True, window="closed", timeout_s=3600),
    _job("tsfm_nightly", "TimesFM nightly cache", "daily", "Every day after 16:30 IST (daily bars in)", "Fills the TimesFM forecast cache. Needs the timesfm compose profile.", b_tsfm_nightly, [P("max_failed_pct", "Max failed %", "int", 40, 1, 100)], heavy=True, window="closed", timeout_s=3600),
    _job("tsfm_probe", "TimesFM Gate A probe (one seed)", "monthly", "Monthly, one seed at a time", "Does TimesFM predict the 5-day range / volume better than ATR / HAR? Out of sample.", b_tsfm_probe,
         [SEED, P("max_forecasts", "Max forecasts", "int", 400, 20, 5000), P("per_symbol", "Days per symbol", "int", 20, 5, 100)], heavy=True, window="closed", timeout_s=HEAVY_S, needs=["fetch_history"]),
    _job("tsfm_combine", "TimesFM Gate A across seeds", "monthly", "After 2-3 probe seeds", "Gate A verdict over every probe report.", b_tsfm_combine, timeout_s=120, needs=["tsfm_probe"]),
    _job("kronos_backtest", "Kronos Gate 4f backtest", "monthly", "Monthly (needs the Kronos container)", "Would the Kronos gate have helped? Bounded number of forecasts.", b_kronos,
         [SEED, P("per_symbol", "Days per symbol", "int", 5, 1, 50), P("max_forecasts", "Max forecasts", "int", 300, 20, 3000)], heavy=True, window="closed", timeout_s=HEAVY_S, needs=["fetch_history"]),
    _job("train_model", "Retrain the prediction model", "model", "Only when you want it now. It is ALREADY retrained 00:30 IST by GitHub Actions", "Starts POST /training/train/run on decision-prediction and waits for it.", b_train_model,
         [P("label_source", "Label source", "choice", "t1_outcome", choices=["t1_outcome", "trade_pnl"])], heavy=True, window="closed", timeout_s=1800, auto=True,
         after="409 means a training is already running: the nightly one, or this one started twice."),
    _job("evaluate_outcomes", "Score pending outcomes (T+1 / T+5 / structure)", "model", "Every day after close (also runs by GitHub Actions)", "Scores yesterday's predictions so the model has fresh labels.", b_evaluate, [P("max_batch", "Max batch", "int", 80, 10, 400)],
         timeout_s=900, auto=True),
    _job("structure_refresh", "Structure refresh", "model", "Every day after close (also runs by GitHub Actions)", "Re-reads daily structure rows for the universe.", b_structure_refresh, [P("force", "Force re-read", "bool", False)], window="closed", heavy=True, timeout_s=2400, auto=True),
    _job("regime_history_replay", "Market Weather history replay", "weekly", "Once after the regime package is deployed, then monthly",
         "Replays the state classifier over 5 years of index + VIX history: is the state worse on days that turned out wider, and does it beat yesterday's ATR alone? "
         "Verdict USEFUL / USEFUL_NOT_BEYOND_ATR / NO_EDGE / NOT_ENOUGH_DATA. Tests the index only, not your trades. Changes nothing.", b_regime_replay,
         [P("period", "History", "choice", "5y", choices=["2y", "5y", "10y"])], timeout_s=900,
         after="USEFUL: carry on to the shadow run. NO_EDGE or NOT_ENOUGH_DATA: do not switch Market Weather to live; the classifier needs rework first."),
    _job("wiring_check", "Market Weather wiring check", "ops", "After every deploy, and any time you doubt the layer is on (market hours show the live multipliers best)",
         "For real-trade and position-stocks: the mode switch, every overlay dial exists and is live, a stamp-only overlay is accepted, read back and removed, the in-force multiplier "
         "matches the switch, and the short shadow endpoint answers. Never touches an overlay that is already active.", b_wiring_check,
         [P("expect_mode", "Expected mode", "choice", "live", choices=["live", "shadow", "off", "any"])], timeout_s=180,
         after="WIRING: PASS = the layer can reach both services. FAIL: the first failing line says which service and which link is missing; fix that before trusting a quiet morning."),
    _job("tm_gate_check", "Time Machine live gate check", "ops", "Every trading morning at 09:20 (scheduled), and any time you doubt the gate is running; the gate itself also runs on its own timer",
         "Triggers a scan (fresh Time Machine verdicts for every pick the two services are watching, for the current entry bar), then reads each service: mode, worker + timer threads, "
         "checks, refusals, refusal rate. FAIL if a service does not answer, is not in the expected mode, has no threads, or refuses more than half of at least 10 checks. Places nothing; never changes a switch.", b_tm_gate,
         [P("scan", "Trigger a scan first", "bool", True), P("wait", "Wait for the verdicts", "bool", False), P("expect_mode", "Expected mode", "choice", "live", choices=["live", "log", "off", "any"]),
          P("min_checks", "Checks needed before judging the refusal rate", "int", 10, 1, 500)], timeout_s=180,
         after="PASS: the gate is on in both services. NOT_ENOUGH_DATA: wired, no entries yet. FAIL: the line names the service; a high refusal rate means set TM_GATE_MODE=log (one switch) and read recent_checks."),
    _job("short_shadow_report", "Short selling shadow report", "weekly", "Weekly while SHORT_MODE is shadow (and before any move to live)",
         "Reads the shadow shorts from position-stocks and judges the numbers: do the counters agree, and once there are enough closed would-be shorts over enough days, "
         "is the net P&L after charges above zero. Verdict PASS / FAIL / NOT_ENOUGH_DATA. Changes nothing and never touches SHORT_MODE.", b_short_shadow,
         [P("min_trades", "Closed would-be shorts needed", "int", 100, 1, 1000), P("min_days", "Days with results needed", "int", 20, 1, 120)], timeout_s=120,
         after="NOT_ENOUGH_DATA: keep collecting, nothing to do. PASS: the shadow shorts earn after charges; SHORT_MODE stays your decision. FAIL: do not move to live; read the gate counters and the recent rows first."),
    _job("dhan_sell_probe", "Dhan SELL probe (T6) - places 1 real share", "ops", "Once, in market hours 09:30-14:30 IST, before SHORT_MODE goes live (and again after any Dhan change)",
         "PLACES ONE REAL 1-share INTRADAY SELL Super Order on a liquid stock, reads Dhan's stop and target legs back, cancels the legs, covers with a 1-share BUY and reads the cover back. "
         "Records Dhan's answers and a verdict PASS / FAIL. A live short is refused until this has a PASS (SHORT_LIVE_REQUIRES_PROBE). Nothing is sent unless you tick the box. "
         "If the cover cannot be completed the short is adopted as a normal short position and Telegram shouts.", b_sell_probe,
         [P("yes", "I understand this places a real 1-share order", "bool", False), P("symbol", "Symbol (blank = ITC, NTPC, ONGC, SBIN, TATASTEEL in turn)", "str", ""),
          P("status_only", "Only show the last result", "bool", False)], window="open", timeout_s=240,
         after="PASS: SHORT_MODE=live is now one switch (start with SHORT_LIVE_QTY_CAP=1). FAIL: read the 'dhan' lines, they say what Dhan refused or what did not read back; SHORT_MODE stays shadow."),
    _job("shadow_shorts_check", "Short shadow + bar counters (T5)", "ops", "Every trading day 09:35 IST (scheduled), or any time you doubt the short shadow is running",
         "Reads the short shadow (mode, gate counters, would-be shorts, last SELL probe verdict) and the real-trade ORB bar counters (hit / miss / stale). FAIL if a service does not answer, SHORT_MODE is not off / shadow / live "
         "(or not the one you expect), or the gate counters are missing. Skips an exchange holiday. Places nothing.", b_shadow_shorts_check,
         [P("expect_short_mode", "Expected SHORT_MODE", "choice", "any", choices=["any", "off", "shadow", "live"])], timeout_s=120,
         after="PASS: the shadow is recording. WARN lines are things to look at, not failures."),
    _job("shorts_flat_check", "No short open after the 14:55 cover (T7)", "ops", "Every trading day 15:05 IST (scheduled), and right after the first live short day",
         "Reads position-stocks: no SHORT position is still OPEN, no plain-order SELL is still watched, no shadow short is stuck OPEN. FAIL names the symbol: check Dhan and cover by hand if needed. Skips an exchange holiday. Places nothing.",
         b_shorts_flat_check, timeout_s=120, after="PASS: the day ended flat. FAIL: the line names what is still open."),
    _job("overlay_clear_check", "Market Weather overlay gone after 15:30", "ops", "Every trading day 15:35 IST (scheduled)",
         "Reads GET /control/overlay of real-trade and position-stocks: after 15:30 IST no overlay may be active, so every dial is back at its saved value. FAIL names the service. Before 15:30 an active overlay is only an INFO line. Skips an exchange holiday.",
         b_overlay_clear_check, timeout_s=120, after="PASS: tomorrow starts from the saved values. FAIL: use 'Revert to saved' on the Market Weather card."),
    _job("short_history_replay", "Short selling bar replay", "weekly", "Any time, minutes; weekly while SHORT_MODE is shadow or live (evidence only)",
         "Pretends to SELL at fixed times of day on recent 5-minute bars of liquid NSE stocks, walks the stop and the target (stop first when both are touched) and the 14:55 cover, "
         "after flat charges, and runs the same for BUY as a control. Break-even aware: at 2:1 a short only needs to win 1 trade in 3. Verdict PASS / FAIL / NOT_ENOUGH_DATA. Changes nothing and gates nothing.",
         b_short_replay, [P("stop_pct", "Stop %", "float", 1.0, 0.2, 5.0), P("target_pct", "Target %", "float", 2.0, 0.2, 10.0), P("charges_pct", "Charges % (round trip)", "float", 0.08, 0.0, 1.0),
                          P("min_trades", "Replays needed", "int", 200, 20, 5000), P("min_sessions", "Stock-sessions needed", "int", 10, 1, 120)], timeout_s=600,
         after="NOT_ENOUGH_DATA: market-data did not serve enough 5-minute history; nothing to act on. PASS: the mirrored short rule earns on recent bars. FAIL: read the BUY control line first; if the long mirror earned, the window was a rising market."),
    _job("regime_shadow_report", "Market Weather shadow report", "weekly", "Weekly, after the shadow weeks have run (needs the saved real-trade records)",
         "Replays the Market Weather profile on your closed real-trade records, using the state the shadow log says was in force at each entry: which trades it would have skipped, "
         "and whether the skipped ones were WORSE than the average trade of those days (not merely fewer trades). Verdict PASS / FAIL / NOT_ENOUGH_DATA. Changes nothing.", b_regime_shadow,
         timeout_s=300, needs=["export_records"],
         after="PASS is the gate for step 6 (live in DEMO), not an automatic switch. FAIL or NOT_ENOUGH_DATA: keep MARKET_STATE_MODE=shadow."),
    _job("regime_learner", "Market Weather weekly learner", "weekly", "Weekly, only after the shadow report has PASSed and 90+ trades carry a state",
         "Groups your closed real-trade records by market state and time of day, forgets old trades gradually, shrinks each group toward your all-trade average, and applies a confidence bar "
         "that rises with the number of groups tested. Proposes AT MOST ONE dial change per run; a loosening is only confirmed when the previous run proposed the identical change. "
         "Verdict PROPOSE_TIGHTEN / PROPOSE_LOOSEN / NO_CHANGE / NOT_ENOUGH_DATA. Changes nothing.", b_regime_learner,
         timeout_s=300, needs=["export_records"],
         after="A CONFIRMED proposal is for you to read, then change by hand. Nothing here is applied, and none of these dials is in the calibrator's rules."),
    _job("live_structure_report", "Live structure report (G4)", "ops", "Any time", "Saves GET /api/structure/report?days=60: the live observations G4 uses.", b_live_structure_report, timeout_s=120),
]
JOBS: Dict[str, Dict[str, Any]] = {j["id"]: j for j in JOB_LIST}

# Packs = ordered lists of job ids with fixed params. The page shows them as one button each.
PACKS: List[Dict[str, Any]] = [
    {"id": "time_machine_open", "title": "Time Machine morning scan", "when": "Every trading day 09:20 IST (scheduled), after the Market Weather lock",
     "what": "Triggers a Time Machine scan in real-trade and position-stocks and checks that the live gate is running in both.", "jobs": [["tm_gate_check", {"scan": True}]], "stop_on_failure": False},
    {"id": "monday_open", "title": "Monday open pack", "when": "Monday 08:45-09:30 IST, and any day after a restart or deploy",
     "what": "Health, switch snapshot, Market Weather wiring check, Time Machine gate check. (The 09:35 and 10:20 live checks are their own scheduled packs.)",
     "jobs": [["services_health", {}], ["control_snapshot", {}], ["wiring_check", {}], ["tm_gate_check", {"scan": True}]], "stop_on_failure": False},
    {"id": "morning_open", "title": "Morning open pack", "when": "Tuesday to Friday 08:50 IST (scheduled); Monday uses the Monday open pack",
     "what": "Health, switch snapshot and the Market Weather wiring check, every trading morning, so a mid-week deploy is caught before the 09:14 lock.",
     "jobs": [["services_health", {}], ["control_snapshot", {}], ["wiring_check", {}]], "stop_on_failure": False},
    {"id": "live_check_open", "title": "Live check 09:35 + short shadow (T5)", "when": "Every trading day 09:35 IST (scheduled)",
     "what": "The 09:30 live check (depth-20 / order feed layout, quote age) once the market is really open, then the short shadow and ORB bar counters.",
     "jobs": [["monday_check_0930", {}], ["shadow_shorts_check", {}]], "stop_on_failure": False},
    {"id": "live_check_1015", "title": "Live check 10:20 (session must read open)", "when": "Every trading day 10:20 IST (scheduled)",
     "what": "The 10:15 live check: the session verdict must read `open`.", "jobs": [["monday_check_1015", {}]], "stop_on_failure": False},
    {"id": "shorts_flat_close", "title": "Shorts flat check (T7)", "when": "Every trading day 15:05 IST (scheduled), after the 14:55 cover",
     "what": "No short may still be open after the 14:55 cover.", "jobs": [["shorts_flat_check", {}]], "stop_on_failure": False},
    {"id": "overlay_clear_close", "title": "Overlay cleared check", "when": "Every trading day 15:35 IST (scheduled), after the 15:30 end of the Market Weather overlay",
     "what": "The Market Weather overlay must be gone in both services.", "jobs": [["overlay_clear_check", {}]], "stop_on_failure": False},
    {"id": "daily_close", "title": "Daily close pack", "when": "Every trading day after 15:45 IST",
     "what": "Daily trades with entry / exit time and P&L, which service to keep ON / pause (advice only), save records, save the position-stocks ledger, score outcomes, structure refresh, TimesFM cache.",
     "jobs": [["daily_trades", {"days": 10}], ["service_advice", {"days": 10}], ["export_records", {}], ["opening_replay", {}], ["evaluate_outcomes", {}], ["structure_refresh", {}], ["tsfm_nightly", {}]], "stop_on_failure": False},
    {"id": "friday_review", "title": "Friday review pack", "when": "Friday after close",
     "what": "Closed-trade report, entry slippage review, hold buckets, opening-window verdict, board parity, short shadow report, swing shadow report (E138: a weekly job that no pack ran), switch snapshot of changes.",
     "jobs": [["export_records", {}], ["records_report", {}], ["entry_slippage", {}], ["hold_bucket", {}], ["strategy_readout", {}], ["board_parity", {}], ["short_shadow_report", {}], ["swing_shadow_report", {}], ["control_snapshot", {"only_changes": True}]], "stop_on_failure": False},
    {"id": "weekly_calibration", "title": "Weekly calibration pack", "when": "Saturday 08:00 IST (market closed, 1-3 hours)",
     "what": "The full calibration (history, A/B, G1, walk-forward, G2-G4, parity), the completeness check, then a snapshot of the switches + metrics and its comparison with the previous run.",
     "jobs": [["calibration_full", {}], ["calibration_status", {}], ["calibration_snapshot", {}], ["calibration_compare", {}]], "stop_on_failure": False},
    {"id": "monthly_evidence", "title": "Monthly evidence pack", "when": "First Saturday of the month (market closed, several hours)",
     "what": "Legacy gates, entry rules, P8 retest, ML V2, intraday gates, TimesFM probe + combine, Kronos backtest. Needs a finished weekly calibration (history on disk).",
     "jobs": [["legacy_gate", {}], ["entry_rules", {}], ["p8_retest", {}], ["ml_v2", {}], ["intraday_gates", {}], ["tsfm_probe", {}], ["tsfm_combine", {}], ["kronos_backtest", {}]], "stop_on_failure": False},
]
PACK_MAP = {p["id"]: p for p in PACKS}

# Default timetable (IST) the optional scheduler uses once switched on. weekday: 0=Mon .. 6=Sun
SCHEDULE_DEFAULT: List[Dict[str, Any]] = [
    {"id": "s_monday_open", "pack": "monday_open", "days": [0], "time": "08:50", "enabled": True},
    {"id": "s_tm_open", "pack": "time_machine_open", "days": [0, 1, 2, 3, 4], "time": "09:20", "enabled": True},
    {"id": "s_morning_open", "pack": "morning_open", "days": [1, 2, 3, 4], "time": "08:50", "enabled": True},
    {"id": "s_live_open", "pack": "live_check_open", "days": [0, 1, 2, 3, 4], "time": "09:35", "enabled": True},
    {"id": "s_live_1015", "pack": "live_check_1015", "days": [0, 1, 2, 3, 4], "time": "10:20", "enabled": True},
    {"id": "s_shorts_flat", "pack": "shorts_flat_close", "days": [0, 1, 2, 3, 4], "time": "15:05", "enabled": True},
    {"id": "s_overlay_clear", "pack": "overlay_clear_close", "days": [0, 1, 2, 3, 4], "time": "15:35", "enabled": True},
    {"id": "s_daily_close", "pack": "daily_close", "days": [0, 1, 2, 3, 4], "time": "16:05", "enabled": True},
    {"id": "s_friday_review", "pack": "friday_review", "days": [4], "time": "16:40", "enabled": True},
    {"id": "s_weekly_cal", "pack": "weekly_calibration", "days": [5], "time": "08:00", "enabled": False},
    {"id": "s_monthly", "pack": "monthly_evidence", "days": [5], "time": "13:00", "enabled": False, "first_week_only": True},
]


def coerce_params(job: Dict[str, Any], given: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate and bound the parameters; unknown names are rejected so nothing unexpected reaches a command line."""
    given = dict(given or {})
    out: Dict[str, Any] = {}
    spec = {p["name"]: p for p in job["params"]}
    extra = set(given) - set(spec)
    if extra:
        raise ValueError(f"unknown parameter(s): {', '.join(sorted(extra))}")
    for name, p in spec.items():
        v = given.get(name, p["default"])
        k = p["kind"]
        if k == "int":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{name}: must be a whole number")
            if p["min"] is not None and v < p["min"] or p["max"] is not None and v > p["max"]:
                raise ValueError(f"{name}: must be between {p['min']} and {p['max']}")
        elif k == "bool":
            v = v in (True, 1, "1", "true", "True", "yes", "on")
        elif k == "choice":
            v = str(v)
            if v not in p["choices"]:
                raise ValueError(f"{name}: must be one of {', '.join(p['choices'])}")
        else:
            v = str(v).strip()
            if len(v) > 60 or not all(c.isalnum() or c in " :.,-_" for c in v):
                raise ValueError(f"{name}: letters, digits, space and : . , - _ only (max 60)")
        out[name] = v
    return out


def public_catalog() -> Dict[str, Any]:
    jobs = [{k: v for k, v in j.items() if k != "builder"} for j in JOB_LIST]
    return {"groups": [{"id": g, "title": t} for g, t in GROUPS], "jobs": jobs, "packs": PACKS, "schedule_default": SCHEDULE_DEFAULT}
