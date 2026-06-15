#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-inspect}"

AUDIT_SERVICE_NAME="${CODEX_AUDIT_SERVICE_SYSTEMD_NAME:-codex-audit-service}"
ROUTER_SERVICE_NAME="${CODEX_SERVICE_ROUTER_SYSTEMD_NAME:-codex-service-router}"
GATEWAY_SERVICE_NAME="${CODEX_GATEWAY_SERVICE_SYSTEMD_NAME:-codex-gateway-service}"
DEPLOY_DIR="${CODEX_AUDIT_SERVICE_DEPLOY_DIR:-/opt/codex-audit-bridge}"
AUDIT_PORT="${CODEX_AUDIT_SERVICE_PORT:-8797}"
ROUTER_PORT="${CODEX_SERVICE_ROUTER_PORT:-8787}"
GATEWAY_PORT="${CODEX_GATEWAY_SERVICE_PORT:-8788}"
AUDIENCE="${CODEX_AUDIT_SERVICE_AUDIENCE:-quant-codex-audit}"
ALLOWED_REPOSITORIES="${CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES:-QuantStrategyLab/CodexAuditBridge}"
ALLOWED_SOURCE_REPOSITORIES="${CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES:-QuantStrategyLab/CryptoSnapshotPipelines,QuantStrategyLab/CryptoLivePoolPipelines,QuantStrategyLab/HkEquitySnapshotPipelines,QuantStrategyLab/UsEquitySnapshotPipelines,QuantStrategyLab/AiLongHorizonSignalPipelines,QuantStrategyLab/ResearchSignalContextPipelines}"

require_sudo() {
  if ! sudo -n true; then
    echo "sudo without password is required on the self-hosted runner" >&2
    exit 1
  fi
}

systemctl_status_brief() {
  local service="$1"
  if systemctl list-unit-files "$service.service" >/dev/null 2>&1; then
    systemctl is-enabled "$service" 2>/dev/null || true
    systemctl is-active "$service" 2>/dev/null || true
    systemctl status "$service" --no-pager --lines=8 2>/dev/null || true
  fi
}

inspect() {
  echo "## Host"
  hostname
  id -un
  uname -a
  echo

  echo "## Tools"
  command -v python3 || true
  command -v codex || true
  if command -v codex >/dev/null 2>&1; then
    codex --version || true
  fi
  command -v systemctl || true
  command -v nginx || true
  command -v caddy || true
  echo

  echo "## Listening ports"
  ss -ltnp 2>/dev/null | grep -E ':(80|443|8787|8788|8797)\b' || true
  echo

  echo "## Services"
  systemctl_status_brief "$GATEWAY_SERVICE_NAME"
  systemctl_status_brief "$AUDIT_SERVICE_NAME"
  systemctl_status_brief "$ROUTER_SERVICE_NAME"
  systemctl_status_brief nginx
  systemctl_status_brief caddy
  echo

  echo "## Reverse proxy hints"
  if command -v nginx >/dev/null 2>&1; then
    sudo nginx -T 2>/dev/null | grep -nE 'server_name|listen|proxy_pass|codex|8787|8788|8797' | head -120 || true
  fi
}

install_file() {
  local source="$1"
  local target="$2"
  local mode="$3"
  sudo install -D -m "$mode" "$source" "$target"
}

write_audit_service_unit() {
  local runner_user
  runner_user="$(id -un)"
  sudo tee "/etc/systemd/system/${AUDIT_SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=QuantStrategyLab Codex audit service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${runner_user}
WorkingDirectory=${DEPLOY_DIR}
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=CODEX_AUDIT_SERVICE_HOST=127.0.0.1
Environment=CODEX_AUDIT_SERVICE_PORT=${AUDIT_PORT}
Environment=CODEX_AUDIT_SERVICE_AUDIENCE=${AUDIENCE}
Environment=CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=${ALLOWED_REPOSITORIES}
Environment=CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES=${ALLOWED_SOURCE_REPOSITORIES}
Environment=CODEX_AUDIT_SERVICE_SANDBOX=read-only
ExecStart=/usr/bin/env python3 ${DEPLOY_DIR}/scripts/codex_audit_service.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}

write_router_service_unit() {
  local runner_user
  runner_user="$(id -un)"
  sudo tee "/etc/systemd/system/${ROUTER_SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Codex service public route router
After=network-online.target ${AUDIT_SERVICE_NAME}.service ${GATEWAY_SERVICE_NAME}.service
Wants=network-online.target

[Service]
Type=simple
User=${runner_user}
WorkingDirectory=${DEPLOY_DIR}
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=CODEX_SERVICE_ROUTER_HOST=127.0.0.1
Environment=CODEX_SERVICE_ROUTER_PORT=${ROUTER_PORT}
Environment=CODEX_SERVICE_ROUTER_GATEWAY_UPSTREAM=http://127.0.0.1:${GATEWAY_PORT}
Environment=CODEX_SERVICE_ROUTER_AUDIT_UPSTREAM=http://127.0.0.1:${AUDIT_PORT}
ExecStart=/usr/bin/env python3 ${DEPLOY_DIR}/scripts/codex_service_router.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}

move_gateway_to_private_port() {
  if ! systemctl list-unit-files "${GATEWAY_SERVICE_NAME}.service" >/dev/null 2>&1; then
    echo "Gateway service ${GATEWAY_SERVICE_NAME}.service was not found; skipping gateway port override."
    return
  fi
  sudo mkdir -p "/etc/systemd/system/${GATEWAY_SERVICE_NAME}.service.d"
  sudo tee "/etc/systemd/system/${GATEWAY_SERVICE_NAME}.service.d/10-private-port.conf" >/dev/null <<EOF
[Service]
Environment=CODEX_GATEWAY_SERVICE_HOST=127.0.0.1
Environment=CODEX_GATEWAY_SERVICE_PORT=${GATEWAY_PORT}
EOF
}

deploy() {
  require_sudo
  install_file "scripts/codex_audit_service.py" "${DEPLOY_DIR}/scripts/codex_audit_service.py" "0755"
  install_file "scripts/codex_service_router.py" "${DEPLOY_DIR}/scripts/codex_service_router.py" "0755"
  write_audit_service_unit
  write_router_service_unit
  move_gateway_to_private_port
  sudo systemctl daemon-reload
  if systemctl list-unit-files "${GATEWAY_SERVICE_NAME}.service" >/dev/null 2>&1; then
    sudo systemctl restart "$GATEWAY_SERVICE_NAME"
  fi
  sudo systemctl enable --now "$AUDIT_SERVICE_NAME"
  sudo systemctl restart "$AUDIT_SERVICE_NAME"
  sudo systemctl enable --now "$ROUTER_SERVICE_NAME"
  sudo systemctl restart "$ROUTER_SERVICE_NAME"

  python3 - <<PY
import json
import urllib.request
for url in ["http://127.0.0.1:${AUDIT_PORT}/healthz", "http://127.0.0.1:${ROUTER_PORT}/healthz"]:
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "ok":
        raise SystemExit(f"unexpected health response from {url}: {payload}")
    print(f"{url} ok")
PY
}

case "$MODE" in
  inspect)
    inspect
    ;;
  deploy)
    deploy
    ;;
  *)
    echo "usage: $0 [inspect|deploy]" >&2
    exit 2
    ;;
esac
