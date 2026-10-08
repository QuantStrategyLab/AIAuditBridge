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
  builder_status=0
  bash "$ROOT/scripts/daily_briefing.sh" || builder_status=$?
  if [[ "$builder_status" -ne 0 ]]; then
    echo "[briefing-pipeline-result:v1] branch=domain builder_exit=${builder_status} consumer_exit=not_run domain_exit=${builder_status}" >&2
    exit "$builder_status"
  fi
  if [[ ! -d "$AAB" ]]; then
    echo "[briefing-pipeline] skip dispatch: AIAuditBridge not found at $AAB" >&2
    echo "[briefing-pipeline-result:v1] branch=domain builder_exit=0 consumer_exit=not_run domain_exit=0" >&2
    exit 0
  fi
  cd "$AAB"
  set +e
  run_domain_consume
  consume_status=$?
  set -e
  echo "[briefing-pipeline-result:v1] branch=domain builder_exit=0 consumer_exit=${consume_status} domain_exit=${consume_status}" >&2
  exit "$consume_status"
fi

builder_status=0
consume_status=not_run
bash "$ROOT/scripts/daily_briefing.sh" || builder_status=$?
domain_status=$builder_status
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
echo "[briefing-pipeline-result:v1] branch=domain builder_exit=${builder_status} consumer_exit=${consume_status} domain_exit=${domain_status}" >&2

runtime_status=0
prefix="${QUANT_MONITOR_RUNTIME_PROJECTION_PREFIX:-}"
runtime_tz="${QUANT_MONITOR_RUNTIME_TIMEZONE:-}"
runtime_key="${QUANT_MONITOR_RUNTIME_EXPECTED_TARGET_KEY:-}"
runtime_object_override="${QUANT_MONITOR_RUNTIME_PROJECTION_OBJECT:-}"
runtime_day_override="${QUANT_MONITOR_RUNTIME_BUSINESS_DAY:-}"
trimmed="${prefix%/}"
runtime_scope="${runtime_key##*|}"
if [[ -z "$prefix" || -z "$runtime_tz" || -z "$runtime_key" \
  || "$prefix" == *'*'* || "$prefix" == *'?'* || "$prefix" == *'#'* \
  || "$prefix" == *'@'* || "$prefix" == *'..'* || "$prefix" != gs://* \
  || "${trimmed#gs://}" != */runtime_daily \
  || "$runtime_scope" != "paper" || "$runtime_key" == *" "* \
  || "$runtime_key" != *"|"*"|"* \
  || ( -n "$runtime_object_override" && -z "$runtime_day_override" ) \
  || ( -z "$runtime_object_override" && -n "$runtime_day_override" ) \
  || ( -n "$runtime_object_override" && "$runtime_object_override" != "${trimmed}/longbridge/paper/${runtime_day_override}/"* ) ]]; then
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
    if [[ -n "$runtime_object_override" ]]; then
      runtime_day="$runtime_day_override"
      runtime_source=(--runtime-projection-gcs "$runtime_object_override")
    else
      runtime_source=(--runtime-handoff-prefix "$trimmed")
    fi
    set +e
    (
      cd "$AAB"
      PYTHONPATH=. python3 scripts/consume_daily_briefing.py \
        "${runtime_source[@]}" \
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
