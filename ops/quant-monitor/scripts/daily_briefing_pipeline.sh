#!/usr/bin/env bash
# Generate daily reports and dispatch via AIAuditBridge (task 10).
# Runtime GCS digest stays off unless QUANT_MONITOR_RUNTIME_DIGEST_ENABLED=true.
# The daily timer is unchanged.
set -euo pipefail

ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
AAB="${AIAUDIT_BRIDGE_ROOT:-$HOME/Projects/AIAuditBridge}"
DAY=$(date -u +%Y-%m-%d)
OUT="$ROOT/data/daily-reports/$DAY"

# shellcheck source=scripts/source_telegram_env.sh
source "$ROOT/scripts/source_telegram_env.sh"

run_domain_consume() {
  local -a consume_args
  consume_args=(--report-dir "$OUT" --day "$DAY" --dispatch)
  if [[ "${QUANT_MONITOR_AI_SUMMARY:-false}" == "true" ]]; then
    consume_args+=(--ai-summary)
  fi
  PYTHONPATH=. python3 scripts/consume_daily_briefing.py "${consume_args[@]}"
}

if [[ "${QUANT_MONITOR_RUNTIME_DIGEST_ENABLED:-false}" != "true" ]]; then
  bash "$ROOT/scripts/daily_briefing.sh"
  if [[ ! -d "$AAB" ]]; then
    echo "[briefing-pipeline] skip dispatch: AIAuditBridge not found at $AAB" >&2
    exit 0
  fi
  cd "$AAB"
  run_domain_consume
  exit $?
fi

domain_status=0
bash "$ROOT/scripts/daily_briefing.sh" || domain_status=$?
if [[ ! -d "$AAB" ]]; then
  echo "[briefing-pipeline] skip dispatch: AIAuditBridge not found at $AAB" >&2
else
  cd "$AAB"
  set +e
  run_domain_consume
  consume_status=$?
  set -e
  if [[ "$consume_status" -ne 0 && "$domain_status" -eq 0 ]]; then
    domain_status=$consume_status
  fi
fi

runtime_status=0
prefix="${QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX:-}"
runtime_tz="${QUANT_MONITOR_RUNTIME_TIMEZONE:-}"
runtime_key="${QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY:-}"
trimmed="${prefix%/}"
runtime_scope="${runtime_key##*|}"
if [[ -z "$prefix" || -z "$runtime_tz" || -z "$runtime_key" \
  || "$prefix" == *'*'* || "$prefix" == *'?'* || "$prefix" == *'#'* \
  || "$prefix" == *'@'* || "$prefix" == *'..'* || "$prefix" != gs://* \
  || "${trimmed#gs://}" != */runtime_daily \
  || "$runtime_scope" != "paper" || "$runtime_key" == *" "* \
  || "$runtime_key" != *"|"*"|"* ]]; then
  echo "[briefing-pipeline] runtime digest rejected: runtime_projection_config_invalid" >&2
  runtime_status=2
else
  set +e
  runtime_day="$(python3 -c 'from datetime import datetime; from zoneinfo import ZoneInfo; import os, sys
try:
    sys.stdout.write(datetime.now(ZoneInfo(os.environ["QUANT_MONITOR_RUNTIME_TIMEZONE"])).date().isoformat())
except Exception:
    sys.exit(2)')"
  runtime_day_status=$?
  set -e
  if [[ "$runtime_day_status" -ne 0 || -z "$runtime_day" ]]; then
    echo "[briefing-pipeline] runtime digest rejected: invalid_runtime_timezone" >&2
    runtime_status=2
  elif [[ ! -d "$AAB" ]]; then
    echo "[briefing-pipeline] runtime digest rejected: runtime_consumer_unavailable" >&2
    runtime_status=2
  else
    set +e
    (
      cd "$AAB"
      PYTHONPATH=. python3 scripts/consume_daily_briefing.py \
        --runtime-projection-gcs "${trimmed}/longbridge/paper/${runtime_day}.json" \
        --day "$runtime_day" \
        --expected-target-key "$runtime_key" \
        --dispatch >/dev/null
    )
    runtime_status=$?
    set -e
  fi
fi

if [[ "$domain_status" -ne 0 || "$runtime_status" -ne 0 ]]; then
  echo "[briefing-pipeline] domain_exit=${domain_status} runtime_exit=${runtime_status}" >&2
  exit 1
fi
