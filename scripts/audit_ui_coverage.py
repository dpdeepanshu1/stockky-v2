"""E148: UI v7 coverage matrix (READ-ONLY, stdlib). For each of the 13 tabs plus the system-check, service-manager and login / admin-gate
screens it prints: which header the tab draws (the shared v6/v7 hero, the tab's own hero, the dashboard hero or a plain header), how many
v6 / v7 classes and how many v3-v5 ("legacy") card classes its own files use, and which shared `ui/` pieces they import.

  python3 scripts/audit_ui_coverage.py [--json]

The numbers are a map for the restyle, not a verdict: a tab that draws the shared hero still shows v3-v5 cards inside it until those are
moved to the v6/v7 pieces (KpiStrip, RiskRail, ExposureStrip, ...). Exit code is always 0. "files" are the tab's own component files only
(shared helpers such as api.ts are not counted).
"""
import json
import re
import sys
from pathlib import Path

R = Path(__file__).resolve().parent.parent
SRC = R / "frontend" / "src"
C = SRC / "components"

# tab id -> (files or folders under frontend/src, where the tab's header is drawn)
TABS = {
    "dashboard": (["components/app/DashboardView.tsx", "components/MarketMovers.tsx", "components/MarketSentimentHeader.tsx"], "dash"),
    "notifications": (["components/NotificationsPanel.tsx"], "Alerts"),
    "training": (["components/Training.tsx", "components/TrainingProgressPanel.tsx"], "Training Lab"),
    "trades": (["components/Trades.tsx"], "Paper Trades"),
    "hot": (["components/HotStocks.tsx"], "Hot Picks"),
    "surprise": (["components/SurpriseStocks.tsx"], "Surprise Momentum"),
    "ipo": (["components/IpoTracker.tsx", "components/IpoLifecycle.tsx", "components/IpoFeedHealth.tsx"], "IPO Tracker"),
    "datafeed": (["components/DataFeed.tsx", "components/DataHealthAudit.tsx"], "Data Feed"),
    "settings": (["components/app/SettingsPage.tsx"], "Settings"),
    "watchlist": (["components/WatchlistManager.tsx"], "Watchlist"),
    "realtrade": (["components/RealAutoTrade.tsx", "components/realAuto"], "own"),
    "positionstocks": (["components/PositionStocksTab.tsx", "components/board"], "own"),
    "calibration": (["components/CalibrationControlTab.tsx", "components/calibration"], "own"),
    "system-check": (["components/SystemCheck.tsx"], "n/a"),
    "service-manager": (["components/ServiceManager.tsx"], "n/a"),
    "admin-gate / login": (["components/AdminGate.tsx"], "n/a"),
}
V67 = re.compile(r"\bv[67]-[a-z][a-z0-9-]*")
LEGACY = re.compile(r"terminal-panel|bg-graphite|bg-carbon|bg-obsidian")
UI_IMPORT_STRICT = re.compile(r'from\s+"(?:\.\./)*(?:\./)?ui/([A-Za-z]+)"')


def _files(entries):
    out = []
    for e in entries:
        p = SRC / e
        if p.is_dir():
            out += [f for f in sorted(p.rglob("*")) if f.suffix in (".ts", ".tsx") and ".test." not in f.name]
        elif p.is_file():
            out.append(p)
    return out


def _header(tab, how, tabcontent, files):
    text = "\n".join(f.read_text(encoding="utf-8", errors="ignore") for f in files)
    if how == "dash":
        return "dash-hero" if "dash-hero" in text else "plain"
    if how == "own":
        return "hero (own)" if re.search(r"\bPageHero\b", text) else "plain"
    if how == "n/a":
        return "n/a"
    # a page header passed icon + kicker is drawn as the shared hero
    sources = tabcontent + "\n" + text
    m = re.search(r'<PageHeader title="%s"[^>]*/>' % re.escape(how), sources)
    if not m:
        return "none"
    return "hero (PageHeader)" if (" icon=" in m.group(0) and " kicker=" in m.group(0)) else "plain header"


def build():
    tabcontent = (C / "app" / "TabContent.tsx").read_text(encoding="utf-8", errors="ignore")
    rows = []
    for tab, (entries, how) in TABS.items():
        files = _files(entries)
        text = "\n".join(f.read_text(encoding="utf-8", errors="ignore") for f in files)
        ui = sorted(set(UI_IMPORT_STRICT.findall(text)) | ({"PageHero"} if re.search(r'from\s+"[^"]*PageHero"', text) else set()))
        rows.append({
            "tab": tab, "header": _header(tab, how, tabcontent, files), "files": len(files),
            "v67_classes": len(V67.findall(text)), "legacy_classes": len(LEGACY.findall(text)), "ui_pieces": ui,
        })
    return rows


def main():
    rows = build()
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=1))
        return
    print("%-20s %-18s %5s %6s %6s  %s" % ("tab", "header", "files", "v6/v7", "legacy", "ui pieces used in the tab's own files"))
    for r in rows:
        print("%-20s %-18s %5d %6d %6d  %s" % (r["tab"], r["header"], r["files"], r["v67_classes"], r["legacy_classes"], ", ".join(r["ui_pieces"]) or "-"))
    thirteen = [r for r in rows if r["tab"] not in ("system-check", "service-manager", "admin-gate / login")]
    no_hero = [r["tab"] for r in thirteen if not r["header"].startswith("hero")]
    print("\ntabs without the shared hero: %s" % (", ".join(no_hero) or "none"))
    legacy_heavy = sorted((r for r in thirteen if r["legacy_classes"] > r["v67_classes"]), key=lambda r: -r["legacy_classes"])
    print("tabs whose cards are still mostly v3-v5 (legacy classes > v6/v7 classes): %s" % ", ".join("%s (%d vs %d)" % (r["tab"], r["legacy_classes"], r["v67_classes"]) for r in legacy_heavy))


if __name__ == "__main__":
    main()
