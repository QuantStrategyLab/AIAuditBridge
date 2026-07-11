#!/usr/bin/env bash
set -euo pipefail

ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
source "$ROOT/scripts/common_env.sh"
HEALTH_DIR="$ROOT/data/health/dashboard"
HEALTH_FILE="$HEALTH_DIR/strategy_health_dashboard.json"
OUTPUT="$ROOT/data/health/strategy_health_dashboard.v1.json"
REVIEW_DIR="${QUANT_REVIEW_DIR:-$ROOT/data/strategy-reviews}"
mkdir -p "$HEALTH_DIR" "$(dirname "$OUTPUT")"

write_unavailable_snapshot() {
  python3 "$ROOT/scripts/build_dashboard_snapshot.py" \
    --health-file "$HEALTH_DIR/.collector-unavailable.json" \
    --review-dir "$REVIEW_DIR" \
    --output "$OUTPUT"
}

run_lifecycle_dashboard() {
  local help
  help="$(quant_lifecycle dashboard --help 2>&1 || true)"
  if grep -q -- "--output-dir" <<<"$help"; then
    local modern_dir="$HEALTH_DIR/.modern-dashboard-output"
    rm -rf "$modern_dir"
    mkdir -p "$modern_dir"
    if ! quant_lifecycle dashboard --output-dir "$modern_dir" --format json; then
      rm -rf "$modern_dir"
      return 1
    fi
    local modern_file="$modern_dir/strategy_health_dashboard.json"
    if [[ ! -f "$modern_file" ]]; then
      echo "[dashboard] lifecycle CLI did not produce strategy_health_dashboard.json" >&2
      rm -rf "$modern_dir"
      return 1
    fi
    cp "$modern_file" "$HEALTH_FILE"
    rm -rf "$modern_dir"
    return
  fi

  # Older QPK CLI versions write to ./dashboard_output instead of accepting
  # --output-dir. Run there and copy only the expected JSON artifact.
  local legacy_dir="$HEALTH_DIR/.legacy-dashboard-output"
  rm -rf "$legacy_dir"
  mkdir -p "$legacy_dir"
  (cd "$legacy_dir" && quant_lifecycle dashboard --format json)
  local legacy_file="$legacy_dir/dashboard_output/strategy_health_dashboard.json"
  if [[ ! -f "$legacy_file" ]]; then
    echo "[dashboard] lifecycle CLI did not produce strategy_health_dashboard.json" >&2
    return 1
  fi
  cp "$legacy_file" "$HEALTH_DIR/strategy_health_dashboard.json"
  rm -rf "$legacy_dir"
}

if ! run_lifecycle_dashboard; then
  write_unavailable_snapshot
  echo "[dashboard] lifecycle dashboard failed; wrote an unavailable payload" >&2
  exit 1
fi

python3 "$ROOT/scripts/build_dashboard_snapshot.py" \
  --health-file "$HEALTH_FILE" \
  --review-dir "$REVIEW_DIR" \
  --output "$OUTPUT"
