#!/usr/bin/env bash
# Deploy ops/quant-monitor to VPS (pull AIAuditBridge + setup venv + systemd).
set -euo pipefail

VPS_HOST="${VPS_HOST:-qvps}"
VPS_PORT="${VPS_PORT:-8822}"
SOURCE_SHA="${AIAUDIT_BRIDGE_SOURCE_SHA:-}"
if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[deploy] AIAUDIT_BRIDGE_SOURCE_SHA must be the reviewed 40-character main SHA" >&2
  exit 2
fi
# Deploy only the exact production checkout fetched below; never overlay a
# developer worktree or an uncommitted local ops/quant-monitor directory.
REMOTE_AAB="/home/ubuntu/quant-monitor-runtime/AIAuditBridge"
REMOTE_MONITOR="$REMOTE_AAB/ops/quant-monitor"

echo "[deploy] updating AIAuditBridge on ${VPS_HOST}"
ssh -p "${VPS_PORT}" "${VPS_HOST}" bash -s -- "$SOURCE_SHA" "$REMOTE_AAB" <<'REMOTE'
set -euo pipefail
SOURCE_SHA="$1"
REMOTE_AAB="$2"
mkdir -p "$REMOTE_AAB"
if [[ -d "$REMOTE_AAB/.git" ]]; then
  if [[ -n "$(git -C "$REMOTE_AAB" status --porcelain --untracked-files=all -- client scripts service ops/quant-monitor/scripts ops/quant-monitor/systemd)" ]]; then
    echo "[deploy] refusing dirty production runtime source" >&2
    exit 1
  fi
  git -C "$REMOTE_AAB" fetch origin main --quiet
else
  git clone --depth 1 https://github.com/QuantStrategyLab/AIAuditBridge.git "$REMOTE_AAB"
fi
REMOTE_MAIN_SHA="$(git -C "$REMOTE_AAB" rev-parse origin/main)"
if [[ "$REMOTE_MAIN_SHA" != "$SOURCE_SHA" ]]; then
  echo "[deploy] requested SHA does not match fetched main" >&2
  exit 1
fi
git -C "$REMOTE_AAB" checkout --detach "$SOURCE_SHA" --quiet
echo "[deploy] fixed source SHA ${SOURCE_SHA}"
REMOTE

echo "[deploy] bootstrap runtime + systemd"
ssh -p "${VPS_PORT}" "${VPS_HOST}" bash -s -- "$SOURCE_SHA" "$REMOTE_AAB" <<'REMOTE'
set -euo pipefail
SOURCE_SHA="$1"
REMOTE_AAB="$2"
REMOTE_MONITOR="$REMOTE_AAB/ops/quant-monitor"
OLD_UNIT="/etc/systemd/system/codex-quant.service"
CHAT_ID=""
if [[ -f "$OLD_UNIT" ]]; then
  CHAT_ID="$(
    grep -E '^Environment=GLOBAL_TELEGRAM_CHAT_ID=' "$OLD_UNIT" \
      | head -1 \
      | sed -E 's/^Environment=(GLOBAL_TELEGRAM_CHAT_ID=)+//' \
      || true
  )"
fi

AIAUDIT_BRIDGE_ROOT="$REMOTE_AAB" \
  QUANT_MONITOR_ROOT="$REMOTE_MONITOR" \
  bash "$REMOTE_MONITOR/scripts/setup_vps_runtime.sh" "$SOURCE_SHA" "$REMOTE_AAB"

sudo systemctl stop codex-quant.service 2>/dev/null || true
sudo systemctl disable codex-quant.service 2>/dev/null || true

install_unit() {
  local src="$1" dest="$2"
  if [[ -n "$CHAT_ID" ]]; then
    sed "s/^Environment=GLOBAL_TELEGRAM_CHAT_ID=$/Environment=GLOBAL_TELEGRAM_CHAT_ID=${CHAT_ID}/" "$src" > "/tmp/$(basename "$dest")"
    sudo cp "/tmp/$(basename "$dest")" "$dest"
  else
    sudo cp "$src" "$dest"
  fi
}

install_unit "$REMOTE_MONITOR/systemd/codex-quant.service.example" /etc/systemd/system/codex-quant.service
install_unit "$REMOTE_MONITOR/systemd/codex-daily-briefing.service.example" /etc/systemd/system/codex-daily-briefing.service
sudo cp "$REMOTE_MONITOR/systemd/codex-quant.timer.example" /etc/systemd/system/codex-quant.timer
sudo cp "$REMOTE_MONITOR/systemd/codex-daily-briefing.timer.example" /etc/systemd/system/codex-daily-briefing.timer

sudo systemctl daemon-reload
sudo systemctl enable codex-quant.timer codex-daily-briefing.timer
sudo systemctl restart codex-quant.timer codex-daily-briefing.timer
if ! sudo systemctl start codex-quant.service; then
  monitor_status="$(systemctl show codex-quant.service -p ExecMainStatus --value)"
  if [[ "$monitor_status" != "2" ]]; then
    echo "[deploy] codex-quant.service failed with unexpected status ${monitor_status}" >&2
    exit 1
  fi
  echo "[deploy] codex-quant.service completed with active monitor alerts" >&2
fi

systemctl is-active codex-quant.timer
systemctl is-active codex-daily-briefing.timer
systemctl show codex-quant.service -p ExecStart --value | head -1
REMOTE

echo "[deploy] done — health every 30m, daily briefing 22:30 UTC"
