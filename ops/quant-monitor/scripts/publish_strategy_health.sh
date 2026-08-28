#!/usr/bin/env bash
set -euo pipefail

ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
INPUT="${STRATEGY_HEALTH_INPUT:-$ROOT/data/health/strategy_health_dashboard.v1.json}"

if [[ "${STRATEGY_HEALTH_PUBLISH:-0}" != "1" ]]; then
  echo "[publish] disabled; no outbound sync" >&2
  exit 0
fi

: "${STRATEGY_HEALTH_SYNC_URL:?STRATEGY_HEALTH_SYNC_URL is required when publishing is enabled}"

sync_token="${STRATEGY_HEALTH_SYNC_TOKEN:-}"
if [[ -z "$sync_token" && -n "${STRATEGY_HEALTH_SYNC_TOKEN_FILE:-}" ]]; then
  if [[ ! -f "${STRATEGY_HEALTH_SYNC_TOKEN_FILE}" || ! -r "${STRATEGY_HEALTH_SYNC_TOKEN_FILE}" ]]; then
    echo "[publish] STRATEGY_HEALTH_SYNC_TOKEN_FILE is not a readable regular file" >&2
    exit 1
  fi
  sync_token="$(<"${STRATEGY_HEALTH_SYNC_TOKEN_FILE}")"
fi
if [[ -z "$sync_token" ]]; then
  echo "[publish] STRATEGY_HEALTH_SYNC_TOKEN or STRATEGY_HEALTH_SYNC_TOKEN_FILE is required when publishing is enabled" >&2
  exit 1
fi

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

STRATEGY_HEALTH_INPUT="$INPUT" STRATEGY_HEALTH_SYNC_TOKEN="$sync_token" python3 - <<'PY' | curl --fail --silent --show-error --max-time 20 --config - >/dev/null
import json
import os


def write_option(name, value):
    print(f"{name} = {json.dumps(value)}")


write_option("url", os.environ["STRATEGY_HEALTH_SYNC_URL"])
write_option("request", "POST")
write_option("header", f"Authorization: Bearer {os.environ['STRATEGY_HEALTH_SYNC_TOKEN']}")
write_option("header", "Content-Type: application/json")
write_option("data-binary", "@" + os.environ["STRATEGY_HEALTH_INPUT"])
PY

echo "[publish] strategy health snapshot sent"
