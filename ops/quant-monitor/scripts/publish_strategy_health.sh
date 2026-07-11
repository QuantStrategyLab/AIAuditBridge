#!/usr/bin/env bash
set -euo pipefail

ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
INPUT="${STRATEGY_HEALTH_INPUT:-$ROOT/data/health/strategy_health_dashboard.v1.json}"

if [[ "${STRATEGY_HEALTH_PUBLISH:-0}" != "1" ]]; then
  echo "[publish] disabled; no outbound sync" >&2
  exit 0
fi

: "${STRATEGY_HEALTH_SYNC_URL:?STRATEGY_HEALTH_SYNC_URL is required when publishing is enabled}"
: "${STRATEGY_HEALTH_SYNC_TOKEN:?STRATEGY_HEALTH_SYNC_TOKEN is required when publishing is enabled}"

if [[ ! -f "$INPUT" ]]; then
  echo "[publish] dashboard snapshot is unavailable" >&2
  exit 1
fi

python3 - "$INPUT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("schema_version") != "strategy_health_dashboard.v1":
    raise SystemExit("[publish] unsupported dashboard schema")
PY

curl --fail --silent --show-error --max-time 20 --config - >/dev/null <<CURL_CONFIG
url = "${STRATEGY_HEALTH_SYNC_URL}"
request = POST
header = Authorization: Bearer ${STRATEGY_HEALTH_SYNC_TOKEN}
header = Content-Type: application/json
data-binary = @${INPUT}
CURL_CONFIG

echo "[publish] strategy health snapshot sent"
