#!/usr/bin/env bash
# Source runtime telegram env if present (never commit token files).
set -euo pipefail

ENV_FILE="${QUANT_SENTINEL_ENV_FILE:-/run/quant-monitor/telegram.env}"
ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a
elif [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  source "$ROOT/.env"
  set +a
fi
