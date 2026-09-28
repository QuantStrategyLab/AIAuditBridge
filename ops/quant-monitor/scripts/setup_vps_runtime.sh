#!/usr/bin/env bash
# Bootstrap VPS runtime without writing build metadata into source mirrors.
set -euo pipefail

ROOT="${QUANT_MONITOR_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
# shellcheck source=scripts/common_env.sh
source "$ROOT/scripts/common_env.sh"

SOURCE_SHA="${1:-}"
EXPECTED_AAB_ROOT="${2:-}"
if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ || -z "$EXPECTED_AAB_ROOT" ]]; then
  echo "[setup] reviewed AIAuditBridge SHA and source root are required" >&2
  exit 2
fi
if [[ "$AAB_ROOT" != "$EXPECTED_AAB_ROOT" ]]; then
  echo "[setup] AIAuditBridge source root does not match deployment checkout" >&2
  exit 1
fi
if [[ ! -d "$AAB_ROOT/.git" ]]; then
  echo "[setup] AIAuditBridge missing at $AAB_ROOT" >&2
  exit 1
fi
if [[ -n "$(git -C "$AAB_ROOT" status --porcelain --untracked-files=all -- client scripts service ops/quant-monitor/scripts ops/quant-monitor/systemd)" ]]; then
  echo "[setup] refusing dirty AIAuditBridge runtime source" >&2
  exit 1
fi
ACTUAL_AAB_SHA="$(git -C "$AAB_ROOT" rev-parse HEAD)"
if [[ "$ACTUAL_AAB_SHA" != "$SOURCE_SHA" ]]; then
  echo "[setup] AIAuditBridge checkout does not match reviewed SHA" >&2
  exit 1
fi

VENV="$ROOT/.venv"
QPK_ROOT="${QUANT_PLATFORM_KIT_ROOT:?QuantPlatformKit not found}"

if [[ ! -d "$QPK_ROOT/.git" ]]; then
  echo "[setup] cloning QuantPlatformKit into $QPK_ROOT" >&2
  mkdir -p "$(dirname "$QPK_ROOT")"
  git clone --depth 1 https://github.com/QuantStrategyLab/QuantPlatformKit.git "$QPK_ROOT"
fi

git -C "$QPK_ROOT" fetch origin main --quiet
if [[ -n "$(git -C "$QPK_ROOT" status --porcelain --untracked-files=no)" ]]; then
  echo "[setup] QPK tracked changes require mirror synchronization/review" >&2
  exit 1
fi
git -C "$QPK_ROOT" checkout --detach --quiet origin/main

python3 -m venv "$VENV"
"$VENV/bin/pip" install -U pip wheel
# setuptools writes egg-info even for local non-editable installs. Build from a
# temporary archive; common_env still imports the synchronized mirror via PYTHONPATH.
build_root=$(mktemp -d "${TMPDIR:-/tmp}/quant-monitor-qpk.XXXXXX")
trap 'rm -rf -- "$build_root"' EXIT
git -C "$QPK_ROOT" archive HEAD | tar -x -C "$build_root"
"$VENV/bin/pip" install "$build_root"
# Ensure lifecycle runtime deps on minimal VPS.
"$VENV/bin/pip" install numpy pandas google-cloud-storage

if ! command -v gh >/dev/null 2>&1; then
  echo "[setup] gh CLI is required for trusted lifecycle artifact synchronization" >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "[setup] gh CLI authentication is required for lifecycle artifacts" >&2
  exit 1
fi
if ! command -v gcloud >/dev/null 2>&1; then
  echo "[setup] warning: gcloud not installed; telegram env load may fail" >&2
fi

echo "[setup] ok venv=$VENV qpk=$QPK_ROOT"
