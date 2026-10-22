#!/usr/bin/env python3
"""Fail when a file holds something that looks like a live secret (F32, 2026-10-22).

Why: a full admin JWT sat in a saved calibration run's output.txt and an Upstash token sat in .env.example (both found by hand, both
had to be rotated). Nothing in CI looked for either. This scanner is stdlib-only, offline and never prints a secret: a finding shows
file:line, the rule, the first 4 characters, the length and a short fingerprint (sha256, 12 hex) that you can put in
scripts/secrets_allowlist.txt when a hit is a known false positive.

Rules: JWT, private-key block, AWS access key, Telegram bot token, GitHub / Slack token, password inside a connection URL
(postgres / redis / rediss / mysql / amqp / mongodb), a high-entropy value assigned to a NAME that contains TOKEN / SECRET / PASSWORD /
API_KEY / ACCESS_KEY / PRIVATE_KEY, and file names that should never be committed (wallets, keys, .env, saved run output).
Placeholders (changeme, your-..., <...>, ${...}, xxxx, example, generate ...) are ignored.

  python3 scripts/check_secrets.py              # whole repo, exit 1 + a redacted list when something is found
  python3 scripts/check_secrets.py PATH ...     # only these files / folders
  python3 scripts/check_secrets.py --list-rules
"""
from __future__ import annotations

import hashlib
import math
import re
import sys
from pathlib import Path
from typing import Iterator, List, NamedTuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _repo_walk  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = Path(__file__).resolve().parent / "secrets_allowlist.txt"
MAX_BYTES = 2_000_000
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2", ".ttf", ".pyc", ".parquet", ".sqlite", ".db"}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "poetry.lock"}   # integrity hashes, not secrets
SKIP_DIRS = {".git"}
CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs"}   # NAME = value in code is an identifier far more often than a secret

JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----")
AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
TELEGRAM = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")
GH_SLACK = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|xox[baprs]-[A-Za-z0-9-]{10,})\b")
URL_PASSWORD = re.compile(r"\b(?:postgres(?:ql)?(?:\+\w+)?|rediss?|mysql|amqps?|mongodb(?:\+srv)?)://[^:/\s@'\"]+:([^@\s/'\"]{6,})@")
ASSIGN = re.compile(r"""(?ix)
    (?P<name>[A-Z0-9_.-]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)[A-Z0-9_.-]*)   # NAME
    ["']?\s*[:=]\s*["']?                                                                                # = or :
    (?P<val>[A-Za-z0-9+/_\-.=!@#$%^&*]{16,})                                                            # value
""")
PLACEHOLDER = re.compile(r"(?i)change.?me|your[-_ ]|<[^>]*>|\$\{|\$\(|xxxx|example|generate|placeholder|replace|redacted|dummy|sample|\.\.\.|secret.?here|todo|none|null|true|false|not.?set|^\$|^pass(?:word)?$|^[A-Z][A-Z_]{3,}$")
FORBIDDEN_NAMES = {"ewallet.p12", "cwallet.sso", "id_rsa", "id_ed25519", "keystore.jks"}
FORBIDDEN_SUFFIXES = {".pem", ".pfx", ".p12", ".key", ".token", ".jwt"}
FORBIDDEN_PREFIXES = ("run_output",)
RULES = ["jwt", "private-key", "aws-key", "telegram-token", "github-slack-token", "url-password", "assigned-secret", "forbidden-file"]


class Finding(NamedTuple):
    path: str
    line: int
    rule: str
    first4: str
    length: int
    fp: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.first4}... (len {self.length}) fp={self.fp}"


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:12]


def entropy(s: str) -> float:
    if not s:
        return 0.0
    return -sum((s.count(c) / len(s)) * math.log2(s.count(c) / len(s)) for c in set(s))


def load_allowlist(path: Path = ALLOWLIST) -> set:
    """Fingerprints (first column) of known false positives; '#' starts a comment."""
    out = set()
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.split("#", 1)[0].strip()
            if ln:
                out.add(ln.split()[0])
    except OSError:
        pass
    return out


def _is_placeholder(v: str) -> bool:
    return bool(PLACEHOLDER.search(v))


def scan_text(text: str, rel: str, allow: Optional[set] = None) -> List[Finding]:
    """Findings for one file's text. Pure; never raises."""
    allow = allow or set()
    code = Path(rel).suffix in CODE_SUFFIXES
    out: List[Finding] = []

    def add(n: int, rule: str, value: str) -> None:
        fp = fingerprint(value)
        if fp not in allow:
            out.append(Finding(rel, n, rule, value[:4], len(value), fp))

    for n, line in enumerate(text.splitlines(), 1):
        if len(line) > 4000:
            line = line[:4000]
        for m in JWT.finditer(line):
            add(n, "jwt", m.group(0))
        for m in PRIVATE_KEY.finditer(line):
            add(n, "private-key", m.group(0))
        for m in AWS_KEY.finditer(line):
            add(n, "aws-key", m.group(0))
        for m in TELEGRAM.finditer(line):
            add(n, "telegram-token", m.group(0))
        for m in GH_SLACK.finditer(line):
            add(n, "github-slack-token", m.group(0))
        for m in URL_PASSWORD.finditer(line):
            if not _is_placeholder(m.group(1)):
                add(n, "url-password", m.group(1))
        for m in ([] if code else ASSIGN.finditer(line)):
            v = m.group("val").rstrip(".,;)")
            if len(v) >= 16 and not _is_placeholder(v) and entropy(v) >= 3.3 and not re.fullmatch(r"[A-Za-z_.\[\]()]+", v) and not JWT.search(v) and "/" not in v[:1]:
                add(n, "assigned-secret", v)
    return out


def forbidden_file(p: Path) -> bool:
    n = p.name
    if n in FORBIDDEN_NAMES or p.suffix in FORBIDDEN_SUFFIXES or n.startswith(FORBIDDEN_PREFIXES):
        return True
    return n == ".env" or (n.startswith(".env.") and n not in {".env.example", ".env.oracle.example", ".env.oracle.recommended"})


def iter_files(paths: List[Path]) -> Iterator[Path]:
    import os
    for base in paths:
        if base.is_file():
            yield base
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            here = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not _repo_walk.is_vendored_dir(here / d))
            for f in sorted(filenames):
                yield here / f


def scan_paths(paths: List[Path], allow: Optional[set] = None) -> List[Finding]:
    allow = load_allowlist() if allow is None else allow
    out: List[Finding] = []
    for p in iter_files(paths):
        try:
            rel = str(p.resolve().relative_to(ROOT))
        except ValueError:
            rel = str(p)
        if p.name == ".gitkeep":
            continue
        if forbidden_file(p):
            out.append(Finding(rel, 0, "forbidden-file", p.name[:4], len(p.name), fingerprint(rel)))
            continue
        if p.suffix in SKIP_SUFFIXES or p.name in SKIP_NAMES:
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:2048]:
            continue
        out.extend(scan_text(raw.decode("utf-8", "ignore"), rel, allow))
    return out


def main(argv: List[str]) -> int:
    if "--list-rules" in argv:
        print("\n".join(RULES))
        return 0
    targets = [Path(a) for a in argv if not a.startswith("--")] or [ROOT]
    found = scan_paths(targets)
    if not found:
        print("ok: no secret-looking values found")
        return 0
    print(f"FOUND {len(found)} possible secret(s) - values are redacted. Rotate anything real, then delete it from the file "
          f"(and from every copy / zip). A known false positive: add its fp to scripts/secrets_allowlist.txt.")
    for f in found:
        print("  " + f.render())
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
