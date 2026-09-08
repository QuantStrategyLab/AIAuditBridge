#!/usr/bin/env bash
# Generate daily reports and dispatch via AIAuditBridge (task 10).
set -euo pipefail

ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
AAB="${AIAUDIT_BRIDGE_ROOT:-$HOME/Projects/AIAuditBridge}"
DAY=$(date -u +%Y-%m-%d)
OUT="$ROOT/data/daily-reports/$DAY"

# shellcheck source=scripts/source_telegram_env.sh
source "$ROOT/scripts/source_telegram_env.sh"

bash "$ROOT/scripts/daily_briefing.sh"

if [[ ! -d "$AAB" ]]; then
  echo "[briefing-pipeline] skip dispatch: AIAuditBridge not found at $AAB" >&2
  exit 0
fi

cd "$AAB"
consume_args=(--report-dir "$OUT" --day "$DAY" --dispatch)
if [[ "${QUANT_MONITOR_AI_SUMMARY:-false}" == "true" ]]; then
  consume_args+=(--ai-summary)
fi
PYTHONPATH=. python3 scripts/consume_daily_briefing.py "${consume_args[@]}"
