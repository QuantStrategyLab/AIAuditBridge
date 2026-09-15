#!/usr/bin/env bash
# Install the root-only GitHub App issuer and org-health token wiring.
set -euo pipefail

APP_ID=""
INSTALLATION_ID=""
SERVICE_NAME="codex-audit-service"
DEPLOY_DIR="/opt/codex-audit-bridge"
PRIVATE_KEY_STDIN=0
PAYLOAD_DIR="${CODEX_ORG_HEALTH_PAYLOAD_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
EXPECTED_ORG_HEALTH_SHA256="d74d8f3f0630d40c8b09e863315a47d3f03151830f135810326257040f97750b"

fail() {
  echo "org-health provision failed: $1" >&2
  exit 1
}

if [ "$(id -u)" -ne 0 ]; then
  fail "root_required"
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    --app-id) APP_ID="${2:-}"; shift 2 ;;
    --installation-id) INSTALLATION_ID="${2:-}"; shift 2 ;;
    --service-name) SERVICE_NAME="${2:-}"; shift 2 ;;
    --deploy-dir) DEPLOY_DIR="${2:-}"; shift 2 ;;
    --payload-dir) PAYLOAD_DIR="${2:-}"; shift 2 ;;
    --private-key-stdin) PRIVATE_KEY_STDIN=1; shift ;;
    *) fail "invalid_argument" ;;
  esac
done

[[ "$APP_ID" =~ ^[1-9][0-9]*$ ]] || fail "invalid_app_id"
[[ "$INSTALLATION_ID" =~ ^[1-9][0-9]*$ ]] || fail "invalid_installation_id"
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]] || fail "invalid_service_name"
[[ "$DEPLOY_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail "invalid_deploy_dir"
[[ "$PAYLOAD_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail "invalid_payload_dir"
[ "$PRIVATE_KEY_STDIN" -eq 1 ] || fail "private_key_stdin_required"

ISSUER_SOURCE="$PAYLOAD_DIR/refresh_org_health_github_token.py"
MODULE_SOURCE="$PAYLOAD_DIR/org_health.py"
SERVICE_SOURCE="$PAYLOAD_DIR/codex-org-health-refresh.service"
TIMER_SOURCE="$PAYLOAD_DIR/codex-org-health-refresh.timer"
for source in "$ISSUER_SOURCE" "$MODULE_SOURCE" "$SERVICE_SOURCE" "$TIMER_SOURCE"; do
  [ -f "$source" ] && [ ! -L "$source" ] || fail "payload_missing"
done

CONFIG_DIR="/etc/codex-org-health"
ISSUER_DIR="/usr/local/lib/codex-org-health"
TOKEN_DIR="/run/codex-org-health"
DROPIN_DIR="/etc/systemd/system/${SERVICE_NAME}.service.d"
DROPIN_FILE="$DROPIN_DIR/org-health-token.conf"
SERVICE_DIR="$DEPLOY_DIR/service"
MODULE_TARGET="$SERVICE_DIR/org_health.py"
MODULE_BACKUP="$SERVICE_DIR/org_health.py.pre-org-health-token"

for directory in "$CONFIG_DIR" "$ISSUER_DIR" "$TOKEN_DIR" "$DROPIN_DIR" "$DEPLOY_DIR" "$DEPLOY_DIR/service"; do
  [ ! -L "$directory" ] || fail "directory_symlink"
done
for target in "$ISSUER_DIR/refresh_org_health_github_token.py" "$DEPLOY_DIR/service/org_health.py" \
  /etc/systemd/system/codex-org-health-refresh.service /etc/systemd/system/codex-org-health-refresh.timer "$DROPIN_FILE"; do
  [ ! -L "$target" ] || fail "target_symlink"
done
if [ ! -d "$SERVICE_DIR" ] || [ -L "$SERVICE_DIR" ]; then
  fail "service_directory_missing_or_unsafe"
fi
service_dir_mode="$(stat -c '%u %a' "$SERVICE_DIR" 2>/dev/null || true)"
service_dir_uid="${service_dir_mode%% *}"
service_dir_perm="${service_dir_mode##* }"
[ "$service_dir_uid" = "0" ] || fail "service_directory_unsafe"
[[ "$service_dir_perm" =~ ^[0-7]{3,4}$ ]] || fail "service_directory_unsafe"
(( (8#$service_dir_perm & 18) == 0 )) || fail "service_directory_unsafe"
if [ ! -f "$MODULE_TARGET" ] || [ -L "$MODULE_TARGET" ]; then
  fail "org_health_module_missing_or_unsafe"
fi
module_sha256="$(sha256sum "$MODULE_TARGET" | awk '{print $1}')"
[ "$module_sha256" = "$EXPECTED_ORG_HEALTH_SHA256" ] || fail "org_health_module_drift"
[ ! -L "$MODULE_BACKUP" ] || fail "org_health_module_backup_exists"
[ ! -e "$MODULE_BACKUP" ] || fail "org_health_module_backup_exists"

install -d -o root -g root -m 0700 "$CONFIG_DIR"
install -d -o root -g root -m 0755 "$ISSUER_DIR"
install -d -o root -g ubuntu -m 0750 "$TOKEN_DIR"
install -d -o root -g root -m 0755 "$DROPIN_DIR"

tmp_app="$(mktemp)"
tmp_installation="$(mktemp)"
tmp_key="$(mktemp)"
cleanup() {
  rm -f "$tmp_app" "$tmp_installation" "$tmp_key"
}
trap cleanup EXIT
umask 077
printf '%s\n' "$APP_ID" >"$tmp_app"
printf '%s\n' "$INSTALLATION_ID" >"$tmp_installation"
cat >"$tmp_key"
grep -Eq -- '-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----' "$tmp_key" || fail "private_key_invalid"
grep -Eq -- '-----END [A-Z0-9 ]*PRIVATE KEY-----' "$tmp_key" || fail "private_key_invalid"
install -o root -g root -m 0600 "$tmp_app" "$CONFIG_DIR/app-id"
install -o root -g root -m 0600 "$tmp_installation" "$CONFIG_DIR/installation-id"
install -o root -g root -m 0600 "$tmp_key" "$CONFIG_DIR/app-private-key.pem"

# The service code and issuer are installed as root-owned files.  Only this
# module is replaced in the existing service tree; unrelated service files and
# drop-ins remain untouched.
install -o root -g root -m 0644 "$MODULE_TARGET" "$MODULE_BACKUP"
install -o root -g root -m 0755 "$ISSUER_SOURCE" "$ISSUER_DIR/refresh_org_health_github_token.py"
install -o root -g root -m 0644 "$MODULE_SOURCE" "$MODULE_TARGET"
install -o root -g root -m 0644 "$SERVICE_SOURCE" /etc/systemd/system/codex-org-health-refresh.service
install -o root -g root -m 0644 "$TIMER_SOURCE" /etc/systemd/system/codex-org-health-refresh.timer
if [ -L "$DROPIN_FILE" ]; then
  fail "dropin_symlink"
fi
umask 022
cat >"$DROPIN_FILE" <<EOF_DROPIN
[Service]
Environment=CODEX_AUDIT_SERVICE_GITHUB_TOKEN_FILE=/run/codex-org-health/installation-token.json
EOF_DROPIN
chown root:root "$DROPIN_FILE"
chmod 0644 "$DROPIN_FILE"

systemctl daemon-reload
echo "org_health_provisioned=1"
