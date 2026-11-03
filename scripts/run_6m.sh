#!/usr/bin/env bash
set -euo pipefail
REPO="${1:-$HOME/stockky-v2}"; HIST="${2:-$HOME/hist2y}"
python3 "$REPO/scripts/fetch_history_for_backtest.py" "$HIST" --period 2y --sample 400 --seed 7 --min-bars 300 --universe-url http://localhost:8000/api/universe --manifest "$HOME/sample.json"
python3 "$(dirname "$0")/six_month_backtest.py" "$REPO" "$HIST" --months 6 --which both
echo "Reports: $HIST/legacy_6m_out and $HIST/entry_6m_out"
