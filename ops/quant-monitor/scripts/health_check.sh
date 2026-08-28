#!/usr/bin/env bash
set -euo pipefail
ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=scripts/common_env.sh
source "$ROOT/scripts/common_env.sh"

bash "$ROOT/scripts/sync_strategy_repos.sh"
python3 "$ROOT/scripts/sync_lifecycle_artifacts.py"
set +e
python3 "$ROOT/scripts/health_cycle.py"
health_cycle_status=$?
set -e

# A monitor alert exits with status 2 after it has written a normalized health
# snapshot.  Publish that snapshot before preserving the alert status, so the
# unified console and Telegram/Issue paths observe the same fact.
if [[ "$health_cycle_status" -ne 0 && "$health_cycle_status" -ne 2 ]]; then
  exit "$health_cycle_status"
fi

bash "$ROOT/scripts/publish_strategy_health.sh"
exit "$health_cycle_status"
