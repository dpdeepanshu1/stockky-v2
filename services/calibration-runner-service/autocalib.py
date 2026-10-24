"""autocalib.py - group 476: automatic calibration of the Real Money Calibration switches from the TEST RESULTS.

What it does
  Every switch that has a test is listed in RULES: which test decides it, and how the verdict of that test maps to a value. When a test run finishes
  (or you press "Evaluate now"), the engine reads the latest finished result of each test, decides the value for each switch, applies it through the
  same control API the page uses (POST <service>/control/flags/<KEY>, so every change is also in that service's own audit log) and sends ONE Telegram
  message listing every switch it changed.

Safety rules (all enforced here, all covered by tests/test_autocalib.py)
  * only keys in RULES are ever touched; keys in PROTECTED (broker stop, fail-closed data rules, recorders, ...) can never get a rule;
  * a test that failed / timed out / is older than AUTOCALIB_MAX_AGE_DAYS, or whose verdict is "not enough data / no edge", changes NOTHING (hold);
  * a change that LOOSENS a trading rule needs the same value from two DIFFERENT test runs in a row (a single noisy run cannot switch a rule off);
    a change that tightens needs one. A run that disagrees (or has no opinion) resets the count;
  * numbers move at most AUTOCALIB_STEP_PCT (default 25 %) towards the value the test supports per evaluation, and stay inside the switch's own range;
  * a switch changed in the last AUTOCALIB_COOLDOWN_H hours is left alone; a switch you PIN is never touched;
  * a master switch (enabled) and a dry-run mode (decide and report, apply nothing) are stored in <data>/state/autocalib.json.
Stdlib only. Nothing here runs a script; it only reads the results the runner already holds.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

IST = timezone(timedelta(hours=5, minutes=30))
RT, PS = "real-trade-service", "position-stocks-service"
USABLE = ("success", "attention")          # a script that exits 1 with a verdict is "attention": its verdict is still evidence
MAX_TEXT = 3_000_000
HISTORY_KEEP = 200

# Switches that no test result may ever move. Each has a reason the page shows.
PROTECTED: Dict[str, str] = {
    "SHORT_MODE": "money safety (short selling): whether a short can exist is your decision after the Dhan answer and the 1-share test, never a report's",
    "SHORT_LIVE_QTY_CAP": "money safety (short selling): how many shares a live short may have is your decision, never a report's",
    "SHORT_LIVE_REQUIRES_PROBE": "money safety (short selling): whether a live short needs the Dhan SELL probe first is your decision, never a report's",
    "SHORT_SHORTLIST_ENABLED": "short selling (E107): which shorts are tried first decides where capital goes; it is your decision, never a report's",
    "SHORT_SHORTLIST_MAX": "short selling (E107): how many shorts are quality-checked per cycle is your decision, never a report's",
    "SHORT_STOP_MULT_STRESSED": "money safety (short selling): how wide a short's stop is in a stressed market is your decision, never a report's",
    "SHORT_STOP_MULT_PANIC": "money safety (short selling): how wide a short's stop is in a panic market is your decision, never a report's",
    "SHORT_RISK_MULT": "money safety (short selling): how much a short may risk is your decision, never a report's",
    "SHORT_MAX_POSITIONS": "money safety (short selling): how many shorts may be open at once is your decision, never a report's",
    "SHORT_UPPER_CIRCUIT_BUFFER_PCT": "money safety (short selling): a locked-up stock cannot be bought back, so this buffer is never loosened by a report",
    "SHORT_ENTRY_CUTOFF_IST": "money safety (short selling): a late short is easily squeezed, so the cut-off is never loosened by a report",
    "SHORT_SQUAREOFF_TIME_IST": "money safety (short selling): when a short must be covered is your decision, never a report's",
    "SHORT_ENTRY_START_IST": "money safety (short selling): the earliest time for a short is your decision; the VWAP rests on too few trades before it",
    "SHORT_GAP_UP_REFUSE_PCT": "money safety (short selling): how big a gap-up still above the open blocks a short is your decision, never a report's",
    "SHORT_MAX_DAY_FALL_PCT": "money safety (short selling): do not short what has already fallen hard; a threshold of a gate, decided by you",
    "SHORT_MIN_BOARD_CONVICTION": "money safety (short selling): the Market Board conviction a short needs is a threshold of a gate, decided by you",
    "SHORT_MIN_ENTRY_RANGE_POSITION": "money safety (short selling): how near the day low blocks a short is a threshold of a gate, decided by you",
    "SHORT_VWAP_TOLERANCE_PCT": "money safety (short selling): the VWAP tolerance of the short gate is decided by you, never a report's",
    "BROKER_STOP_ENABLED": "money safety: the resting broker stop is never switched by a report",
    "SIZE_LADDER_ENABLED": "money safety (group 481): position size is raised only by the stored net P&L rule, never switched by a report",
    "SIZE_LADDER_MAX_LEVEL": "money safety (group 481): how far size may grow is your decision, not a report's",
    "BROKER_STOP_INTRADAY": "money safety (group 479): the same-day resting stop is switched by you after a live check, never by a report",
    "STRUCTURE_REQUIRE_DATA": "fail-closed rule: no data means no entry",
    "PS_STRUCTURE_REQUIRE_DATA": "fail-closed rule: no data means no entry",
    "STRUCTURE_FETCH_ENABLED": "input of every structure rule",
    "ENTRY_REQUIRE_LIVE_TICK": "fail-closed rule: no live price means no entry",
    "ENTRY_PRECHECK": "order safety check",
    "ENTRY_DEPTH_GATE": "order safety check",
    "ENTRY_DEPTH_RECHECK": "order safety check",
    "ENTRY_MAX_SLIPPAGE_PCT": "order safety limit",
    "IMPORT_SKIP_PENDING_BUY_MIN": "order / reconcile safety",
    "EOD_SIGNAL_SCAN_SAME_DAY_ENTRY_ENABLED": "stays off: every such fill becomes an overnight position",
    "STOPOUT_GUARD_ENABLED": "loss-review guard (group 471): decided by you, not by a report",
    "STOPOUT_DAY_PAUSE_N": "loss-review guard (group 471): decided by you, not by a report",
    "STOPOUT_SYMBOL_BLOCK": "loss-review guard (group 471): decided by you, not by a report",
    "ENTRY_MIN_QTY": "loss-review guard (group 472): decided by you, not by a report",
    "ENTRY_MIN_REWARD_RISK": "risk rule (1:2 reward:risk, 2026-10-22): decided by you, not by a report",
    "ENTRY_ANCHOR_TO_LIVE": "order safety rule (2026-10-22): stop / target follow the real price, never switched by a report",
    "STOP_OVERSHOOT_ALLOWANCE_PCT": "loss-review guard (group 470): decided by you, not by a report",
    "ORB_MODE": "evidence recorder (shadow verdict): always on so the report has data",
    "VIX_CAUTION_PCTILE": "risk gate (plan P2 item 16, E107): decided by you, not by a report",
    "PREOPEN_GAP_ATR_MULT": "risk gate (plan P2 item 16, E107): decided by you, not by a report",
    "VIX_CAUTION_ABOVE": "risk gate (plan P2 item 16): decided by you, not by a report",
    "VIX_SPIKE_PCT": "risk gate (plan P2 item 16): decided by you, not by a report",
    "VIX_CAUTION_SIZE_MULT": "risk gate (plan P2 item 16): decided by you, not by a report",
    "PREOPEN_GAP_UP_BLOCK_PCT": "risk gate (plan P2 item 16): decided by you, not by a report",
    "PREOPEN_GAP_DOWN_BLOCK_PCT": "risk gate (plan P2 item 16): decided by you, not by a report",
    "PREOPEN_GAP_GATE_UNTIL_IST": "risk gate (plan P2 item 16): decided by you, not by a report",
    "OPENING_GATE_MIN_STOP_ATR_FRAC": "risk gate (plan P2 item 16): decided by you, not by a report",
    "SCALP_TARGET_CAP_MIN_STOP_ATR_MULT": "risk gate (plan P2 item 16): decided by you, not by a report",
    "TRADE_ODDS_MODE": "evidence recorder (stop vs target odds): always on so the report has data",
    "STRUCTURE_OBS_ENABLED": "evidence recorder: always on so tests have data",
    "ORDER_FLOW_RECORD": "evidence recorder: always on so tests have data",
    "ORDER_FLOW_DEPTH20": "evidence recorder: always on so tests have data",
    "BAR_RECORD_ENABLED": "evidence recorder: always on so tests have data",
    "STRUCTURE_GATE_DEMO_MODE": "follows the REAL value; DEMO is never calibrated separately",
    "KRONOS_GATE_DEMO_MODE": "follows the REAL value; DEMO is never calibrated separately",
    "TIMESFM_GATE_DEMO_MODE": "follows the REAL value; DEMO is never calibrated separately",
    "STRUCTURE_BEARISH_BLOCK": "structure gate (enforce): a gate, not a dial; decided by you",
    "PS_STRUCTURE_BEARISH_BLOCK": "structure gate (enforce): a gate, not a dial; decided by you",
    "STRUCTURE_BREAKOUT_VOLUME_MODE": "breakout-volume gate (E73): a gate, not a dial; decided by you",
    "PS_STRUCTURE_BREAKOUT_VOLUME_MODE": "breakout-volume gate (E73): a gate, not a dial; decided by you",
    "STRUCTURE_BREAKOUT_MIN_VOL_RATIO": "breakout-volume gate (E73): threshold of a gate; decided by you",
    "PS_STRUCTURE_BREAKOUT_MIN_VOL_RATIO": "breakout-volume gate (E73): threshold of a gate; decided by you",
    "STRUCTURE_MAX_AGE_DAYS": "structure data freshness: fail-closed input of every structure rule",
    "STRUCTURE_REJECT_STOP_WIDE": "structure stop rule: decided by you, not by a report",
    "STRUCTURE_TRAIL_ENABLED": "structure trailing exit: decided by you, not by a report",
    "PS_STRUCTURE_MAX_CHASE_ATR": "position-stocks structure threshold: decided by you until it has its own test",
    "PS_STRUCTURE_MAX_STRETCH_ATR": "position-stocks structure threshold: decided by you until it has its own test",
    "PS_STRUCTURE_MIN_ROOM_ATR": "position-stocks structure threshold: decided by you until it has its own test",
    "MARKET_STATE_MODE": "Market Weather shadow/live: switched by you after the shadow report, never by a report",
    "MARKET_STATE_RISK_MULT": "Market Weather dial: set only by today's overlay (tighten-only, expires 15:30), never saved by the weekly engine",
    "MARKET_STATE_CONVICTION_ADD": "Market Weather dial: set only by today's overlay (tighten-only, expires 15:30), never saved by the weekly engine",
    "MARKET_STATE_SHORT_MULT": "Market Weather short dial: set only by today's overlay (tighten-only, expires 15:30), never saved by the weekly engine",
}


# ----------------------------------------------------------------------------------------------------- evidence
@dataclass
class Ev:
    """One finished test result: its run id, when it finished (epoch s), the text of its own section, and a reader for its JSON files."""
    job: str
    run_id: str
    finished_at: float
    text: str = ""
    json_reader: Optional[Callable[[str], Any]] = None
    status: str = "success"

    def json(self, name: str) -> Any:
        try:
            return self.json_reader(name) if self.json_reader else None
        except Exception:  # noqa: BLE001 - an unreadable file is "no evidence", never an error
            return None


def section(text: str, job_id: str) -> str:
    """The part of a run's output.txt written by one job (a pack run holds several `## JOB title  (id)` sections). A single-job run is one section."""
    marks = list(re.finditer(r"^## JOB .*\(([a-z0-9_]+)\)\s*$", text, re.M))
    if not marks:
        return text
    for i, m in enumerate(marks):
        if m.group(1) == job_id:
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            return text[m.start():end]
    return ""


def _iso_to_epoch(v: Any) -> float:
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


class RunnerEvidence:
    """Reads the newest usable result of a test from the Runner: the job's own run or any pack run that contained it."""

    def __init__(self, runner: Any, packs: Dict[str, Dict[str, Any]], max_age_s: float, now_fn: Callable[[], float] = time.time):
        self.runner, self.packs, self.max_age_s, self.now = runner, packs, max_age_s, now_fn

    def _targets(self, job_id: str) -> List[str]:
        return [job_id] + [pid for pid, p in self.packs.items() if any(j == job_id for j, _ in p.get("jobs", []))]

    def get(self, job_id: str) -> Optional[Ev]:
        best: Optional[Tuple[float, Dict[str, Any], str]] = None
        for t in self._targets(job_id):
            r = self.runner.latest(t)
            if not r:
                continue
            if r.get("kind") == "pack":
                js = [j for j in (r.get("jobs") or []) if j.get("id") == job_id]
                st = js[0]["status"] if js else ""
            else:
                st = r.get("status")
            fin = _iso_to_epoch(r.get("finished_at"))
            if st not in USABLE or not fin or self.now() - fin > self.max_age_s:
                continue
            if best is None or fin > best[0]:
                best = (fin, r, st)
        if best is None:
            return None
        fin, r, st = best
        rid = r["id"]
        text = ""
        p = self.runner.file_path(rid, "output.txt")
        if p:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(MAX_TEXT)
        files = [f["name"] for f in self.runner.files(rid)]

        def reader(name: str, _rid: str = rid, _files: List[str] = files) -> Any:
            for rel in _files:
                if os.path.basename(rel) == name:
                    fp = self.runner.file_path(_rid, rel)
                    if fp:
                        with open(fp, "r", encoding="utf-8") as fh:
                            return json.load(fh)
            return None
        return Ev(job_id, rid, fin, section(text, job_id), reader, st)


# ----------------------------------------------------------------------------------------------------- decisions
@dataclass
class Decision:
    value: Optional[str]            # canonical string to set; None = hold (no change)
    reason: str
    confirms: int = 1               # distinct test runs that must agree before it is applied (loosening is floored to 2 by the engine)


@dataclass
class Rule:
    key: str
    service: str
    title: str
    tests: Tuple[str, ...]
    explain: str
    fn: Callable[[Dict[str, Ev], str, Dict[str, Any]], Decision]


def _gate_status(ev: Ev, gate: str) -> Optional[str]:
    m = re.search(rf"^{gate}: (PASS|FAIL|NOT_ENOUGH_DATA|NO_INPUT)\b", ev.text, re.M)
    return m.group(1) if m else None


def r_52w_log_only(evs, cur, ctx):
    rep = evs["legacy_gate"].json("gate_report.json") or {}
    g = ((rep.get("gates") or {}).get("range_52w_legacy")) or {}
    v = g.get("verdict_robust") or g.get("verdict")
    if v == "hurts":
        return Decision("true", "legacy gate test: the 52-week rule removed the BETTER entries (verdict hurts, robust) -> evaluate it but do not reject", 2)
    if v == "helps":
        return Decision("false", "legacy gate test: the 52-week rule helps (verdict helps, robust) -> it rejects again", 1)
    return Decision(None, f"52-week rule verdict is {v or 'missing'}: no change")


def r_kronos(evs, cur, ctx):
    m = re.search(r"^VERDICT: (.+)$", evs["kronos_backtest"].text, re.M)
    v = (m.group(1) if m else "").strip()
    if v.startswith("VETO HELPS"):
        return Decision("enforce", "Kronos backtest: vetoed rows are clearly worse and passed rows are positive net of cost", 2)
    if v.startswith(("NOT PROVEN", "VETO SEPARATES")):
        if cur == "enforce":
            return Decision("log", f"Kronos backtest no longer proves the veto ({v[:60]}) -> stop blocking, keep recording", 2)
        return Decision(None, f"Kronos verdict: {v[:60]}: stays {cur}")
    return Decision(None, f"Kronos verdict is {v[:40] or 'missing'}: no change")


def r_tsfm(evs, cur, ctx):
    txt = evs["tsfm_combine"].text
    i = txt.find("{")
    try:
        doc, _ = json.JSONDecoder().raw_decode(txt[i:]) if i >= 0 else ({}, 0)
    except ValueError:
        doc = {}
    st = doc.get("status")
    if st == "PASS":
        return Decision("log", "TimesFM Gate A passed on every seed: record its verdicts (there is no enforce mode)", 2)
    if st == "FAIL":
        return Decision("off", "TimesFM Gate A failed: nothing is fetched or recorded", 1)
    return Decision(None, f"TimesFM Gate A is {st or 'missing'}: no change")


def r_g3_stop(evs, cur, ctx):
    st = _gate_status(evs["gate_check"], "G3")
    if st == "PASS":
        return Decision("on", "G3 stability passed: the structural stop may be used", 2)
    if st == "FAIL":
        return Decision("off", "G3 stability failed: no structural stop", 1)
    return Decision(None, f"G3 is {st or 'missing'}: no change")


def r_g2_exit(evs, cur, ctx):
    st = _gate_status(evs["gate_check"], "G2")
    if st == "FAIL" and cur in ("exit", "tighten"):
        return Decision("log", "G2 benefit failed: structure exits only record, they do not act", 1)
    return Decision(None, f"G2 is {st or 'missing'}: exit mode stays {cur} (it is never promoted automatically)")


def r_g2_gate(evs, cur, ctx):
    st = _gate_status(evs["gate_check"], "G2")
    if st == "PASS":
        return Decision("enforce", "G2 benefit passed: the structure gate is on", 1)
    if st == "FAIL":
        # group 486: G2 FAIL means "benefit NOT PROVEN" (no_edge / low kept share), not "the gate is harmful". The structure entry gate is one of the
        # rules that stays on (scripts/env_structure_check.py, group415), and on 2026-10-21 01:28 this rule switched it off for REAL on a no_edge verdict,
        # one day after the opening guards had been turned off by hand. A report never loosens it: switch it off yourself if you decide to.
        return Decision(None, "G2 benefit not proven: the structure gate is left as it is (a report never switches an entry gate off)")
    return Decision(None, f"G2 is {st or 'missing'}: no change")


def _num(s: Any) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def r_threshold(rule: str):
    def fn(evs, cur, ctx):
        txt = evs["calibration_pack"].text
        if not re.search(r"A/B verdicts: .*all `pass`\.", txt):
            return Decision(None, "A/B verdicts are not `pass` on every seed: the walk-forward lines are hypotheses only")
        m = re.search(rf"^- {rule}: SUGGEST ([0-9.]+)", txt, re.M)
        if not m:
            return Decision(None, f"walk-forward has no stable, significant suggestion for {rule}")
        pick, now = _num(m.group(1)), _num(cur)
        if pick is None or now is None:
            return Decision(None, "suggestion or current value unreadable")
        step = max(abs(now) * float(ctx.get("step_pct", 0.25)), 0.25)
        new = pick if abs(pick - now) <= step else now + (step if pick > now else -step)
        new = round(new, 2)
        if new == now:
            return Decision(None, "already at the suggested value")
        tail = "" if new == pick else f" (moving {step:g} per evaluation towards {pick:g})"
        return Decision(f"{new:g}", f"walk-forward is stable out of sample and suggests {pick:g}{tail}", 1)
    return fn


def r_overnight(evs, cur, ctx):
    calls = re.findall(r"(1 day|2-5 days|6\+ days) vs same day: (better|worse|no clear difference)", evs["hold_bucket"].text)
    if any(c == "worse" for _, c in calls):
        return Decision("false", "hold-bucket test: multi-day holds are clearly worse than same-day exits", 1)
    # Never switched ON automatically: the 20 Oct loss review (group 470) made "off" the default, and carrying a position overnight is a decision for you.
    return Decision(None, "hold-bucket test shows no multi-day bucket worse than same-day: overnight hold is only ever switched OFF automatically, never ON")


def r_intraday(gate_key: str, what: str):
    def fn(evs, cur, ctx):
        rep = evs["intraday_gates"].json("gate_backtest.json") or {}
        v = ((rep.get(gate_key)) or {}).get("verdict")
        if v == "pass":
            return Decision("true", f"intraday backtest: the {what} earns its place (pass)", 1)
        if v == "hurts":
            return Decision("false", f"intraday backtest: the {what} removes the better entries (hurts)", 2)
        return Decision(None, f"{what} verdict is {v or 'missing'}: no change")
    return fn


RULES: List[Rule] = [
    Rule("CANDIDATE_52W_LOG_ONLY", RT, "52-week rule LOG ONLY", ("legacy_gate",), "legacy_gate: gates.range_52w_legacy verdict (robust)", r_52w_log_only),
    Rule("KRONOS_GATE_MODE", RT, "Kronos gate 4f (REAL)", ("kronos_backtest",), "kronos_backtest: VERDICT line", r_kronos),
    Rule("TIMESFM_GATE_MODE", RT, "TimesFM gate 4g (REAL)", ("tsfm_combine",), "tsfm_combine: Gate A over all seeds", r_tsfm),
    Rule("STRUCTURE_STOP_MODE", RT, "Structural stop", ("gate_check",), "gate_check: G3 stability", r_g3_stop),
    Rule("STRUCTURE_EXIT_MODE", RT, "Structure exit", ("gate_check",), "gate_check: G2 benefit (only ever steps DOWN to log)", r_g2_exit),
    Rule("STRUCTURE_GATE_MODE", RT, "Structure gate (REAL)", ("gate_check",), "gate_check: G2 benefit", r_g2_gate),
    Rule("PS_STRUCTURE_GATE_MODE", PS, "Structure gate (position-stocks)", ("gate_check",), "gate_check: G2 benefit", r_g2_gate),
    Rule("STRUCTURE_MAX_CHASE_ATR", RT, "Max chase above zone (ATR)", ("calibration_pack",), "calibration_pack: walk-forward `chase` suggestion", r_threshold("chase")),
    Rule("STRUCTURE_MAX_STRETCH_ATR", RT, "Max stretch above EMA20 (ATR)", ("calibration_pack",), "calibration_pack: walk-forward `stretch` suggestion", r_threshold("stretch")),
    Rule("STRUCTURE_MIN_ROOM_ATR", RT, "Min room to resistance (ATR)", ("calibration_pack",), "calibration_pack: walk-forward `room` suggestion", r_threshold("room")),
    Rule("STRUCTURE_MIN_RR", RT, "Min structure reward:risk", ("calibration_pack",), "calibration_pack: walk-forward `rr` suggestion", r_threshold("rr")),
    Rule("OVERNIGHT_HOLD_ENABLED", RT, "Overnight hold allowed", ("hold_bucket",), "hold_bucket: multi-day worse than same-day -> OFF (never switched ON automatically)", r_overnight),
    Rule("ENTRY_VWAP_GATE_ENABLED", PS, "VWAP entry gate (position-stocks)", ("intraday_gates",), "intraday_gates: vwap_gate verdict on recorded bars", r_intraday("vwap_gate", "VWAP gate")),
    Rule("OPENING_GATE_ENABLED", PS, "Opening quality gate (position-stocks)", ("intraday_gates",), "intraday_gates: opening_gate verdict on recorded bars", r_intraday("opening_gate", "opening gate")),
]
EVIDENCE_JOBS = sorted({t for r in RULES for t in r.tests})


# ----------------------------------------------------------------------------------------------------- control API client
class ControlHTTPError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


class ControlClient:
    """GET /control/flags and POST /control/flags/<KEY> on real-trade / position-stocks with the runner's own service token."""

    def __init__(self, urls: Dict[str, str], token_fn: Callable[[], str], timeout: float = 15.0):
        self.urls, self.token_fn, self.timeout = urls, token_fn, timeout

    def _call(self, service: str, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        base = (self.urls.get(service) or "").rstrip("/")
        if not base:
            raise ControlHTTPError(0, f"no URL for {service}")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method, headers={"Authorization": "Bearer " + self.token_fn(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - fixed internal URL from the environment
                return json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8", "replace")).get("detail")
            except Exception:  # noqa: BLE001
                detail = str(e)
            raise ControlHTTPError(e.code, detail)
        except Exception as e:  # noqa: BLE001
            raise ControlHTTPError(0, f"{type(e).__name__}: {e}"[:200])

    def flags(self, service: str) -> Dict[str, Dict[str, Any]]:
        doc = self._call(service, "GET", "/control/flags")
        return {r["key"]: r for r in doc.get("flags", [])}

    def set(self, service: str, key: str, value: str, confirm: bool, note: str) -> Dict[str, Any]:
        return self._call(service, "POST", f"/control/flags/{key}", {"value": value, "confirm": confirm, "note": note[:200]})

    def overlay(self, service: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /control/overlay (Market Weather). weather.py only ever sends a STAMP-ONLY body here (entries {}) until step 6."""
        return self._call(service, "POST", "/control/overlay", body)


# ----------------------------------------------------------------------------------------------------- the engine
def _now_ist(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M")


def local_change(row: Dict[str, Any], old: Optional[str], new: str) -> str:
    """LOOSENS / TIGHTENS / NEUTRAL from the row the control API describes. Deliberately eager to say LOOSENS (that only asks for a second confirming run);
    the service has the final say and answers 409 for anything it calls loosening."""
    if old is None or old == new:
        return "NEUTRAL"
    kind, looser = row.get("kind"), row.get("looser", "neutral")
    if kind == "bool":
        if looser == "neutral":
            return "NEUTRAL"
        return "LOOSENS" if new == ("true" if looser == "on" else "false") else "TIGHTENS"
    if kind == "enum":
        ch = list(row.get("choices") or [])
        if old in ch and new in ch:
            return "LOOSENS" if ch.index(new) > ch.index(old) else "TIGHTENS"
        return "NEUTRAL"
    if looser == "neutral":
        return "NEUTRAL"
    try:
        up = float(new) > float(old)
    except ValueError:
        return "NEUTRAL"
    return ("LOOSENS" if up else "TIGHTENS") if looser == "higher" else ("TIGHTENS" if up else "LOOSENS")


class AutoCalibrator:
    def __init__(self, state_path: str, control: Any, evidence: Any, notify: Optional[Callable[[str, str, str], Optional[str]]] = None,
                 rules: Sequence[Rule] = tuple(RULES), enabled: bool = True, dry_run: bool = False, cooldown_s: float = 24 * 3600, step_pct: float = 0.25,
                 now_fn: Callable[[], float] = time.time):
        for r in rules:
            if r.key in PROTECTED:
                raise ValueError(f"{r.key} is protected and cannot have an auto-calibration rule")
        self.path, self.control, self.evidence, self.notify = state_path, control, evidence, notify
        self.rules = list(rules)
        self.cooldown_s, self.step_pct, self.now = cooldown_s, step_pct, now_fn
        self.lock = threading.RLock()
        self.state: Dict[str, Any] = {"enabled": bool(enabled), "dry_run": bool(dry_run), "pinned": [], "pending": {}, "history": [], "last": None, "last_errors": {}}
        self._load()

    # -- state on disk (atomic; a damaged file is ignored)
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                for k in ("enabled", "dry_run", "pinned", "pending", "history", "last", "last_errors"):
                    if k in d:
                        self.state[k] = d[k]
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            pass

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ac", dir=os.path.dirname(self.path))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, indent=1, sort_keys=True, default=str)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001 - state is best effort; the decision itself is re-derived from the tests every time
            pass

    # -- settings
    def settings(self) -> Dict[str, Any]:
        with self.lock:
            return {"enabled": self.state["enabled"], "dry_run": self.state["dry_run"], "pinned": list(self.state["pinned"]),
                    "cooldown_h": round(self.cooldown_s / 3600, 2), "step_pct": self.step_pct}

    def configure(self, enabled: Optional[bool] = None, dry_run: Optional[bool] = None, pinned: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        with self.lock:
            if enabled is not None:
                self.state["enabled"] = bool(enabled)
            if dry_run is not None:
                self.state["dry_run"] = bool(dry_run)
            if pinned is not None:
                known = {r.key for r in self.rules}
                bad = [k for k in pinned if k not in known]
                if bad:
                    raise ValueError("not an auto-calibrated switch: " + ", ".join(bad))
                self.state["pinned"] = sorted(set(pinned))
            self._save()
            return self.settings()

    # -- one evaluation
    def evaluate(self, trigger: str = "manual", force: bool = False, dry_run: Optional[bool] = None) -> Dict[str, Any]:
        """Decide (and, unless dry-run, apply) every rule once. `force` runs even when the master switch is off (always as a dry run then)."""
        with self.lock:
            enabled = self.state["enabled"]
            dry = self.state["dry_run"] if dry_run is None else bool(dry_run)
            if not enabled:
                if not force:
                    return {"ran": False, "reason": "auto-calibration is switched off", "rows": []}
                dry = True
            ts = self.now()
            rows: List[Dict[str, Any]] = []
            flags: Dict[str, Any] = {}
            ev_cache: Dict[str, Optional[Ev]] = {}
            changed: List[Dict[str, Any]] = []
            errors: List[Dict[str, Any]] = []
            ctx = {"step_pct": self.step_pct}
            for rule in self.rules:
                row = {"key": rule.key, "service": rule.service, "title": rule.title, "tests": list(rule.tests), "explain": rule.explain,
                       "status": "", "detail": "", "from": None, "to": None, "change": None}
                rows.append(row)
                if rule.key in self.state["pinned"]:
                    row.update(status="pinned", detail="you pinned this switch: never changed automatically")
                    continue
                if rule.service not in flags:
                    try:
                        flags[rule.service] = self.control.flags(rule.service)
                    except Exception as e:  # noqa: BLE001
                        flags[rule.service] = e
                svc = flags[rule.service]
                if isinstance(svc, Exception):
                    row.update(status="service_unreachable", detail=str(svc)[:160])
                    continue
                frow = svc.get(rule.key)
                if frow is None:
                    row.update(status="not_in_catalog", detail=f"{rule.service} does not list this switch")
                    continue
                cur = frow.get("effective")
                row["from"] = cur
                evs: Dict[str, Ev] = {}
                missing = []
                for t in rule.tests:
                    if t not in ev_cache:
                        try:
                            ev_cache[t] = self.evidence.get(t)
                        except Exception:  # noqa: BLE001
                            ev_cache[t] = None
                    if ev_cache[t] is None:
                        missing.append(t)
                    else:
                        evs[t] = ev_cache[t]
                if missing:
                    self.state["pending"].pop(rule.key, None)
                    row.update(status="no_evidence", detail="no usable recent result of: " + ", ".join(missing) + " (never run, failed, or too old)")
                    continue
                try:
                    dec = rule.fn(evs, cur, ctx)
                except Exception as e:  # noqa: BLE001 - a malformed result is "no opinion"
                    dec = Decision(None, f"could not read the test result: {type(e).__name__}")
                row["detail"] = dec.reason
                row["evidence"] = {t: {"run_id": e.run_id, "finished": _now_ist(e.finished_at)} for t, e in evs.items()}
                value = dec.value
                if value is not None and frow.get("kind") in ("int", "float"):
                    try:
                        v = float(value)
                        lo, hi = frow.get("lo"), frow.get("hi")
                        v = min(max(v, lo if lo is not None else v), hi if hi is not None else v)
                        value = str(int(round(v))) if frow.get("kind") == "int" else f"{v:g}"
                    except ValueError:
                        value = None
                if value is None or value == cur:
                    self.state["pending"].pop(rule.key, None)
                    row["status"] = "ok" if value == cur else "hold"
                    continue
                kind = local_change(frow, cur, value)
                need = max(1, dec.confirms, 2 if kind == "LOOSENS" else 1)
                sig = "+".join(sorted(e.run_id for e in evs.values()))
                pend = self.state["pending"].get(rule.key)
                if not pend or pend.get("to") != value:
                    pend = {"to": value, "votes": []}
                if sig not in pend["votes"]:
                    pend["votes"].append(sig)
                self.state["pending"][rule.key] = pend
                votes = len(pend["votes"])
                row.update(to=value, change=kind, votes=votes, needed=need)
                if votes < need:
                    row.update(status="waiting", detail=f"{dec.reason} - waiting for confirmation by a second, different test run ({votes}/{need})")
                    continue
                last = next((h for h in reversed(self.state["history"]) if h.get("key") == rule.key and h.get("applied")), None)
                if last and ts - last["at"] < self.cooldown_s:
                    row.update(status="cooldown", detail=f"{dec.reason} - changed {_now_ist(last['at'])} IST, next change allowed after the cool-down")
                    continue
                if dry:
                    row["status"] = "would_change"
                    continue
                note = f"autocalib: {dec.reason}"[:200]
                try:
                    try:
                        res = self.control.set(rule.service, rule.key, value, False, note)
                    except ControlHTTPError as e:
                        if e.status == 409 and isinstance(e.detail, dict) and e.detail.get("needs_confirm"):
                            kind = "LOOSENS"
                            if votes < 2:
                                row.update(status="waiting", change=kind, needed=2, detail=f"{dec.reason} - the service calls this LOOSENING: waiting for a second, different test run ({votes}/2)")
                                continue
                            res = self.control.set(rule.service, rule.key, value, True, note)
                        else:
                            raise
                except Exception as e:  # noqa: BLE001
                    msg = str(e)[:200]
                    row.update(status="error", detail=msg)
                    if self.state["last_errors"].get(rule.key) != msg:
                        errors.append({"key": rule.key, "service": rule.service, "error": msg})
                    self.state["last_errors"][rule.key] = msg
                    continue
                self.state["last_errors"].pop(rule.key, None)
                self.state["pending"].pop(rule.key, None)
                live = bool(res.get("applied_live"))
                rec = {"at": ts, "key": rule.key, "service": rule.service, "title": rule.title, "from": cur, "to": value, "change": kind, "reason": dec.reason,
                       "evidence": row["evidence"], "applied": True, "applied_live": live, "restart_required": bool(res.get("restart_required")), "trigger": trigger}
                self.state["history"] = (self.state["history"] + [rec])[-HISTORY_KEEP:]
                row.update(status="changed", applied_live=live)
                changed.append(rec)
            report = {"ran": True, "at": ts, "trigger": trigger, "dry_run": dry, "changed": len(changed), "errors": len(errors),
                      "waiting": sum(1 for r in rows if r["status"] == "waiting"), "rows": rows}
            self.state["last"] = {k: v for k, v in report.items() if k != "rows"}
            self.state["last"]["rows"] = [{k: r.get(k) for k in ("key", "status", "from", "to")} for r in rows]
            self._save()
        self._tell(changed, errors, trigger)
        return report

    # -- Telegram
    def _tell(self, changed: List[Dict[str, Any]], errors: List[Dict[str, Any]], trigger: str) -> None:
        if not self.notify or not (changed or errors):
            return
        try:
            lines: List[str] = []
            if changed:
                lines.append(f"⚙️ Auto-calibration changed {len(changed)} switch{'es' if len(changed) != 1 else ''} (after test results):")
                for c in changed:
                    where = "applied now" if c["applied_live"] else "saved - needs a service restart"
                    lines.append(f"• {c['title']} [{c['service'].replace('-service', '')}] {c['key']}: {c['from']} → {c['to']} ({c['change']}, {where})")
                    lines.append(f"  why: {c['reason'][:180]}")
                    ev = ", ".join(f"{t} {e['finished']}" for t, e in (c.get("evidence") or {}).items())
                    if ev:
                        lines.append(f"  test: {ev} IST")
            for e in errors:
                lines.append(f"❌ Could not change {e['key']} on {e['service']}: {e['error'][:160]}")
            lines += ["", "Undo / pin: Real Money Calibration > Auto-calibration."]
            self.notify("Stockky auto-calibration", "\n".join(lines)[:1500], "high" if errors else "normal")
        except Exception:  # noqa: BLE001 - a Telegram problem never undoes a change
            pass

    # -- what the page shows
    def status(self, with_coverage: bool = True) -> Dict[str, Any]:
        with self.lock:
            out = {"settings": self.settings(), "pending": dict(self.state["pending"]), "last": self.state.get("last"),
                   "history": list(reversed(self.state["history"][-50:])), "rules": [
                       {"key": r.key, "service": r.service, "title": r.title, "tests": list(r.tests), "explain": r.explain, "pinned": r.key in self.state["pinned"]}
                       for r in self.rules], "evidence_jobs": EVIDENCE_JOBS, "protected": PROTECTED}
        if with_coverage:
            out["coverage"] = self.coverage()
        return out

    def coverage(self) -> Dict[str, Any]:
        """Every switch of both services: automated (with its test), protected (with the reason) or manual (no test exists yet)."""
        by_key = {(r.service, r.key): r for r in self.rules}
        res: Dict[str, Any] = {"switches": [], "unreachable": []}
        for svc in (RT, PS):
            try:
                fl = self.control.flags(svc)
            except Exception as e:  # noqa: BLE001
                res["unreachable"].append({"service": svc, "error": str(e)[:120]})
                continue
            for k, row in fl.items():
                r = by_key.get((svc, k))
                if r:
                    st, why = "automated", f"decided by {', '.join(r.tests)}: {r.explain}"
                elif k in PROTECTED:
                    st, why = "protected", PROTECTED[k]
                else:
                    st, why = "manual", "no test result decides this one yet: change it on the page"
                res["switches"].append({"service": svc, "key": k, "title": row.get("title"), "status": st, "why": why, "effective": row.get("effective")})
        n = res["switches"]
        res["counts"] = {s: sum(1 for x in n if x["status"] == s) for s in ("automated", "protected", "manual")}
        return res

    # -- runner hook
    def on_run_finished(self, run: Dict[str, Any], packs: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Called after every run. Evaluates only when the run could have produced evidence. Never raises."""
        try:
            st = str(run.get("status"))
            if st not in ("success", "attention") or run.get("kind") not in ("job", "pack"):
                return None
            target = run.get("target")
            ids = {target} if run.get("kind") == "job" else {j for j, _ in (packs.get(target) or {}).get("jobs", [])}
            if not ids & set(EVIDENCE_JOBS):
                return None
            return self.evaluate(trigger=f"run {target}")
        except Exception:  # noqa: BLE001
            return None
