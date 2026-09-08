#!/usr/bin/env bash
# Install opt-in Cursor configuration and a bounded account-roster refresh timer.
# The existing AI gateway owns HTTP/auth/jobs. No login or model call occurs here.
set -euo pipefail
MODE="${1:-inspect}"
CURSOR_DEPLOY_DIR="${CODEX_AUDIT_SERVICE_DEPLOY_DIR:-/opt/codex-audit-bridge}"
CURSOR_SERVICE_USER="${AI_GATEWAY_CURSOR_SERVICE_USER:-ubuntu}"
CURSOR_EXECUTABLE="${AI_GATEWAY_CURSOR_BIN:-/home/ubuntu/.local/bin/agent}"
CURSOR_CONFIG_ROOT=/etc/codex-audit-bridge
CURSOR_POLICY_ROOT=/etc/codex-audit-bridge-policy
if [[ "$MODE" == inspect ]]; then
  test -x "$CURSOR_EXECUTABLE" && echo 'cursor_cli=present' || echo 'cursor_cli=missing'
  systemctl is-active cursor-model-catalog-sync.timer || true
  exit 0
fi
if [[ "$MODE" != install ]]; then
  echo 'usage: deploy_cursor_research.sh {inspect|install}' >&2
  exit 2
fi
sudo -n true
[[ -x "$CURSOR_EXECUTABLE" ]] || { echo 'installed Cursor CLI required' >&2; exit 1; }
[[ -f "$CURSOR_DEPLOY_DIR/service/cursor_account.py" ]] || { echo 'deploy the matching AI gateway package first' >&2; exit 1; }
# Preserve existing approved configuration, including a deliberately disabled lane.
for config_directory in "$CURSOR_CONFIG_ROOT" "$CURSOR_POLICY_ROOT"; do
  if ! sudo test -d "$config_directory"; then
    sudo install -d -m 0755 "$config_directory"
  fi
done
if ! sudo test -e "$CURSOR_POLICY_ROOT/cursor_research.json"; then
  sudo install -m 0644 -o root -g root ops/cursor-research/policy.example.json "$CURSOR_POLICY_ROOT/cursor_research.json"
fi
if ! sudo test -e "$CURSOR_CONFIG_ROOT/cursor.env"; then
  sudo tee "$CURSOR_CONFIG_ROOT/cursor.env" >/dev/null <<EOF
AI_GATEWAY_CURSOR_ENABLED=false
AI_GATEWAY_CURSOR_FALLBACK_ENABLED=false
AI_GATEWAY_CURSOR_BIN=$CURSOR_EXECUTABLE
AI_GATEWAY_CURSOR_POLICY_PATH=$CURSOR_POLICY_ROOT/cursor_research.json
MODEL_CATALOG_PATH=/var/lib/codex-audit-bridge/model_catalog.json
EOF
  sudo chmod 0600 "$CURSOR_CONFIG_ROOT/cursor.env"
fi
sudo tee /etc/systemd/system/cursor-model-catalog-sync.service >/dev/null <<EOF
[Unit]
Description=Refresh Cursor account model roster for existing AI gateway
After=network-online.target
[Service]
Type=oneshot
User=$CURSOR_SERVICE_USER
WorkingDirectory=$CURSOR_DEPLOY_DIR
EnvironmentFile=$CURSOR_CONFIG_ROOT/cursor.env
StateDirectory=codex-audit-bridge
ExecStart=/usr/bin/python3 $CURSOR_DEPLOY_DIR/scripts/sync_model_catalog.py --subscriptions-only
NoNewPrivileges=true
TimeoutStartSec=90
EOF
sudo tee /etc/systemd/system/cursor-model-catalog-sync.timer >/dev/null <<'EOF'
[Unit]
Description=Refresh Cursor account model roster daily
[Timer]
OnCalendar=*-*-* 06:20:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now cursor-model-catalog-sync.timer
# No gateway restart, auth setup, policy activation or model execution.
echo 'Cursor refresh timer installed; research activation remains a separate configured decision'
