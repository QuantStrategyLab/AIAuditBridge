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
for repo in "${REPOS[@]}"; do
  dir="$ROOT/$repo"
  if [[ -d "$dir/.git" ]]; then
    git -C "$dir" fetch origin main --quiet || true
    git -C "$dir" checkout main --quiet || true
    git -C "$dir" pull --ff-only origin main --quiet || true
    echo "[sync] $repo ok"
  else
    echo "[sync] skip missing $dir" >&2
  fi
done
