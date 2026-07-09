#!/usr/bin/env bash
# Deploy / refresh monthly model-catalog sync on the Codex VPS.
# Intended to run on the self-hosted codex-vps runner (no interactive prompts).
set -euo pipefail

MODE="${1:-deploy}"
AAB_ROOT="${AIAUDIT_BRIDGE_ROOT:-/home/ubuntu/Projects/AIAuditBridge}"
UNIT_SRC="${AAB_ROOT}/ops/codex-audit/systemd"
ENV_FILE="/etc/codex-audit-bridge/model-catalog.env"
CATALOG_PATH="/var/lib/codex-audit-bridge/model_catalog.json"

require_sudo() {
  if ! sudo -n true 2>/dev/null; then
    echo "passwordless sudo is required on the VPS runner" >&2
    exit 1
  fi
}

pull_bridge() {
  mkdir -p "$(dirname "$AAB_ROOT")"
  if [[ -d "${AAB_ROOT}/.git" ]]; then
    git -C "$AAB_ROOT" fetch origin main --quiet
    git -C "$AAB_ROOT" checkout main --quiet
    git -C "$AAB_ROOT" pull --ff-only origin main --quiet
  else
    git clone --depth 1 https://github.com/QuantStrategyLab/AIAuditBridge.git "$AAB_ROOT"
  fi
  echo "bridge_head=$(git -C "$AAB_ROOT" rev-parse --short HEAD)"
}

write_env_file() {
  local openai="${OPENAI_API_KEY:-}"
  local anthropic="${ANTHROPIC_API_KEY:-}"
  if [[ -z "$openai" && -z "$anthropic" ]]; then
    echo "OPENAI_API_KEY or ANTHROPIC_API_KEY required for live catalog sync" >&2
    exit 1
  fi
  sudo mkdir -p /etc/codex-audit-bridge
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
  sudo cp "${UNIT_SRC}/model-catalog-sync.service.example" /etc/systemd/system/model-catalog-sync.service
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
    systemctl status model-catalog-sync.service --no-pager --lines=40 >&2 || true
    journalctl -u model-catalog-sync.service -n 80 --no-pager >&2 || true
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
  pull_bridge
  write_env_file
  install_units
  run_sync_now
  echo "deploy complete"
}

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
