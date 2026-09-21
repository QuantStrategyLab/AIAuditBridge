#!/usr/bin/env bash
# Install-only immutable quant-monitor release from an exact local commit SHA.
# Does not fetch/pull, switch services, reload systemd, notify, or trade.
set -euo pipefail

usage() {
  cat <<'EOF' >&2
usage: install_immutable_release.sh --sha <40-hex> --repo <git-root> \
  [--release-root <dir>] [--runtime-data <dir>] [--runtime-venv <dir>]

Environment alternatives:
  QUANT_MONITOR_RELEASE_SHA
  QUANT_MONITOR_REPO_ROOT or AIAUDIT_BRIDGE_ROOT
  QUANT_MONITOR_RELEASE_ROOT (default /opt/quant-monitor/releases)
  QUANT_MONITOR_RUNTIME_DATA
  QUANT_MONITOR_RUNTIME_VENV

Install only. Switch, rollback, and deploy_to_vps.sh are separate and not performed.
EOF
}

SHA="${QUANT_MONITOR_RELEASE_SHA:-}"
REPO_ROOT="${QUANT_MONITOR_REPO_ROOT:-${AIAUDIT_BRIDGE_ROOT:-}}"
RELEASE_ROOT="${QUANT_MONITOR_RELEASE_ROOT:-/opt/quant-monitor/releases}"
RUNTIME_DATA="${QUANT_MONITOR_RUNTIME_DATA:-}"
RUNTIME_VENV="${QUANT_MONITOR_RUNTIME_VENV:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha)
      SHA="${2:-}"
      shift 2
      ;;
    --repo)
      REPO_ROOT="${2:-}"
      shift 2
      ;;
    --release-root)
      RELEASE_ROOT="${2:-}"
      shift 2
      ;;
    --runtime-data)
      RUNTIME_DATA="${2:-}"
      shift 2
      ;;
    --runtime-venv)
      RUNTIME_VENV="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

die() {
  echo "$*" >&2
  exit 1
}

require_abs_path() {
  local label="$1" path="$2"
  if [[ -z "$path" || "$path" == "/" ]]; then
    die "missing ${label}"
  fi
  if [[ ! "$path" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
    die "invalid ${label} path"
  fi
  case "$path" in
    */..|*/../*|../*|..)
      die "invalid ${label} path"
      ;;
  esac
  if [[ -L "$path" ]]; then
    die "${label} must not be a symlink: ${path}"
  fi
}

resolve_existing_dir() {
  local label="$1" path="$2"
  require_abs_path "$label" "$path"
  if [[ ! -d "$path" ]]; then
    die "${label} does not exist or is not a directory: ${path}"
  fi
  # Reject if final path component is a symlink (require_abs_path already
  # rejects the path itself being a symlink; also reject symlink dirs).
  if [[ -L "$path" ]]; then
    die "${label} must not be a symlink: ${path}"
  fi
  printf '%s\n' "$path"
}

ensure_runtime_symlink() {
  local link_path="$1" target="$2" label="$3"
  if [[ -L "$link_path" ]]; then
    local current
    current="$(readlink "$link_path")"
    if [[ "$current" == "$target" ]]; then
      return 0
    fi
    die "existing ${label} symlink points elsewhere; refusing overwrite"
  fi
  if [[ -e "$link_path" ]]; then
    die "existing ${label} path is not the expected symlink; refusing overwrite"
  fi
  ln -s "$target" "$link_path"
}

[[ -n "$SHA" && -n "$REPO_ROOT" && -n "$RUNTIME_DATA" && -n "$RUNTIME_VENV" ]] || {
  usage
  exit 2
}

[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "release SHA must be an exact 40-hex commit id"

require_abs_path "repository root" "$REPO_ROOT"
require_abs_path "release root" "$RELEASE_ROOT"
RUNTIME_DATA="$(resolve_existing_dir "runtime data" "$RUNTIME_DATA")"
RUNTIME_VENV="$(resolve_existing_dir "runtime venv" "$RUNTIME_VENV")"

if [[ ! -d "$REPO_ROOT/.git" && ! -f "$REPO_ROOT/.git" ]]; then
  die "repository root is not a git repository: ${REPO_ROOT}"
fi

if ! git -C "$REPO_ROOT" cat-file -e "${SHA}^{commit}" 2>/dev/null; then
  die "commit not found in repository: ${SHA}"
fi

release="${RELEASE_ROOT}/${SHA}"
required_paths=(
  "ops/quant-monitor/scripts/common_env.sh"
  "ops/quant-monitor/scripts/health_check.sh"
  "ops/quant-monitor/scripts/daily_briefing_pipeline.sh"
  "ops/quant-monitor/AGENTS.md"
)

stage_release() {
  local destination="$1"
  # mktemp creates the top-level directory as 0700.  The systemd service runs
  # as ubuntu, so make only this release root traversable/readable; preserve
  # the archive's modes for all files and nested directories.
  chmod 755 "$destination" || die "unable to make staged release readable"
  git -C "$REPO_ROOT" archive --format=tar "$SHA" | tar -x -C "$destination" \
    || die "git archive or extraction failed for ${SHA}"

  for rel in "${required_paths[@]}"; do
    if [[ ! -e "${destination}/${rel}" ]]; then
      die "archived tree missing required path: ${rel}"
    fi
  done

  ensure_runtime_symlink "${destination}/ops/quant-monitor/data" "$RUNTIME_DATA" "data"
  ensure_runtime_symlink "${destination}/ops/quant-monitor/.venv" "$RUNTIME_VENV" ".venv"
}

verify_existing_release() (
  local existing="$1"
  local verification
  verification="$(mktemp -d "${RELEASE_ROOT}/.verify.XXXXXX")" \
    || die "unable to create verification directory"
  trap 'rm -rf -- "$verification"' EXIT

  stage_release "$verification"
  if ! diff -qr --exclude=data --exclude=.venv "$verification" "$existing" >/dev/null; then
    die "existing release differs; refusing overwrite"
  fi
)

if [[ -e "$release" || -L "$release" ]]; then
  if [[ -L "$release" || ! -d "$release" ]]; then
    die "existing release target is not a directory; refusing overwrite"
  fi
  verify_existing_release "$release"
  echo "release_reused=${release}"
  exit 0
fi

if [[ ! -d "$RELEASE_ROOT" ]]; then
  mkdir -p "$RELEASE_ROOT" || die "unable to create release root: ${RELEASE_ROOT}"
fi
if [[ ! -d "$RELEASE_ROOT" || -L "$RELEASE_ROOT" || ! -w "$RELEASE_ROOT" ]]; then
  die "release root is not a writable directory: ${RELEASE_ROOT}"
fi

staged=""
cleanup() {
  if [[ -n "${staged}" && -d "${staged}" ]]; then
    # Own staging directory only; never touch runtime/release targets.
    rm -rf -- "${staged}"
  fi
}
trap cleanup EXIT

staged="$(mktemp -d "${RELEASE_ROOT}/.stage.XXXXXX")" || die "unable to create staging directory"

stage_release "$staged"

mv "$staged" "$release" || die "atomic install move failed"
staged=""
trap - EXIT

echo "release_installed=${release}"
