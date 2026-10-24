"""timemachine/service.py - E111: the Time Machine as a service: validate a request, load the real 5-minute sessions, replay the rule on them, simulate
thousands of made-up sessions, and return one JSON-ready answer. Read-only and evidence only: no order, no position, no database write.

Settings are read from config at CALL time (TIME_MACHINE_ENABLED / _SIMS / _SLIPPAGE_PCT / _LOOKBACK), so a change on the Switches page applies on the next run.
The data fetch and the charges function are injectable, so the tests need no network, no database and no broker."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import config
from timemachine import data as tm_data
from timemachine import engine as E

logger = logging.getLogger("position-stocks-time-machine")

MAX_COMPARE_SYMBOLS = 15
COMPARE_SIMS_CAP = 5000                  # a compare run is many stocks, so each one uses at most this many made-up sessions
DEFAULT_ENTRY_TIME = "10:00"
TABLE_FIRST, TABLE_LAST, TABLE_STEP = "09:30", "14:00", 15      # the default entry-time table
_SEM = threading.BoundedSemaphore(2)     # at most two runs at once; a third is told to wait (HTTP 429) instead of piling up
_recent: List[Dict[str, Any]] = []
_lock = threading.Lock()

ChargesFn = Callable[[float, float, int, bool], float]


class TimeMachineError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": False, "code": self.code, "error": self.message}


# ------------------------------------------------------------------ helpers

def _num(d: Dict[str, Any], key: str, lo: float, hi: float, default: Optional[float] = None) -> float:
    raw = d.get(key, default)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise TimeMachineError("BAD_PARAM", f"{key} must be a number")
    if not (v == v) or v < lo or v > hi:
        raise TimeMachineError("BAD_PARAM", f"{key} must be between {lo:g} and {hi:g}")
    return v


def _default_flat_by(side: str) -> str:
    return str(getattr(config, "SHORT_SQUAREOFF_TIME_IST", "14:55") if side == "SHORT" else getattr(config, "EOD_SQUAREOFF_TIME_IST", "15:00"))


def default_charges_fn() -> ChargesFn:
    """Total rupee charges of one intraday round trip, from the same Dhan charge model the ledger uses (a short sells first)."""
    from orders import charges_ledger

    def fn(entry: float, exit_: float, qty: int, short: bool) -> float:
        return float(charges_ledger.charges_for_trade(entry, exit_, qty, short=short)["total"])
    return fn


def default_table_times(flat: int) -> List[int]:
    first, last = E.hhmm(TABLE_FIRST), E.hhmm(TABLE_LAST)
    return [t for t in range(first, last + 1, TABLE_STEP) if t < flat]


def parse_params(body: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and fill defaults. Raises TimeMachineError(BAD_PARAM). Pure apart from reading config defaults."""
    if not isinstance(body, dict):
        raise TimeMachineError("BAD_PARAM", "send a JSON object")
    side = str(body.get("side") or "LONG").strip().upper()
    side = {"BUY": "LONG", "SELL": "SHORT"}.get(side, side)
    if side not in ("LONG", "SHORT"):
        raise TimeMachineError("BAD_PARAM", "side must be LONG or SHORT")
    stop = _num(body, "stop_pct", 0.1, 10.0, 1.0)
    target = _num(body, "target_pct", 0.1, 30.0, 2.0)
    try:
        flat = E.hhmm(body.get("flat_by") or _default_flat_by(side))
        entry = E.hhmm(body.get("entry_time") or DEFAULT_ENTRY_TIME)
    except ValueError:
        raise TimeMachineError("BAD_PARAM", "times must look like 09:45")
    if flat <= E.OPEN_MIN + E.SLOT_MIN or flat > E.CLOSE_MIN:
        raise TimeMachineError("BAD_PARAM", "flat_by must be between 09:25 and 15:30")
    if (entry - E.OPEN_MIN) % E.SLOT_MIN or not (E.OPEN_MIN <= entry < flat):
        raise TimeMachineError("BAD_PARAM", f"entry_time must be a 5-minute mark from 09:15 and before the flat-by time ({E.fmt(flat)})")
    sims = int(_num(body, "sims", 1000, 50000, int(getattr(config, "TIME_MACHINE_SIMS", 10000))))
    seed = int(_num(body, "seed", 0, 2 ** 31 - 1, 20261024))
    slip = _num(body, "slippage_pct", 0.0, 1.0, float(getattr(config, "TIME_MACHINE_SLIPPAGE_PCT", 0.05)))
    value = _num(body, "value_rs", 1000.0, 10_000_000.0, float(getattr(config, "TIME_MACHINE_DEFAULT_VALUE_RS", 20000.0)))
    lookback = str(body.get("lookback") or getattr(config, "TIME_MACHINE_LOOKBACK", "1mo")).lower()
    if lookback not in tm_data.LOOKBACKS:
        raise TimeMachineError("BAD_PARAM", "lookback must be 1mo or 2mo")
    times_raw = body.get("entry_times")
    if times_raw in (None, "", []):
        times = default_table_times(flat)
    else:
        if not isinstance(times_raw, (list, tuple)) or len(times_raw) > 40:
            raise TimeMachineError("BAD_PARAM", "entry_times must be a list of at most 40 times")
        try:
            times = sorted({E.hhmm(t) for t in times_raw})
        except ValueError:
            raise TimeMachineError("BAD_PARAM", "entry_times must look like 09:45")
        times = [t for t in times if (t - E.OPEN_MIN) % E.SLOT_MIN == 0 and E.OPEN_MIN <= t < flat]
    if entry not in times:
        times = sorted(set(times) | {entry})
    return {"side": side, "stop_pct": stop, "target_pct": target, "flat_by": flat, "entry": entry, "sims": sims, "seed": seed, "slippage_pct": slip,
            "value_rs": value, "lookback": lookback, "entry_times": times}


def make_rule(p: Dict[str, Any], ref_price: float, charges_fn: ChargesFn) -> E.Rule:
    """The rule with its round-trip charges as % of the entry value for each way a trade can end (target, stop, flat), from the real charge model."""
    if not (ref_price and ref_price > 0):
        raise TimeMachineError("NO_PRICE", "no usable reference price for the charges", 502)
    qty = int(p["value_rs"] // ref_price)
    if qty < 1:
        raise TimeMachineError("BAD_PARAM", f"value_rs {p['value_rs']:.0f} is below one share at about {ref_price:.2f}")
    short = p["side"] == "SHORT"
    sp, tp = p["stop_pct"] / 100.0, p["target_pct"] / 100.0
    exits = {"target": ref_price * (1 - tp if short else 1 + tp), "stop": ref_price * (1 + sp if short else 1 - sp), "flat": ref_price}
    val = ref_price * qty
    ch = {k: float(charges_fn(ref_price, px, qty, short)) / val * 100.0 for k, px in exits.items()}
    return E.Rule(p["side"], p["stop_pct"], p["target_pct"], p["flat_by"], p["slippage_pct"], ch)


def _row(s: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-ready copy of a summary (rounded)."""
    if not s or not s.get("n"):
        return {"n": 0}
    out = dict(s)
    for k in ("p_target", "p_stop", "p_flat", "ci_low", "ci_high", "win_rate_net", "avg_r_gross", "avg_r_net", "avg_net_pct"):
        if out.get(k) is not None:
            out[k] = round(out[k], 4)
    return out


# ------------------------------------------------------------------ the run

def enabled() -> bool:
    return bool(getattr(config, "TIME_MACHINE_ENABLED", True))


def run(body: Dict[str, Any], fetch: Optional[tm_data.Fetcher] = None, charges_fn: Optional[ChargesFn] = None, with_table: bool = True) -> Dict[str, Any]:
    """One full Time Machine answer for one stock. Raises TimeMachineError (the route turns it into an HTTP error). Never places or changes anything."""
    if not enabled():
        raise TimeMachineError("DISABLED", "the Time Machine is switched off (TIME_MACHINE_ENABLED)", 503)
    started = time.monotonic()
    p = parse_params(body)
    try:
        symbol = tm_data.normalize_symbol(body.get("symbol"))
    except tm_data.DataError as e:
        raise TimeMachineError(e.code, e.message, e.status)
    if not _SEM.acquire(timeout=1.0):
        raise TimeMachineError("BUSY", "two Time Machine runs are already in progress; try again in a few seconds", 429)
    try:
        try:
            sessions, info = tm_data.load_sessions(symbol, p["lookback"], fetch)
        except tm_data.DataError as e:
            raise TimeMachineError(e.code, e.message, e.status)
        days = sorted(sessions)
        ref = sessions[days[-1]][-1][4]
        rule = make_rule(p, ref, charges_fn or default_charges_fn())
        ctrl = rule.opposite()
        entry = p["entry"]
        table_mods = p["entry_times"] if with_table else [entry]

        hist_rows = E.replay_history(sessions, rule, [entry])
        hist = E.summarize_rows(hist_rows, rule)
        sim = E.simulate(sessions, [(rule, table_mods), (ctrl, [entry])], entry, p["sims"], p["seed"])
        main_acc = sim["per_plan"][0][entry]
        sim_sum = E.summarize_acc(main_acc, rule)
        ctrl_sum = E.summarize_acc(sim["per_plan"][1][entry], ctrl)
        ctrl_hist = E.summarize_rows(E.replay_history(sessions, ctrl, [entry]), ctrl)
        vd = E.verdict(sim_sum, hist, rule, len(sessions))

        rs = sorted(main_acc.r_list or [])
        spread = {k: (round(E.quantile(rs, q), 4) if rs else None) for k, q in (("p05", .05), ("p25", .25), ("p50", .5), ("p75", .75), ("p95", .95))}
        table: List[Dict[str, Any]] = []
        if with_table:
            hist_all = E.replay_history(sessions, rule, table_mods)
            for em in table_mods:
                hs = E.summarize_rows([r for r in hist_all if r["entry"] == E.fmt(em)], rule)
                ss = E.summarize_acc(sim["per_plan"][0][em], rule)
                table.append({"time": E.fmt(em), "is_selected": em == entry,
                              "sim_p_target": round(ss["p_target"], 4) if ss.get("n") else None,
                              "sim_avg_r_net": round(ss["avg_r_net"], 4) if ss.get("n") else None,
                              "hist_n": hs.get("n", 0),
                              "hist_p_target": round(hs["p_target"], 4) if hs.get("n") else None,
                              "hist_avg_r_net": round(hs["avg_r_net"], 4) if hs.get("n") else None})
        drift = E.window_drift_pct(sessions)
        warnings: List[str] = []
        if info.get("fallback"):
            warnings.append("The 2-month window was not available; the last month was used.")
        if len(sessions) < E.MIN_HISTORY_SESSIONS:
            warnings.append(f"Only {len(sessions)} complete real sessions: the history side cannot vote.")
        if drift is not None and abs(drift) >= 0.3:
            warnings.append(f"The window drifted {drift:+.2f}% per session on average, and the simulation inherits that drift: read the opposite side beside it.")
        gap = round((hist["p_target"] - sim_sum["p_target"]) * 100.0, 1) if hist.get("n") and sim_sum.get("n") else None
        out = {
            "ok": True, "symbol": symbol,
            "params": {"side": rule.side, "stop_pct": rule.stop_pct, "target_pct": rule.target_pct, "rr": round(rule.rr, 3), "entry_time": E.fmt(entry),
                       "flat_by": E.fmt(rule.flat_by), "sims": sim["sessions"], "seed": p["seed"], "slippage_pct": rule.slippage_pct, "value_rs": p["value_rs"],
                       "lookback": p["lookback"]},
            "data": {"sessions": len(sessions), "first_day": days[0], "last_day": days[-1], "last_close": round(ref, 2), "lookback_used": info["lookback_used"],
                     "cached": info["cached"], "window_drift_pct": round(drift, 3) if drift is not None else None,
                     "source": "market-data-service /history, 5-minute bars"},
            "charges_pct": {k: round(v, 4) for k, v in rule.charges.items()},
            "breakeven": {"win_gross": round(rule.be_win(), 4), "win_net": round(rule.be_win_net(), 4)},
            "history": _row(hist), "history_rows": hist_rows,
            "simulation": {**_row(sim_sum), "r_spread": spread, "r_histogram": E.r_histogram(rs, rule)},
            "history_vs_simulation_pts": gap,
            "control": {"side": ctrl.side, "simulation": _row(ctrl_sum), "history": _row(ctrl_hist)},
            "verdict": {"code": vd["code"], "line": vd["line"]},
            "by_entry_time": table, "warnings": warnings, "limits": list(E.LIMITS),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        _remember(symbol, rule.side, vd["code"], out["elapsed_ms"])
        return out
    finally:
        _SEM.release()


def compare(body: Dict[str, Any], fetch: Optional[tm_data.Fetcher] = None, charges_fn: Optional[ChargesFn] = None) -> Dict[str, Any]:
    """The same rule on several stocks (at most 15), ranked by the simulated average net R. A stock that cannot be loaded is listed under `failed`, not hidden."""
    if not enabled():
        raise TimeMachineError("DISABLED", "the Time Machine is switched off (TIME_MACHINE_ENABLED)", 503)
    syms_raw = body.get("symbols")
    if not isinstance(syms_raw, (list, tuple)) or not syms_raw:
        raise TimeMachineError("BAD_PARAM", "symbols must be a non-empty list")
    syms: List[str] = []
    for s in syms_raw:
        try:
            n = tm_data.normalize_symbol(s)
        except tm_data.DataError as e:
            raise TimeMachineError(e.code, e.message, e.status)
        if n not in syms:
            syms.append(n)
    if len(syms) > MAX_COMPARE_SYMBOLS:
        raise TimeMachineError("BAD_PARAM", f"compare takes at most {MAX_COMPARE_SYMBOLS} stocks at a time")
    base = {k: v for k, v in body.items() if k not in ("symbols", "symbol")}
    base["sims"] = min(int(base.get("sims") or getattr(config, "TIME_MACHINE_SIMS", 10000)), COMPARE_SIMS_CAP)
    p = parse_params(base)                      # a bad shared setting is refused before any stock is fetched
    rows: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    for s in syms:
        try:
            r = run({**base, "symbol": s}, fetch, charges_fn, with_table=False)
        except TimeMachineError as e:
            if e.code in ("DISABLED", "BAD_PARAM"):
                raise
            failed.append({"symbol": s, "code": e.code, "error": e.message})
            continue
        sim, hist = r["simulation"], r["history"]
        rows.append({"symbol": s, "verdict": r["verdict"]["code"], "sim_p_target": sim.get("p_target"), "sim_avg_r_net": sim.get("avg_r_net"),
                     "hist_n": hist.get("n", 0), "hist_p_target": hist.get("p_target"), "hist_avg_r_net": hist.get("avg_r_net"),
                     "control_sim_avg_r_net": (r["control"]["simulation"] or {}).get("avg_r_net"), "sessions": r["data"]["sessions"],
                     "last_close": r["data"]["last_close"], "window_drift_pct": r["data"]["window_drift_pct"]})
    rows.sort(key=lambda x: (x["sim_avg_r_net"] is None, -(x["sim_avg_r_net"] or 0.0), x["symbol"]))
    return {"ok": True, "rows": rows, "failed": failed,
            "params": {"side": p["side"], "stop_pct": p["stop_pct"], "target_pct": p["target_pct"], "entry_time": E.fmt(p["entry"]), "flat_by": E.fmt(p["flat_by"]),
                       "sims": base["sims"], "value_rs": p["value_rs"]},
            "limits": list(E.LIMITS)}


# ------------------------------------------------------------------ status

def _remember(symbol: str, side: str, code: str, ms: int) -> None:
    with _lock:
        _recent.append({"at": time.time(), "symbol": symbol, "side": side, "verdict": code, "ms": ms})
        del _recent[:-20]


def status() -> Dict[str, Any]:
    with _lock:
        recent = list(reversed(_recent))
    return {"enabled": enabled(), "sims_default": int(getattr(config, "TIME_MACHINE_SIMS", 10000)),
            "slippage_pct": float(getattr(config, "TIME_MACHINE_SLIPPAGE_PCT", 0.05)), "lookback": str(getattr(config, "TIME_MACHINE_LOOKBACK", "1mo")),
            "default_value_rs": float(getattr(config, "TIME_MACHINE_DEFAULT_VALUE_RS", 20000.0)),
            "defaults": {"side": "LONG", "stop_pct": 1.0, "target_pct": 2.0, "entry_time": DEFAULT_ENTRY_TIME,
                         "flat_by_long": _default_flat_by("LONG"), "flat_by_short": _default_flat_by("SHORT")},
            "max_compare_symbols": MAX_COMPARE_SYMBOLS, "cache": tm_data.cache_stats(), "recent": recent, "limits": list(E.LIMITS)}
