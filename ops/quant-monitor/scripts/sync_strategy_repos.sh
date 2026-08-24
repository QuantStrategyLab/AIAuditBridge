#!/usr/bin/env bash
set -euo pipefail
REPOS=(
  QuantPlatformKit
  CnEquityStrategies
  HkEquityStrategies
  UsEquityStrategies
  CryptoStrategies
)
ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
MIRROR_ROOT="${QUANT_PROJECTS_ROOT:-$ROOT/data/lifecycle-projects}"
REPOSITORY_BASE_URL="${QUANT_MONITOR_REPOSITORY_BASE_URL:-https://github.com/QuantStrategyLab}"
failed=0

mkdir -p "$MIRROR_ROOT"

for repo in "${REPOS[@]}"; do
  dir="$MIRROR_ROOT/$repo"
  if [[ -d "$dir/.git" ]]; then
    if ! git -C "$dir" fetch origin main --quiet; then
      echo "[sync] $repo fetch failed" >&2
      failed=1
      continue
    fi
    if ! git -C "$dir" checkout --detach --quiet origin/main; then
      echo "[sync] $repo checkout failed" >&2
      failed=1
      continue
    fi
    echo "[sync] $repo ok"
  else
    if ! git clone --depth 1 "$REPOSITORY_BASE_URL/$repo.git" "$dir" --quiet; then
      echo "[sync] $repo clone failed" >&2
      failed=1
      continue
    fi
    echo "[sync] $repo cloned"
  fi
done
exit "$failed"
