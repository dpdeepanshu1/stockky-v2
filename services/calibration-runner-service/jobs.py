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
    for s in out:
        s["soft"] = True     # a service that is switched off (kronos / timesfm profile) is a note, not a failed run
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
    args = ["--base", ctx["gw"], "--universe-url", ctx["gw"] + "/api/universe", "--period", p.get("period", "5y"), "--sample", p.get("limit", 400), "--seed", seed,
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
    return [_py(ctx, "structure_backtest.py", "--step", p.get("step", 3), "--horizon", p.get("horizon", 10), _hist(ctx, int(p.get("seed", 7))),
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
         [SEED, P("step", "Step", "int", 3, 1, 10), P("horizon", "Horizon (days)", "choice", "10", choices=["1", "5", "10", "20"])], heavy=True, window="closed", timeout_s=HEAVY_S, needs=["fetch_history"]),
    _job("gate_check", "G2 / G3 / G4 verdict", "weekly", "After every A/B report", "Reads every seed's A/B report and the live structure report.", b_gate_check, timeout_s=300,
         after="PASS on all gates: structure rules may be trusted in REAL mode. FAIL / NOT_ENOUGH_DATA: keep the kill switches ready (STRUCTURE_POLICY_MODE / GATE_MODE / STOP_MODE off)."),
    _job("calibration_pack", "Env lines from the walk-forward", "monthly", "After the walk-forward", "Advisory env lines for the four gate thresholds. Applies nothing.", b_calibration_pack, timeout_s=120),
    _job("legacy_gate", "Legacy candidate gates after costs", "monthly", "Monthly, after fetch_history", "Did each legacy real-trade gate remove the worse entries?", b_legacy_gate, [SEED, JOBS_P, P("sample", "Symbols", "int", 200, 50, 800), P("step", "Step", "int", 3, 1, 10)],
         heavy=True, window="closed", timeout_s=HEAVY_S, resumable=True, needs=["fetch_history"]),
    _job("entry_rules", "Entry-path rules and stop width", "monthly", "Monthly, after fetch_history", "Did the entry rules and the stop / target width help after costs?", b_entry_rules, [SEED, JOBS_P, P("step", "Step", "int", 3, 1, 10)],
         heavy=True, window="closed", timeout_s=HEAVY_S, resumable=True, needs=["fetch_history"]),
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
    _job("live_structure_report", "Live structure report (G4)", "ops", "Any time", "Saves GET /api/structure/report?days=60: the live observations G4 uses.", b_live_structure_report, timeout_s=120),
]
JOBS: Dict[str, Dict[str, Any]] = {j["id"]: j for j in JOB_LIST}

# Packs = ordered lists of job ids with fixed params. The page shows them as one button each.
PACKS: List[Dict[str, Any]] = [
    {"id": "monday_open", "title": "Monday open pack", "when": "Monday 08:45-09:30 IST, and any day after a restart or deploy",
     "what": "Health, switch snapshot, then the 09:30 live check. (Run the 10:15 check from the list after 10:15.)",
     "jobs": [["services_health", {}], ["control_snapshot", {}], ["monday_check_0930", {}]], "stop_on_failure": False},
    {"id": "daily_close", "title": "Daily close pack", "when": "Every trading day after 15:45 IST",
     "what": "Daily trades with entry / exit time and P&L, which service to keep ON / pause (advice only), save records, save the position-stocks ledger, score outcomes, structure refresh, TimesFM cache.",
     "jobs": [["daily_trades", {"days": 10}], ["service_advice", {"days": 10}], ["export_records", {}], ["opening_replay", {}], ["evaluate_outcomes", {}], ["structure_refresh", {}], ["tsfm_nightly", {}]], "stop_on_failure": False},
    {"id": "friday_review", "title": "Friday review pack", "when": "Friday after close",
     "what": "Closed-trade report, entry slippage review, hold buckets, opening-window verdict, board parity, switch snapshot of changes.",
     "jobs": [["export_records", {}], ["records_report", {}], ["entry_slippage", {}], ["hold_bucket", {}], ["strategy_readout", {}], ["board_parity", {}], ["control_snapshot", {"only_changes": True}]], "stop_on_failure": False},
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
