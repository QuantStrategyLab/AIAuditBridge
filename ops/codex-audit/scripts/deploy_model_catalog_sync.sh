#!/usr/bin/env bash
# Deploy / refresh monthly model-catalog sync on the Codex VPS.
# Intended to run on the self-hosted codex-vps runner (no interactive prompts).
set -euo pipefail

MODE="${1:-deploy}"
AAB_ROOT="${AIAUDIT_BRIDGE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
UNIT_SRC="${AAB_ROOT}/ops/codex-audit/systemd"
RELEASE_ROOT="${MODEL_CATALOG_RELEASE_ROOT:-/opt/codex-model-catalog/releases}"
SYNC_CODE_ROOT=""
ENV_FILE="/etc/codex-audit-bridge/model-catalog.env"
CATALOG_PATH="/var/lib/codex-audit-bridge/model_catalog.json"

require_sudo() {
  if ! sudo -n true 2>/dev/null; then
    echo "passwordless sudo is required on the VPS runner" >&2
    exit 1
  fi
}

stage_bridge_release() (
  # Archive committed inputs, never checkout/reset a developer's long-lived tree.
  set -euo pipefail
  local revision="${MODEL_CATALOG_REVISION:-${GITHUB_SHA:-}}"
  if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]] || [[ "$(git -C "$AAB_ROOT" rev-parse HEAD)" != "$revision" ]]; then
    echo "model catalog deployment requires the exact checkout revision" >&2
    return 1
  fi
  if [[ ! "$RELEASE_ROOT" =~ ^/[A-Za-z0-9_./-]+$ ]] || [[ -L "$RELEASE_ROOT" ]]; then
    echo "invalid model catalog release directory" >&2
    return 1
  fi
  local release="$RELEASE_ROOT/$revision" archive_dir staged=""
  archive_dir="$(mktemp -d)"
  trap 'rm -rf "$archive_dir"; if [[ -n "$staged" ]]; then sudo rm -rf "$staged"; fi' EXIT
  git -C "$AAB_ROOT" archive "$revision" service scripts/sync_model_catalog.py generated/model_catalog.json ops/codex-audit/systemd | tar -x -C "$archive_dir"
  chmod -R a+rX "$archive_dir"
  if [[ -e "$release" || -L "$release" ]]; then
    if [[ -L "$release" || ! -d "$release" ]] || ! diff -qr "$archive_dir" "$release" >/dev/null; then
      echo "existing model catalog release differs; refusing overwrite" >&2
      return 1
    fi
  else
    if [[ ! -d "$RELEASE_ROOT" ]]; then
      sudo install -d -m 0755 "$RELEASE_ROOT"
    fi
    staged="$(sudo mktemp -d "$RELEASE_ROOT/.stage.XXXXXX")"
    sudo cp -R "$archive_dir/." "$staged/"
    sudo chmod -R a+rX "$staged"
    sudo mv "$staged" "$release"
    staged=""
  fi
  echo "bridge_head=$revision"
)

select_release() {
  local revision="${MODEL_CATALOG_REVISION:-${GITHUB_SHA:-}}"
  stage_bridge_release
  SYNC_CODE_ROOT="$RELEASE_ROOT/$revision"
  UNIT_SRC="$SYNC_CODE_ROOT/ops/codex-audit/systemd"
}

write_env_file() {
  # The existing root-owned credentials and directory permissions are preserved.
  if sudo test -f "$ENV_FILE"; then
    echo "existing model catalog environment preserved"
    return
  fi
  local openai="${OPENAI_API_KEY:-}"
  local anthropic="${ANTHROPIC_API_KEY:-}"
  if [[ -z "$openai" && -z "$anthropic" ]]; then
    echo "OPENAI_API_KEY or ANTHROPIC_API_KEY required for live catalog sync" >&2
    exit 1
  fi
  if ! sudo test -d "$(dirname "$ENV_FILE")"; then
    sudo install -d -m 0700 "$(dirname "$ENV_FILE")"
  fi
  local tmp
  tmp="$(mktemp)"
  umask 077
  {
    if [[ -n "$openai" ]]; then
      printf 'OPENAI_API_KEY=%s\n' "$openai"
    fi
    if [[ -n "$anthropic" ]]; then
      printf 'ANTHROPIC_API_KEY=%s\n' "$anthropic"
    fi
  } >"$tmp"
  sudo install -m 600 -o root -g ubuntu "$tmp" "$ENV_FILE"
  rm -f "$tmp"
  echo "wrote ${ENV_FILE} (mode 0600)"
}

install_units() {
  local unit_file
  unit_file="$(mktemp)"
  sed "s|@MODEL_CATALOG_CODE_ROOT@|$SYNC_CODE_ROOT|g" "${UNIT_SRC}/model-catalog-sync.service.example" > "$unit_file"
  sudo install -m 0644 "$unit_file" /etc/systemd/system/model-catalog-sync.service
  rm -f "$unit_file"
  sudo cp "${UNIT_SRC}/model-catalog-sync.timer.example" /etc/systemd/system/model-catalog-sync.timer
  sudo systemctl daemon-reload
  sudo systemctl enable model-catalog-sync.timer
  sudo systemctl restart model-catalog-sync.timer
  echo "timer_enabled=$(systemctl is-enabled model-catalog-sync.timer)"
  echo "timer_active=$(systemctl is-active model-catalog-sync.timer)"
}

run_sync_now() {
  # Ensure StateDirectory exists with correct ownership before first oneshot.
  sudo mkdir -p /var/lib/codex-audit-bridge
  sudo chown ubuntu:ubuntu /var/lib/codex-audit-bridge
  sudo systemctl start model-catalog-sync.service
  local rc=0
  systemctl is-failed model-catalog-sync.service >/dev/null 2>&1 && rc=1 || true
  if [[ "$rc" -ne 0 ]]; then
    echo "model-catalog-sync.service failed" >&2
    exit 1
  fi
  if [[ ! -f "$CATALOG_PATH" ]]; then
    echo "catalog missing after sync: ${CATALOG_PATH}" >&2
    exit 1
  fi
  echo "catalog_path=${CATALOG_PATH}"
  python3 - <<'PY'
import json
from pathlib import Path
path = Path("/var/lib/codex-audit-bridge/model_catalog.json")
payload = json.loads(path.read_text(encoding="utf-8"))
tiers = {name: spec.get("model") for name, spec in (payload.get("tiers") or {}).items()}
models = payload.get("models") or {}
top = sorted(
    models.values(),
    key=lambda item: (float(item.get("capability_score") or 0.0), str(item.get("model_id") or "")),
    reverse=True,
)[:12]
print(f"catalog_source={payload.get('catalog_source')}")
print(f"synced_at={payload.get('synced_at')}")
print(f"tiers={json.dumps(tiers, sort_keys=True)}")
print(f"deprecated={payload.get('deprecated')}")
print(f"inventory_count={len(models)}")
print(f"has_gpt_5_6={any('5.6' in str(mid) for mid in models)}")
print(
    "top_models="
    + json.dumps(
        [
            {
                "model": item.get("model_id"),
                "provider": item.get("provider"),
                "score": round(float(item.get("capability_score") or 0.0), 4),
            }
            for item in top
        ],
        sort_keys=True,
    )
)
PY
}

inspect() {
  echo "## bridge"
  if [[ -d "${AAB_ROOT}/.git" ]]; then
    git -C "$AAB_ROOT" rev-parse --short HEAD
    git -C "$AAB_ROOT" log -1 --oneline
  else
    echo "missing ${AAB_ROOT}"
  fi
  echo
  echo "## env"
  if sudo test -f "$ENV_FILE"; then
    sudo bash -c "grep -E '^[A-Z0-9_]+=' '$ENV_FILE' | sed -E 's/=.*/=<present>/'"
  else
    echo "missing ${ENV_FILE}"
  fi
  echo
  echo "## timer"
  systemctl is-enabled model-catalog-sync.timer 2>/dev/null || echo "timer not enabled"
  systemctl is-active model-catalog-sync.timer 2>/dev/null || echo "timer not active"
  systemctl list-timers model-catalog-sync.timer --no-pager 2>/dev/null || true
  echo
  echo "## catalog"
  if [[ -f "$CATALOG_PATH" ]]; then
    python3 - <<'PY'
import json
from pathlib import Path
path = Path("/var/lib/codex-audit-bridge/model_catalog.json")
payload = json.loads(path.read_text(encoding="utf-8"))
print(f"synced_at={payload.get('synced_at')}")
print(f"catalog_source={payload.get('catalog_source')}")
print(f"tiers={sorted((payload.get('tiers') or {}).keys())}")
PY
  else
    echo "missing ${CATALOG_PATH}"
  fi
}

deploy() {
  require_sudo
  select_release
  write_env_file
  install_units
  run_sync_now
  echo "deploy complete"
}

# Functions may be sourced by offline deployment fixtures without side effects.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return
fi

case "$MODE" in
  inspect)
    inspect
    ;;
  deploy)
    deploy
    ;;
  sync-now)
    require_sudo
    run_sync_now
    ;;
  *)
    echo "usage: $0 {inspect|deploy|sync-now}" >&2
    exit 2
    ;;
esac
