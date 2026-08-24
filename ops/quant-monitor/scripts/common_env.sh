#!/usr/bin/env bash
# Shared PYTHONPATH / CLI helpers for VPS quant-monitor scripts.
set -euo pipefail

ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
AAB_ROOT="${AIAUDIT_BRIDGE_ROOT:-$(cd "$ROOT/../.." && pwd)}"
PROJECTS_ROOT="${PROJECTS_ROOT:-$HOME/Projects}"
QUANT_PROJECTS_ROOT="${QUANT_PROJECTS_ROOT:-$ROOT/data/lifecycle-projects}"
LIFECYCLE_LOCAL_ROOT="${LIFECYCLE_LOCAL_ROOT:-$ROOT/data/lifecycle-store}"
# Monitoring must never import an editable QuantPlatformKit checkout from a
# developer workspace: the monitor synchronizes and consumes this dedicated
# mirror instead.
QPK_ROOT="${QUANT_PLATFORM_KIT_ROOT:-$QUANT_PROJECTS_ROOT/QuantPlatformKit}"
VENV="${QUANT_MONITOR_VENV:-$ROOT/.venv}"

export QUANT_MONITOR_ROOT="$ROOT"
export AIAUDIT_BRIDGE_ROOT="$AAB_ROOT"
export QUANT_PLATFORM_KIT_ROOT="$QPK_ROOT"
export PROJECTS_ROOT
export QUANT_PROJECTS_ROOT
export LIFECYCLE_LOCAL_ROOT

if [[ -x "$VENV/bin/python" ]]; then
  export PATH="$VENV/bin:$PATH"
fi

if [[ -d "$QPK_ROOT/src" ]]; then
  export PYTHONPATH="${QPK_ROOT}/src:${AAB_ROOT}${PYTHONPATH:+:$PYTHONPATH}"
fi

quant_lifecycle() {
  if command -v quant-lifecycle >/dev/null 2>&1; then
    quant-lifecycle "$@"
  else
    python3 -m quant_platform_kit.strategy_lifecycle.cli "$@"
  fi
}
