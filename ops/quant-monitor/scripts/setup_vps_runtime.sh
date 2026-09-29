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

LOCK_RELPATH="ops/quant-monitor/requirements-linux-py312.lock"
LOCK_FILE="$ROOT/requirements-linux-py312.lock"
if [[ ! -f "$LOCK_FILE" ]]; then
  echo "[setup] missing requirements-linux-py312.lock" >&2
  exit 1
fi
# ROOT override must not silently consume a different unknown lock file.
case "$(cd "$(dirname "$LOCK_FILE")" && pwd -P)/$(basename "$LOCK_FILE")" in
  "$(cd "$AAB_ROOT/ops/quant-monitor" && pwd -P)/requirements-linux-py312.lock") ;;
  *)
    echo "[setup] requirements-linux-py312.lock must be the reviewed file under AIAuditBridge/ops/quant-monitor" >&2
    exit 1
    ;;
esac

DIRTY_PATHSPEC=(
  client
  scripts
  service
  ops/quant-monitor/scripts
  ops/quant-monitor/systemd
  ops/quant-monitor/qpk-runtime.sha
  ops/quant-monitor/requirements-linux-py312.lock
)
if [[ -n "$(git -C "$AAB_ROOT" status --porcelain --untracked-files=all -- "${DIRTY_PATHSPEC[@]}")" ]]; then
  echo "[setup] refusing dirty AIAuditBridge runtime source" >&2
  exit 1
fi
ACTUAL_AAB_SHA="$(git -C "$AAB_ROOT" rev-parse HEAD)"
if [[ "$ACTUAL_AAB_SHA" != "$SOURCE_SHA" ]]; then
  echo "[setup] AIAuditBridge checkout does not match reviewed SHA" >&2
  exit 1
fi
if ! EXPECTED_LOCK="$(git -C "$AAB_ROOT" show "${SOURCE_SHA}:${LOCK_RELPATH}")"; then
  echo "[setup] requirements-linux-py312.lock missing from reviewed SOURCE_SHA" >&2
  exit 1
fi
ACTUAL_LOCK="$(<"$LOCK_FILE")"
if [[ "$ACTUAL_LOCK" != "$EXPECTED_LOCK" ]]; then
  echo "[setup] requirements-linux-py312.lock does not match reviewed SOURCE_SHA" >&2
  exit 1
fi

# Refuse unsupported hosts before any mirror/venv mutation.
python3 - <<'PY'
import os
import platform
import re
import sys


def fail(message: str) -> None:
    print(f"[setup] {message}", file=sys.stderr)
    print(
        "[setup] requirements-linux-py312.lock only covers CPython 3.12 on "
        "Linux x86_64 with glibc >= 2.34; refusing unsupported environment "
        "(no silent fallback)",
        file=sys.stderr,
    )
    raise SystemExit(1)


if sys.version_info[:2] != (3, 12):
    fail(
        f"unsupported Python {sys.version_info.major}.{sys.version_info.minor}; "
        "need 3.12"
    )
if platform.system() != "Linux":
    fail(f"unsupported OS {platform.system()!r}; need Linux")
machine = platform.machine().lower()
if machine not in {"x86_64", "amd64"}:
    fail(f"unsupported machine {platform.machine()!r}; need x86_64")

try:
    glibc = os.confstr("CS_GNU_LIBC_VERSION")
except (AttributeError, OSError, ValueError):
    glibc = ""
match = re.search(r"(\d+)\.(\d+)", glibc or "")
if match is None:
    fail(f"unable to determine glibc version from {glibc!r}")
major = int(match.group(1))
minor = int(match.group(2))
if (major, minor) < (2, 34):
    fail(f"glibc {major}.{minor} is below required 2.34")
PY

if [[ ! -f "$ROOT/qpk-runtime.sha" ]]; then
  echo "[setup] missing qpk-runtime.sha" >&2
  exit 1
fi
QPK_RUNTIME_SHA="$(<"$ROOT/qpk-runtime.sha")"
QPK_RUNTIME_SHA="${QPK_RUNTIME_SHA%$'\n'}"
if [[ ! "$QPK_RUNTIME_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[setup] qpk-runtime.sha must contain a reviewed 40-character SHA" >&2
  exit 1
fi

VENV="$ROOT/.venv"
QPK_ROOT="${QUANT_PLATFORM_KIT_ROOT:?QuantPlatformKit not found}"

qpk_created=0
if [[ ! -d "$QPK_ROOT/.git" ]]; then
  echo "[setup] cloning QuantPlatformKit into $QPK_ROOT" >&2
  mkdir -p "$(dirname "$QPK_ROOT")"
  git clone --no-checkout https://github.com/QuantStrategyLab/QuantPlatformKit.git "$QPK_ROOT"
  qpk_created=1
fi

git -C "$QPK_ROOT" fetch origin "$QPK_RUNTIME_SHA" --quiet
# Fresh --no-checkout clones report every tracked path as deleted until the first
# checkout; only existing mirrors must refuse unknown/staged edits first.
if [[ "$qpk_created" -eq 0 ]]; then
  if [[ -n "$(git -C "$QPK_ROOT" status --porcelain --untracked-files=no)" ]]; then
    echo "[setup] QPK tracked changes require mirror synchronization/review" >&2
    exit 1
  fi
fi
git -C "$QPK_ROOT" checkout --detach --quiet "$QPK_RUNTIME_SHA"
ACTUAL_QPK_SHA="$(git -C "$QPK_ROOT" rev-parse HEAD)"
if [[ "$ACTUAL_QPK_SHA" != "$QPK_RUNTIME_SHA" ]]; then
  echo "[setup] QuantPlatformKit checkout does not match pinned SHA" >&2
  exit 1
fi

python3 -m venv "$VENV"
# Locked third-party tree only; no unbounded pip/wheel upgrade and no free-floating
# numpy/pandas/google-cloud-storage installs.
if ! "$VENV/bin/python" -m pip install --require-hashes --only-binary=:all: -r "$LOCK_FILE"; then
  echo "[setup] locked dependency install failed" >&2
  exit 1
fi
# setuptools writes egg-info even for local non-editable installs. Build from a
# temporary archive; common_env still imports the synchronized mirror via PYTHONPATH.
# Reuse locked setuptools/wheel; do not download build dependencies.
build_root=$(mktemp -d "${TMPDIR:-/tmp}/quant-monitor-qpk.XXXXXX")
trap 'rm -rf -- "$build_root"' EXIT
git -C "$QPK_ROOT" archive HEAD | tar -x -C "$build_root"
if ! "$VENV/bin/python" -m pip install --no-deps --no-build-isolation "$build_root"; then
  echo "[setup] QuantPlatformKit install failed" >&2
  exit 1
fi
if ! "$VENV/bin/python" -m pip check; then
  echo "[setup] pip check failed after locked install" >&2
  exit 1
fi

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

echo "[setup] ok venv=$VENV qpk=$QPK_ROOT sha=$QPK_RUNTIME_SHA"
