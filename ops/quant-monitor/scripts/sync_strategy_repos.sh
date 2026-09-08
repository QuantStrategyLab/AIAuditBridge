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

# Only setuptools output confirmed on the dedicated QPK mirror is recoverable.
# Unknown/staged edits are user changes, even when checkout would carry them over.
metadata_only_changes() {
  local entry
  [[ "$repo" == "QuantPlatformKit" ]] || return 1
  while IFS= read -r entry; do
    case "$entry" in
      " M src/quant_platform_kit.egg-info/PKG-INFO"|" M src/quant_platform_kit.egg-info/SOURCES.txt") ;;
      *) return 1 ;;
    esac
  done <<< "$dirty"
}

replace_metadata_dirty_mirror() (
  # Preserve the entire original directory, including untracked local state.
  # A fixed backup slot makes a second dirty cycle fail instead of accumulating
  # copies or silently overwriting the only recovery copy.
  local backup="$dir.preserved-before-metadata-refresh"
  local replacement target origin
  if [[ -e "$backup" || -L "$backup" || -L "$dir" ]]; then
    echo "[sync] $repo preservation requires review" >&2
    return 1
  fi
  replacement=$(mktemp -d "$MIRROR_ROOT/.${repo}.refresh.XXXXXX") || return 1
  trap 'rm -rf -- "$replacement"' EXIT
  target=$(git -C "$dir" rev-parse origin/main) || return 1
  origin=$(git -C "$dir" remote get-url origin) || return 1
  git clone --no-hardlinks --no-checkout --quiet "$dir" "$replacement" || return 1
  git -C "$replacement" remote set-url origin "$origin" || return 1
  git -C "$replacement" checkout --detach --quiet "$target" || return 1
  git -C "$replacement" update-ref refs/remotes/origin/main "$target" || return 1
  # No source directory is moved until the replacement is complete and clean.
  [[ -z "$(git -C "$replacement" status --porcelain --untracked-files=no)" ]] || return 1
  [[ "$(git -C "$dir" status --porcelain --untracked-files=no)" == "$dirty" ]] || return 1
  mv -- "$dir" "$backup" || return 1
  if ! mv -- "$replacement" "$dir"; then
    mv -- "$backup" "$dir" || echo "[sync] $repo preserved mirror requires manual restore" >&2
    return 1
  fi
  echo "[sync] $repo original mirror preserved"
)

mkdir -p "$MIRROR_ROOT"
# The two directory renames form a recoverable swap, not a simultaneous pair.
# Exclude a second synchronizer from entering that brief gap.
sync_lock="$MIRROR_ROOT/.sync-strategy-repos.lock"
if ! mkdir "$sync_lock" 2>/dev/null; then
  echo "[sync] synchronization already active or interrupted; review lock" >&2
  exit 1
fi
trap 'rmdir -- "$sync_lock"' EXIT

for repo in "${REPOS[@]}"; do
  dir="$MIRROR_ROOT/$repo"
  if [[ -d "$dir/.git" ]]; then
    if ! git -C "$dir" fetch origin main --quiet; then
      echo "[sync] $repo fetch failed" >&2
      failed=1
      continue
    fi
    dirty=$(git -C "$dir" status --porcelain --untracked-files=no) || {
      failed=1
      continue
    }
    if [[ -n "$dirty" ]]; then
      if ! metadata_only_changes; then
        echo "[sync] $repo tracked changes require review" >&2
        failed=1
        continue
      fi
      if ! replace_metadata_dirty_mirror; then
        echo "[sync] $repo metadata mirror refresh failed" >&2
        failed=1
        continue
      fi
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
