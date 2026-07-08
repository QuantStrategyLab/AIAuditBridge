#!/usr/bin/env bash
set -euo pipefail
ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=scripts/common_env.sh
source "$ROOT/scripts/common_env.sh"

bash "$ROOT/scripts/sync_strategy_repos.sh"
python3 "$ROOT/scripts/health_cycle.py"
