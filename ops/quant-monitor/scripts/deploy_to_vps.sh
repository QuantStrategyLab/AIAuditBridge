#!/usr/bin/env bash
# Deploy ops/quant-monitor to VPS with QuantSentinel GCP secret wiring.
set -euo pipefail

VPS_HOST="${VPS_HOST:-qvps}"
VPS_PORT="${VPS_PORT:-8822}"
AAB_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
LOCAL_ROOT="$AAB_ROOT/ops/quant-monitor"
REMOTE_AAB="${REMOTE_AAB:-~/Projects/AIAuditBridge}"
REMOTE_DIR="${REMOTE_DIR:-$REMOTE_AAB/ops/quant-monitor}"

echo "[deploy] syncing $LOCAL_ROOT -> ${VPS_HOST}:${REMOTE_DIR}"
ssh -p "${VPS_PORT}" "${VPS_HOST}" "mkdir -p ${REMOTE_AAB}/ops"
rsync -avz -e "ssh -p ${VPS_PORT}" \
  --exclude '.git' \
  --exclude 'data/' \
  "$LOCAL_ROOT/" "${VPS_HOST}:${REMOTE_DIR}/"

echo "[deploy] installing systemd unit"
ssh -p "${VPS_PORT}" "${VPS_HOST}" bash -s <<REMOTE
set -euo pipefail
REMOTE_DIR="/home/ubuntu/Projects/AIAuditBridge/ops/quant-monitor"
sudo cp "\${REMOTE_DIR}/systemd/codex-quant.service.example" /etc/systemd/system/codex-quant.service
sudo systemctl daemon-reload
sudo systemctl enable codex-quant.service
sudo systemctl restart codex-quant.service
systemctl is-active codex-quant.service
REMOTE

echo "[deploy] done — configure GLOBAL_TELEGRAM_CHAT_ID in /etc/systemd/system/codex-quant.service"
