#!/usr/bin/env python3
"""scripts/strategy_registry.py - the strategy registry and lifecycle: one status per strategy, moved only by repeated evidence. (E578, round 553)

Reads the JSON written by scripts/evidence_report.py (its `strategy` group: source_tab | decision_label) and keeps ONE small state file (--store) with, per strategy:
a lifecycle status, the latest evidence, a streak of agreeing verdicts and a short history of status changes.

Status (a record of what the evidence says, never an instruction and never a switch):
  testing   default for a strategy seen for the first time, or one whose evidence is mixed
  proven    the evidence report said POSITIVE on 3 separate days in a row
  watch     was proven, and the verdict has been NOT_PROVEN on 3 separate days in a row
  demoted   the evidence report said NEGATIVE on 3 separate days in a row (from testing, watch or proven)
  retired   set by hand with --set; the script never moves it again

Rules: NOT_ENOUGH_DATA counts as no information (the streak and status stay as they are). A verdict that differs from the streak's verdict restarts the streak at 1.
A status changes only when the streak reaches --confirm (3) days, and the streak counts one evidence DATE once (a re-run on the same day refreshes the numbers, never the streak).
demoted -> testing after 3 days in a row of NOT_PROVEN; proven / watch / demoted -> demoted after 3 days of NEGATIVE; testing / watch -> proven after 3 days of POSITIVE.
An evidence report older than --max-age-days (3) is refused (exit 1) so a stale file can never advance a streak.
Drift (optional, --drift drift_monitor.json from scripts/drift_monitor.py, round 554): a PROVEN strategy whose recent trades read DECAY on 3 separate days in a row moves to `watch`
(any other state on a new day restarts that count). Drift never promotes, never demotes to `demoted`, and is refused when older than --max-age-days like the evidence.

It changes no switch, no setting and no trade. It writes only the --store file and the --json file you name.

  python3 scripts/strategy_registry.py --evidence evidence_report.json --store /data/runner/state/strategy_registry.json --json registry.json
  python3 scripts/strategy_registry.py --store /data/runner/state/strategy_registry.json --set "swing | BUY NOW=retired"     # manual: retired / testing / proven / watch / demoted

Exit 0 written, 1 needs a look (no or stale or unreadable evidence). Pure standard library.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

STATUSES = ("testing", "proven", "watch", "demoted", "retired")
CONFIRM = 3                    # separate evidence days in a row before a status moves
MAX_AGE_DAYS = 3
HISTORY_KEEP = 30
GROUP = "strategy"
LAST_FIELDS = ("n", "verdict", "mean_net_ret_pct", "lower90_pct", "dsr_prob", "win_pct")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def next_status(status: str, verdict: str, streak_n: int, confirm: int = CONFIRM) -> Tuple[str, Optional[str]]:
    """(new status, why or None) after the streak of `verdict` reached streak_n days. Pure. retired never moves."""
    if status == "retired" or streak_n < confirm:
        return status, None
    if verdict == "POSITIVE" and status in ("testing", "watch"):
        return "proven", f"{confirm} days in a row of POSITIVE"
    if verdict == "NEGATIVE" and status in ("testing", "watch", "proven"):
        return "demoted", f"{confirm} days in a row of NEGATIVE"
    if verdict == "NOT_PROVEN" and status == "proven":
        return "watch", f"{confirm} days in a row of NOT_PROVEN"
    if verdict == "NOT_PROVEN" and status == "demoted":
        return "testing", f"{confirm} days in a row of NOT_PROVEN"
    return status, None


def load_store(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if isinstance(doc, dict) and isinstance(doc.get("strategies"), dict):
            return doc
    except Exception:  # noqa: BLE001 - a missing or damaged file starts a fresh registry
        pass
    return {"version": 1, "strategies": {}, "updated": None}


def save_store(path: str, doc: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def evidence_day(rep: Dict[str, Any]) -> Optional[str]:
    """The UTC date the evidence report was made (YYYY-MM-DD), or None when it cannot be read."""
    try:
        d = str(rep.get("generated_at") or "")[:10]
        date.fromisoformat(d)
        return d
    except Exception:  # noqa: BLE001
        return None


def update(store: Dict[str, Any], rep: Dict[str, Any], today: str, confirm: int = CONFIRM) -> List[Dict[str, Any]]:
    """Apply one evidence report to the registry in place. Returns the status changes made (for the report). Never raises on odd rows."""
    day = evidence_day(rep) or today
    cells = (rep.get("groups") or {}).get(GROUP) or {}
    changes: List[Dict[str, Any]] = []
    for key, c in cells.items():
        if not isinstance(c, dict):
            continue
        verdict = str(c.get("verdict") or "")
        s = store["strategies"].setdefault(str(key), {"status": "testing", "first_seen": day, "last_seen": None, "streak": {"verdict": None, "days": 0, "last_day": None},
                                                       "last": {}, "history": []})
        s["last"] = {k: c.get(k) for k in LAST_FIELDS}
        st = s.setdefault("streak", {"verdict": None, "days": 0, "last_day": None})
        if verdict in ("NOT_ENOUGH_DATA", ""):
            s["last_seen"] = day
            continue                                                    # no information: streak and status stay
        if st.get("last_day") != day:                                   # one evidence date counts once
            if st.get("verdict") == verdict:
                st["days"] = int(st.get("days") or 0) + 1
            else:
                st["verdict"], st["days"] = verdict, 1
            st["last_day"] = day
            new, why = next_status(s["status"], verdict, st["days"], confirm)
            if new != s["status"]:
                s["history"] = (s.get("history") or [])[-(HISTORY_KEEP - 1):] + [{"date": day, "from": s["status"], "to": new, "why": why}]
                changes.append({"strategy": key, "from": s["status"], "to": new, "why": why})
                s["status"] = new
                st["verdict"], st["days"] = verdict, 0                  # the streak is spent: the next move needs a fresh run of days
        s["last_seen"] = day
    store["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return changes


def apply_drift(store: Dict[str, Any], drift: Dict[str, Any], today: str, confirm: int = CONFIRM) -> List[Dict[str, Any]]:
    """Round 554: a PROVEN strategy moves to `watch` after `confirm` separate days in a row of DECAY in the drift monitor. Pure bookkeeping; returns the changes made."""
    day = evidence_day(drift) or today
    changes: List[Dict[str, Any]] = []
    for key, c in (drift.get("strategies") or {}).items():
        s = store["strategies"].get(str(key))
        if not s or not isinstance(c, dict):
            continue
        state = str(c.get("state") or "")
        s["drift"] = {"state": state, "z": c.get("z"), "day": day}
        ds = s.setdefault("drift_streak", {"days": 0, "last_day": None})
        if state in ("NOT_ENOUGH_DATA", "") or ds.get("last_day") == day:
            continue
        ds["last_day"] = day
        ds["days"] = int(ds.get("days") or 0) + 1 if state == "DECAY" else 0
        if s.get("status") == "proven" and ds["days"] >= confirm:
            why = f"{confirm} days in a row of DECAY (recent trades far below its own history)"
            s["history"] = (s.get("history") or [])[-(HISTORY_KEEP - 1):] + [{"date": day, "from": "proven", "to": "watch", "why": why}]
            changes.append({"strategy": key, "from": "proven", "to": "watch", "why": why})
            s["status"] = "watch"
            ds["days"] = 0
    return changes


def set_status(store: Dict[str, Any], key: str, status: str, today: str) -> str:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    s = store["strategies"].setdefault(key, {"status": "testing", "first_seen": today, "last_seen": None, "streak": {"verdict": None, "days": 0, "last_day": None}, "last": {}, "history": []})
    old = s["status"]
    if old != status:
        s["history"] = (s.get("history") or [])[-(HISTORY_KEEP - 1):] + [{"date": today, "from": old, "to": status, "why": "set by hand"}]
        s["status"] = status
        s["streak"] = {"verdict": None, "days": 0, "last_day": None}
    return old


def render(store: Dict[str, Any], changes: List[Dict[str, Any]], note: str = "") -> str:
    rows = sorted(store["strategies"].items(), key=lambda kv: (STATUSES.index(kv[1]["status"]) if kv[1].get("status") in STATUSES else 9, kv[0]))
    lines = [f"STRATEGY REGISTRY  {store.get('updated') or '-'}  ({len(rows)} strategies; a status moves after {CONFIRM} separate days in a row; it moves no switch)", ""]
    for k, s in rows:
        la = s.get("last") or {}
        stk = s.get("streak") or {}
        lines.append(f"   {k[:36]:<36} {s['status']:<8} last={la.get('verdict') or '-':<16} n={la.get('n', '-')!s:<4} mean={la.get('mean_net_ret_pct')}% lo90={la.get('lower90_pct')}%  "
                     f"streak={stk.get('verdict') or '-'} x{stk.get('days') or 0}")
    if changes:
        lines += ["", "STATUS CHANGES THIS RUN:"] + [f"   {c['strategy']}: {c['from']} -> {c['to']} ({c['why']})" for c in changes]
        dem = [c["strategy"] for c in changes if c["to"] == "demoted"]
        if dem:
            lines.append("   DEMOTED means the evidence is clearly negative after costs: decide yourself whether to switch it off. Nothing was changed.")
    if note:
        lines += ["", note]
    lines += ["", "Evidence bookkeeping only. A status never changes a switch; auto-calibration and your own click do that."]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--evidence", default="", help="evidence_report.json (from scripts/evidence_report.py --json)")
    ap.add_argument("--store", required=True, help="the registry state file (kept between runs)")
    ap.add_argument("--drift", default="", help="drift_monitor.json (from scripts/drift_monitor.py --json): 3 days of DECAY move a proven strategy to watch")
    ap.add_argument("--json", default="", help="write the registry as JSON here as well")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=STATUS", help="set a status by hand (repeatable), e.g. \"swing | BUY NOW=retired\"")
    ap.add_argument("--confirm", type=int, default=CONFIRM)
    ap.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS)
    ap.add_argument("--today", default="", help="YYYY-MM-DD (tests)")
    a = ap.parse_args(argv)
    today = a.today or _today()
    store = load_store(a.store)
    changes: List[Dict[str, Any]] = []
    note, code = "", 0
    for item in a.set:
        key, _, status = item.rpartition("=")
        try:
            old = set_status(store, key.strip(), status.strip(), today)
            changes.append({"strategy": key.strip(), "from": old, "to": status.strip(), "why": "set by hand"})
        except ValueError as e:
            print(f"strategy_registry: {e}", file=sys.stderr)
            return 2
    if a.evidence:
        try:
            with open(a.evidence, encoding="utf-8") as fh:
                rep = json.load(fh)
            day = evidence_day(rep)
            if day is None:
                raise ValueError("no generated_at date in the evidence file")
            age = (date.fromisoformat(today) - date.fromisoformat(day)).days
            if age > a.max_age_days:
                note, code = f"Evidence report is {age} days old (limit {a.max_age_days}): not used, no streak advanced.", 1
            elif (rep.get("groups") or {}).get(GROUP) is None:
                note, code = "The evidence report has no `strategy` group (made by an older evidence_report.py): not used.", 1
            else:
                changes += update(store, rep, today, max(1, a.confirm))
        except Exception as e:  # noqa: BLE001
            note, code = f"Evidence file could not be read ({type(e).__name__}): not used.", 1
    elif not a.set:
        note, code = "No evidence file given (run export_records and evidence_report first).", 1
    if a.drift and code == 0:
        try:
            with open(a.drift, encoding="utf-8") as fh:
                drep = json.load(fh)
            dday = evidence_day(drep)
            if dday is None:
                raise ValueError("no generated_at date in the drift file")
            dage = (date.fromisoformat(today) - date.fromisoformat(dday)).days
            if dage > a.max_age_days:
                note, code = f"Drift report is {dage} days old (limit {a.max_age_days}): not used.", 1
            else:
                changes += apply_drift(store, drep, today, max(1, a.confirm))
        except Exception as e:  # noqa: BLE001
            note, code = f"Drift file could not be read ({type(e).__name__}): not used.", 1
    print(render(store, changes, note))
    save_store(a.store, store)
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({**store, "changes": changes, "note": note}, fh, indent=1, sort_keys=True)
        os.replace(a.json + ".tmp", a.json)
    return code


if __name__ == "__main__":
    sys.exit(main())
