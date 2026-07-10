#!/usr/bin/env bash
set -euo pipefail
REPOS=(
  QuantPlatformKit
  CnEquityStrategies
  HkEquityStrategies
  UsEquityStrategies
  CryptoStrategies
)
ROOT="${PROJECTS_ROOT:-$HOME/Projects}"
failed=0
for repo in "${REPOS[@]}"; do
  dir="$ROOT/$repo"
  if [[ -d "$dir/.git" ]]; then
    if ! git -C "$dir" fetch origin main --quiet; then
      echo "[sync] $repo fetch failed" >&2
      failed=1
      continue
    fi
    if ! git -C "$dir" checkout main --quiet; then
      echo "[sync] $repo checkout failed" >&2
      failed=1
      continue
    fi
    if ! git -C "$dir" pull --ff-only origin main --quiet; then
      echo "[sync] $repo pull failed" >&2
      failed=1
      continue
    fi
    echo "[sync] $repo ok"
  else
    echo "[sync] skip missing $dir" >&2
    failed=1
  fi
done
exit "$failed"
