#!/usr/bin/env bash
set -euo pipefail
ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=scripts/common_env.sh
source "$ROOT/scripts/common_env.sh"
# shellcheck source=scripts/source_telegram_env.sh
source "$ROOT/scripts/source_telegram_env.sh" 2>/dev/null || true

export DAY
DAY=$(date -u +%Y-%m-%d)
python3 "$ROOT/scripts/daily_briefing_builder.py"
