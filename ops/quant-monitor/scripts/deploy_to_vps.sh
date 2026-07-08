#!/usr/bin/env bash
# Deploy ops/quant-monitor to VPS (pull AIAuditBridge + setup venv + systemd).
set -euo pipefail

VPS_HOST="${VPS_HOST:-qvps}"
VPS_PORT="${VPS_PORT:-8822}"
AAB_ROOT="${AIAUDIT_BRIDGE_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}"
REMOTE_AAB="${REMOTE_AAB:-/home/ubuntu/Projects/AIAuditBridge}"
REMOTE_MONITOR="$REMOTE_AAB/ops/quant-monitor"

echo "[deploy] updating AIAuditBridge on ${VPS_HOST}"
ssh -p "${VPS_PORT}" "${VPS_HOST}" bash -s <<REMOTE
set -euo pipefail
mkdir -p "$REMOTE_AAB"
if [[ -d "$REMOTE_AAB/.git" ]]; then
  git -C "$REMOTE_AAB" fetch origin main --quiet
  git -C "$REMOTE_AAB" checkout main --quiet
  git -C "$REMOTE_AAB" pull --ff-only origin main --quiet
else
  git clone --depth 1 https://github.com/QuantStrategyLab/AIAuditBridge.git "$REMOTE_AAB"
fi
REMOTE

echo "[deploy] syncing local ops changes (if any)"
rsync -avz -e "ssh -p ${VPS_PORT}" \
  --exclude '.git' \
  --exclude 'data/' \
  --exclude '.venv/' \
  "$AAB_ROOT/ops/quant-monitor/" "${VPS_HOST}:${REMOTE_MONITOR}/"

echo "[deploy] bootstrap runtime + systemd"
ssh -p "${VPS_PORT}" "${VPS_HOST}" bash -s <<'REMOTE'
set -euo pipefail
REMOTE_AAB="/home/ubuntu/Projects/AIAuditBridge"
REMOTE_MONITOR="$REMOTE_AAB/ops/quant-monitor"
OLD_UNIT="/etc/systemd/system/codex-quant.service"
CHAT_ID=""
if [[ -f "$OLD_UNIT" ]]; then
  CHAT_ID="$(grep -E '^Environment=GLOBAL_TELEGRAM_CHAT_ID=' "$OLD_UNIT" | head -1 | cut -d= -f2- || true)"
fi

bash "$REMOTE_MONITOR/scripts/setup_vps_runtime.sh"

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
sudo systemctl start codex-quant.service || true

systemctl is-active codex-quant.timer
systemctl is-active codex-daily-briefing.timer
systemctl show codex-quant.service -p ExecStart --value | head -1
REMOTE

echo "[deploy] done — health every 30m, daily briefing 22:30 UTC"
