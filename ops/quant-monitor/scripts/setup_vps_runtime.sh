#!/usr/bin/env bash
# Bootstrap VPS runtime: venv + QuantPlatformKit editable install.
set -euo pipefail

ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=scripts/common_env.sh
source "$ROOT/scripts/common_env.sh"

VENV="$ROOT/.venv"
QPK_ROOT="${QUANT_PLATFORM_KIT_ROOT:?QuantPlatformKit not found}"

if [[ ! -d "$QPK_ROOT/.git" ]]; then
  echo "[setup] cloning QuantPlatformKit into $QPK_ROOT" >&2
  mkdir -p "$(dirname "$QPK_ROOT")"
  git clone --depth 1 https://github.com/QuantStrategyLab/QuantPlatformKit.git "$QPK_ROOT"
fi

git -C "$QPK_ROOT" fetch origin main --quiet || true
git -C "$QPK_ROOT" checkout main --quiet || true
git -C "$QPK_ROOT" pull --ff-only origin main --quiet || true

if [[ ! -d "$AAB_ROOT/.git" ]]; then
  echo "[setup] AIAuditBridge missing at $AAB_ROOT" >&2
  exit 1
fi
git -C "$AAB_ROOT" fetch origin main --quiet || true
git -C "$AAB_ROOT" checkout main --quiet || true
git -C "$AAB_ROOT" pull --ff-only origin main --quiet || true

python3 -m venv "$VENV"
"$VENV/bin/pip" install -U pip wheel
"$VENV/bin/pip" install -e "$QPK_ROOT"
# Ensure lifecycle runtime deps (editable install may skip heavy wheels on minimal VPS).
"$VENV/bin/pip" install numpy pandas google-cloud-storage

if ! command -v gh >/dev/null 2>&1; then
  echo "[setup] warning: gh CLI not installed; drift issues will be skipped" >&2
fi
if ! command -v gcloud >/dev/null 2>&1; then
  echo "[setup] warning: gcloud not installed; telegram env load may fail" >&2
fi

echo "[setup] ok venv=$VENV qpk=$QPK_ROOT"
