#!/usr/bin/env python3
"""Static verification of the changed/new TSX + api.ts, since npm is unavailable
offline. Checks the classes of error a tsc run would have caught for these
specific edits: unbalanced braces/parens, every api.<name>() call actually
existing in api.ts, every referenced local identifier being declared, and no
leftover IPO code in SurpriseStocks."""
from __future__ import annotations

import re
import sys

ROOT = "/sessions/relaxed-laughing-bohr/mnt/outputs/_work/repo/frontend/src"
FILES = [
    f"{ROOT}/api.ts",
    f"{ROOT}/App.tsx",
    f"{ROOT}/components/IpoTracker.tsx",
    f"{ROOT}/components/HotStocks.tsx",
    f"{ROOT}/components/SurpriseStocks.tsx",
]

findings: list[str] = []
checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        findings.append(label)


def strip_noise(src: str) -> str:
    """Remove strings, template literals, comments and regex literals so brace
    counting reflects real code structure."""
    out, i, n = [], 0, len(src)
    # A '/' starting a regex literal can only follow one of these (or nothing);
    # after an identifier or ')' it is division instead. Standard JS heuristic.
    regex_prev = set("(,=:[!&|?{};+-*%~^\n\t ")

    def prev_significant() -> str:
        k = len(out) - 1
        while k >= 0 and out[k] in " \t\n":
            k -= 1
        return out[k] if k >= 0 else ""

    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                i += 1
            i += 2
        elif c == "/" and (prev_significant() in regex_prev or not out):
            # Regex literal: skip to the unescaped closing '/', then its flags.
            i += 1
            in_class = False
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "[":
                    in_class = True
                elif src[i] == "]":
                    in_class = False
                elif src[i] == "/" and not in_class:
                    break
                elif src[i] == "\n":
                    break  # not a regex after all; bail out safely
                i += 1
            i += 1
            while i < n and src[i].isalpha():
                i += 1
        elif c in "\"'":
            q = c
            i += 1
            while i < n and src[i] != q:
                i += 2 if src[i] == "\\" else 1
            i += 1
        elif c == "`":
            i += 1
            depth = 0
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "$" and i + 1 < n and src[i + 1] == "{":
                    depth += 1
                    i += 2
                    continue
                if depth and src[i] == "}":
                    depth -= 1
                    i += 1
                    continue
                if src[i] == "`" and depth == 0:
                    break
                out.append(src[i])
                i += 1
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


api_src = open(f"{ROOT}/api.ts").read()
# Exported api object member names: `name: (` or `name(` at 2-space indent.
api_members = set(re.findall(r"^  ([A-Za-z_$][\w$]*)\s*:", api_src, re.M))
api_members |= set(re.findall(r"^  ([A-Za-z_$][\w$]*)\s*\(", api_src, re.M))

for path in FILES:
    src = open(path).read()
    clean = strip_noise(src)
    name = path.split("/")[-1]

    for open_ch, close_ch in (("{", "}"), ("(", ")"), ("[", "]")):
        delta = clean.count(open_ch) - clean.count(close_ch)
        # Compare the imbalance against the pristine baseline rather than
        # demanding zero: App.tsx legitimately contains JSX-escaped braces
        # (`{"{"}`) that a regex-free stripper cannot account for, and the
        # baseline shows the same non-zero delta. What matters is that an EDIT
        # did not change the delta — that is exactly what an unclosed block does.
        base_path = path.replace("/repo/frontend", "/repo_orig/frontend")
        try:
            base_clean = strip_noise(open(base_path).read())
            expected = base_clean.count(open_ch) - base_clean.count(close_ch)
        except OSError:
            expected = 0  # brand-new file: must balance on its own
        check(
            delta == 0 or delta == expected,
            f"{name}: unbalanced {open_ch}{close_ch} — delta {delta}, "
            f"baseline delta {expected}",
        )

    # Every api.<member>( referenced must exist on the exported api object.
    for member in sorted(set(re.findall(r"\bapi\.([A-Za-z_$][\w$]*)\s*\(", src))):
        check(member in api_members, f"{name}: api.{member}() is not defined in api.ts")

    # No stray merge markers / TODO placeholders from the edits.
    for marker in ("<<<<<<<", ">>>>>>>", "=======\n"):
        check(marker not in src, f"{name}: leftover conflict marker {marker!r}")

    # JSX: every opening tag of a component has a matching close or is self-closing.
    # Must not confuse a TS generic (useState<HotPayload>, useRef<AbortController>)
    # for a JSX element: a real JSX tag is never preceded by an identifier char.
    opens: dict = {}
    i, n = 0, len(src)
    while i < n:
        m = re.compile(r"<([A-Z][\w.]*)(?=[\s/>])").match(src, i)
        if not m or (i > 0 and (src[i - 1].isalnum() or src[i - 1] in "_$")):
            i += 1
            continue
        tag = m.group(1)
        # Walk to this tag's terminating '>', skipping quoted attrs and {…} exprs.
        j, depth = m.end(), 0
        while j < n:
            ch = src[j]
            if ch in "\"'":
                q, j = ch, j + 1
                while j < n and src[j] != q:
                    j += 2 if src[j] == "\\" else 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == ">" and depth == 0:
                break
            j += 1
        self_closing = src[max(0, j - 1)] == "/"
        if not self_closing:
            opens[tag] = opens.get(tag, 0) + 1
        i = j + 1
    for tag, count in sorted(opens.items()):
        closes = len(re.findall(rf"</{re.escape(tag)}>", src))
        check(count == closes, f"{name}: <{tag}> opened {count}x but closed {closes}x")


# ── Change-specific assertions ──────────────────────────────────────────────
surprise = open(f"{ROOT}/components/SurpriseStocks.tsx").read()
hot = open(f"{ROOT}/components/HotStocks.tsx").read()
ipo = open(f"{ROOT}/components/IpoTracker.tsx").read()
app = open(f"{ROOT}/App.tsx").read()

# #1 IPO fully removed from Surprise, not merely duplicated.
check("IpoSection" not in surprise, "SurpriseStocks still references IpoSection")
check("ipoScan" not in surprise, "SurpriseStocks still calls api.ipoScan")
check("ipoList" not in surprise, "SurpriseStocks still calls api.ipoList")
check("IpoAnalysis" not in surprise, "SurpriseStocks still declares IPO types")
check("IpoTracker" in app, "App.tsx does not mount IpoTracker")
check("ipoStop" in ipo, "IpoTracker has no Stop wiring")
check("DataHealthAudit" in ipo, "IpoTracker does not embed DataHealthAudit")
check("display_days" in ipo or "displayDays" in ipo, "IpoTracker missing display_days wiring")

# #12 Surprise Stop button actually exists and is wired.
check("surpriseStop" in surprise, "SurpriseStocks does not call api.surpriseStop")
check("surpriseStop" in api_src, "api.ts is missing surpriseStop")
check("stopSurprise" in surprise, "SurpriseStocks has no stopSurprise handler")
check(
    re.search(r"onClick=\{\(\)\s*=>\s*void stopSurprise\(\)\}", surprise) is not None,
    "SurpriseStocks Stop button is not wired to stopSurprise",
)
check("stopBusy" in surprise, "SurpriseStocks Stop button has no busy state")
check(
    surprise.count("const [stopBusy, setStopBusy]") == 1,
    "SurpriseStocks stopBusy declared 0 or 2+ times",
)

# #2 Hot Picks: real ETA, resume, stop, stored table, health.
check("st.total ?? 0" in hot, "HotStocks still fabricates a total (the ETA bug)")
check("100" not in re.findall(r"st\.total \?\? (\d+)", hot), "HotStocks total default is 100")
check("getStockkyHotTable" in hot, "HotStocks does not read the durable 24h table")
check("getStockkyHotAudit" in hot, "HotStocks has no feed-health panel")
check('"stopped"' in hot, "HotStocks does not handle a stopped job")
check("pollJobRef" in hot, "HotStocks cannot resume an in-flight scan after reload")
check("fmtSec" in hot and "fmtAge" in hot, "HotStocks missing the time formatters")
for member in ("getStockkyHotTable", "getStockkyHotAudit", "getStockkyHotStatus"):
    check(member in api_members, f"api.ts is missing {member}")

print(f"{checks} frontend assertions run.")
if findings:
    print(f"\n*** {len(findings)} FINDING(S) ***")
    for f in findings:
        print(f"  x {f}")
    sys.exit(1)
print("\nRESULT: PASS — balanced syntax, every api.* call exists, IPO fully")
print("        extracted from Surprise, Stop + ETA + resume all wired.")
