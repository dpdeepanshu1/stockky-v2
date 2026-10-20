#!/usr/bin/env python3
"""calibration_snapshot.py - snapshot a calibration run (gate settings + metrics) and compare two snapshots (group 468, item 10).

  python3 scripts/calibration_snapshot.py snapshot --out-dir calibration_out --env ~/stockky-v2/.env --label before-chase-change [--store calibration_snapshots]
  python3 scripts/calibration_snapshot.py compare calibration_snapshots/A.json calibration_snapshots/B.json [--json diff.json]
  python3 scripts/calibration_snapshot.py compare-last --store calibration_snapshots        # the two newest snapshots (what the runner page uses)
  TOKEN=<admin token> python3 scripts/calibration_snapshot.py snapshot --out-dir D --label NAME --rt-url http://real-trade-service:8005 --ps-url http://position-stocks-service:8006
  python3 scripts/calibration_snapshot.py list [--store calibration_snapshots]

A snapshot = the gate settings in force (env lines whose names start with STRUCTURE_, PS_STRUCTURE_, CANDIDATE_, ENTRY_, RISK_ or
TIMESFM_, read from --env and, for names the file lacks, from this process's environment) + per-seed A/B metrics read from
OUT_DIR/ab_<seed>/ab_report.json (verdict, edge in points, bootstrap 90% CI, signal counts, kept %) + whether each seed is COMPLETE
by the same rule as calibration_status.py. Names containing KEY, TOKEN, SECRET, PASSWORD, PASS, TOTP, PIN or DSN are never stored.
With --rt-url / --ps-url (and TOKEN) the settings also include every switch of GET /control/flags of those services, as `real-trade:KEY` / `position-stocks:KEY`
with its EFFECTIVE (running) value; that is what the Real Money Calibration runner uses, because it has no .env of its own.
`compare` prints which settings changed and how every metric moved per seed, and says plainly when the two runs are not comparable
(different seeds, or a seed incomplete in either). It applies nothing and changes no .env. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PREFIXES = ("STRUCTURE_", "PS_STRUCTURE_", "CANDIDATE_", "ENTRY_", "RISK_", "TIMESFM_")
SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASS|TOTP|PIN|DSN)", re.I)


def _wanted(name: str) -> bool:
    return name.startswith(PREFIXES) and not SECRET_RE.search(name)


def read_env_file(path: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path:
        return out
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if _wanted(k):
                out[k] = v
    except OSError:
        pass
    return out


def gate_settings(env_path: Optional[str], environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The env file's settings, plus process-environment values for gate names the file does not set. Secrets never included."""
    s = read_env_file(env_path)
    for k, v in (os.environ if environ is None else environ).items():
        if _wanted(k) and k not in s and v.strip():
            s[k] = v.strip()
    return dict(sorted(s.items()))


def _get_json(url: str, token: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
        return json.loads(r.read().decode("utf-8", "replace"))


def live_flags(tag: str, url: str, token: str, getter=None) -> Dict[str, str]:
    """{`tag:KEY`: effective value} from GET <url>/control/flags. Empty on any failure (the caller says so); secrets are never in that catalog,
    and a key that looks secret is dropped anyway."""
    out: Dict[str, str] = {}
    try:
        doc = (getter or _get_json)(url.rstrip("/") + "/control/flags", token)
        for f in (doc or {}).get("flags") or []:
            k = str(f.get("key") or "")
            if k and not SECRET_RE.search(k) and f.get("effective") is not None:
                out[f"{tag}:{k}"] = str(f.get("effective"))
    except Exception as e:  # noqa: BLE001
        print(f"warning: could not read {tag} /control/flags: {type(e).__name__}: {e}", file=sys.stderr)
    return out


def _json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def seed_metrics(out_dir: Path, min_signals: int = 30) -> Dict[str, Dict[str, Any]]:
    """{seed: metrics} for every ab_<seed>/ab_report.json in out_dir."""
    res: Dict[str, Dict[str, Any]] = {}
    for d in sorted(out_dir.glob("ab_*")):
        rep = _json(d / "ab_report.json")
        if rep is None:
            continue
        seed = d.name[3:]
        sig = rep.get("signals") or {}
        ci = (rep.get("bootstrap") or {}).get("ci90")
        verdict = rep.get("verdict")
        base_n = sig.get("baseline") or 0
        res[seed] = {
            "verdict": verdict, "edge_pp": rep.get("edge_pp"), "ci90": ci,
            "baseline_n": sig.get("baseline"), "candidate_n": sig.get("candidate"), "rejected_n": sig.get("rejected"),
            "kept_pct": rep.get("kept_pct"),
            "candidate_mean_net": (rep.get("candidate") or {}).get("mean_net"),
            "baseline_mean_net": (rep.get("baseline") or {}).get("mean_net"),
            "complete": bool(verdict and verdict != "no_data" and base_n >= min_signals),
        }
    return res


def make_snapshot(out_dir: Path, env_path: Optional[str], label: str, environ: Optional[Dict[str, str]] = None,
                  now: Optional[datetime] = None, flags: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    settings = gate_settings(env_path, environ)
    settings.update(flags or {})
    return {"label": label, "created_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "settings": dict(sorted(settings.items())), "seeds": seed_metrics(out_dir)}


def two_newest(store: Path) -> Optional[tuple]:
    """(older, newer) snapshot dicts: the two newest files of `store` by their time-stamped names. None with fewer than two readable ones."""
    docs = [d for d in (_json(p) for p in sorted(store.glob("*.json"))) if d and d.get("seeds") is not None]
    return (docs[-2], docs[-1]) if len(docs) >= 2 else None


def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-") or "snapshot"


def save_snapshot(snap: Dict[str, Any], store: Path) -> Path:
    store.mkdir(parents=True, exist_ok=True)
    stamp = snap["created_at"].replace(":", "").replace("-", "")[:15]
    p = store / f"{stamp}_{_slug(snap['label'])}.json"
    p.write_text(json.dumps(snap, indent=1))
    return p


def _delta(a: Any, b: Any) -> Optional[float]:
    try:
        return round(float(b) - float(a), 3)
    except (TypeError, ValueError):
        return None


def compare(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Pure. Settings diff and per-seed metric movement from snapshot a (before) to b (after)."""
    sa, sb = a.get("settings") or {}, b.get("settings") or {}
    settings = {"changed": {k: [sa[k], sb[k]] for k in sorted(set(sa) & set(sb)) if sa[k] != sb[k]},
                "added": {k: sb[k] for k in sorted(set(sb) - set(sa))},
                "removed": {k: sa[k] for k in sorted(set(sa) - set(sb))}}
    ma, mb = a.get("seeds") or {}, b.get("seeds") or {}
    seeds: Dict[str, Any] = {}
    for seed in sorted(set(ma) | set(mb)):
        x, y = ma.get(seed), mb.get(seed)
        if x is None or y is None:
            seeds[seed] = {"only_in": "after" if x is None else "before"}
            continue
        seeds[seed] = {
            "verdict": [x.get("verdict"), y.get("verdict")],
            "edge_pp": [x.get("edge_pp"), y.get("edge_pp"), _delta(x.get("edge_pp"), y.get("edge_pp"))],
            "ci90": [x.get("ci90"), y.get("ci90")],
            "kept_pct": [x.get("kept_pct"), y.get("kept_pct"), _delta(x.get("kept_pct"), y.get("kept_pct"))],
            "candidate_n": [x.get("candidate_n"), y.get("candidate_n")],
            "candidate_mean_net": [x.get("candidate_mean_net"), y.get("candidate_mean_net"), _delta(x.get("candidate_mean_net"), y.get("candidate_mean_net"))],
            "complete": [x.get("complete"), y.get("complete")],
        }
    shared = [s for s, v in seeds.items() if "only_in" not in v]
    warnings: List[str] = []
    if not shared:
        warnings.append("no seed appears in both snapshots: the runs are not comparable")
    for s in set(seeds) - set(shared):
        warnings.append(f"seed {s} is only in the {seeds[s]['only_in']} snapshot")
    for s in shared:
        if not all(seeds[s]["complete"]):
            warnings.append(f"seed {s} is incomplete in at least one snapshot: its numbers do not count")
    if not any(settings.values()):
        warnings.append("the gate settings are identical: any metric change comes from the data or the code, not from a setting")
    return {"before": a.get("label"), "after": b.get("label"), "settings": settings, "seeds": seeds, "warnings": warnings}


def to_markdown(diff: Dict[str, Any]) -> str:
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    L = [f"# Calibration compare: {diff['before']} -> {diff['after']}", "", "## Settings", ""]
    st = diff["settings"]
    if not any(st.values()):
        L.append("No gate setting differs.")
    for k, (x, y) in st["changed"].items():
        L.append(f"- changed `{k}`: {x} -> {y}")
    for k, v in st["added"].items():
        L.append(f"- added `{k}` = {v}")
    for k, v in st["removed"].items():
        L.append(f"- removed `{k}` (was {v})")
    L += ["", "## Metrics per seed", "", "| seed | verdict | edge pp (before -> after, change) | kept % | candidate n | candidate mean net | complete |", "|---|---|---|---|---|---|---|"]
    for seed, v in diff["seeds"].items():
        if "only_in" in v:
            L.append(f"| {seed} | only in the {v['only_in']} snapshot | | | | | |")
            continue
        e, k, n, m = v["edge_pp"], v["kept_pct"], v["candidate_n"], v["candidate_mean_net"]
        L.append(f"| {seed} | {f(v['verdict'][0])} -> {f(v['verdict'][1])} | {f(e[0])} -> {f(e[1])} ({f(e[2])}) | {f(k[0])} -> {f(k[1])} | "
                 f"{f(n[0])} -> {f(n[1])} | {f(m[0])} -> {f(m[1])} ({f(m[2])}) | {f(v['complete'][0])}/{f(v['complete'][1])} |")
    if diff["warnings"]:
        L += ["", "## Read this first", ""] + [f"- {w}" for w in diff["warnings"]]
    L += ["", "A change in edge that stays inside the bootstrap CI is noise. Settings are never applied by this tool."]
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], epilog="snapshot flags: --out-dir DIR --env FILE --label NAME --store DIR "
                                 "--rt-url URL --ps-url URL --token T (or env TOKEN); compare-last flags: --store DIR --json FILE")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("snapshot")
    s1.add_argument("--out-dir", required=True)
    s1.add_argument("--env", default="")
    s1.add_argument("--label", required=True)
    s1.add_argument("--store", default="calibration_snapshots")
    s1.add_argument("--rt-url", default="")
    s1.add_argument("--ps-url", default="")
    s1.add_argument("--token", default=os.environ.get("TOKEN", ""))
    s2 = sub.add_parser("compare")
    s2.add_argument("before")
    s2.add_argument("after")
    s2.add_argument("--json", default="")
    s4 = sub.add_parser("compare-last")
    s4.add_argument("--store", default="calibration_snapshots")
    s4.add_argument("--json", default="")
    s3 = sub.add_parser("list")
    s3.add_argument("--store", default="calibration_snapshots")
    a = ap.parse_args(argv)
    if a.cmd == "snapshot":
        flags: Dict[str, str] = {}
        if a.rt_url:
            flags.update(live_flags("real-trade", a.rt_url, a.token))
        if a.ps_url:
            flags.update(live_flags("position-stocks", a.ps_url, a.token))
        snap = make_snapshot(Path(a.out_dir), a.env or None, a.label, flags=flags)
        if not snap["settings"]:
            print("warning: no settings captured (no .env readable, no gate names in the environment, no /control/flags): a later compare "
                  "will not show setting changes", file=sys.stderr)
        if not snap["seeds"]:
            print(f"no ab_*/ab_report.json found in {a.out_dir}: run the calibration first", file=sys.stderr)
            return 1
        print(f"saved {save_snapshot(snap, Path(a.store))} ({len(snap['settings'])} settings, {len(snap['seeds'])} seed(s))")
        return 0
    if a.cmd == "compare-last":
        pair = two_newest(Path(a.store))
        if pair is None:
            print(f"fewer than two snapshots in {a.store}: take one before and one after a settings change (job: Calibration snapshot)")
            return 1
        diff = compare(*pair)
        print(to_markdown(diff))
        if a.json:
            Path(a.json).write_text(json.dumps(diff, indent=1))
        return 0
    if a.cmd == "list":
        for p in sorted(Path(a.store).glob("*.json")):
            d = _json(p) or {}
            print(f"{p.name}  {d.get('label')}  seeds={','.join((d.get('seeds') or {}).keys())}")
        return 0
    x, y = _json(Path(a.before)), _json(Path(a.after))
    if x is None or y is None:
        print("cannot read one of the snapshot files", file=sys.stderr)
        return 1
    diff = compare(x, y)
    print(to_markdown(diff))
    if a.json:
        Path(a.json).write_text(json.dumps(diff, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
