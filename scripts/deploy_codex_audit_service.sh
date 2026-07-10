#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-inspect}"

AUDIT_SERVICE_NAME="${CODEX_AUDIT_SERVICE_SYSTEMD_NAME:-codex-audit-service}"
DEPLOY_DIR="${CODEX_AUDIT_SERVICE_DEPLOY_DIR:-/opt/codex-audit-bridge}"
AUDIT_PORT="${CODEX_AUDIT_SERVICE_PORT:-8797}"
AUDIENCE="${CODEX_AUDIT_SERVICE_AUDIENCE:-quant-codex-audit}"
ALLOWED_REPOSITORIES="${CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES:-QuantStrategyLab/AIAuditBridge,QuantStrategyLab/QuantRuntimeSettings,QuantStrategyLab/QuantPlatformKit}"
# Consumer review workflows use pull_request_target, so every OIDC workflow ref and Git ref remains pinned to refs/heads/main.
ALLOWED_WORKFLOW_REFS="${CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS:-QuantStrategyLab/AIAuditBridge/.github/workflows/codex_audit.yml@refs/heads/main,QuantStrategyLab/AIAuditBridge/.github/workflows/codex_pr_review.yml@refs/heads/main,QuantStrategyLab/QuantRuntimeSettings/.github/workflows/codex_pr_review.yml@refs/heads/main,QuantStrategyLab/QuantPlatformKit/.github/workflows/codex_pr_review.yml@refs/heads/main}"
ALLOWED_REFS="${CODEX_AUDIT_SERVICE_ALLOWED_REFS:-refs/heads/main}"
ALLOWED_REPOSITORY_VISIBILITIES="${CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES:-public}"
ALLOWED_SOURCE_REPOSITORIES="${CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES:-QuantStrategyLab/AIAuditBridge,QuantStrategyLab/QuantRuntimeSettings,QuantStrategyLab/QuantPlatformKit,QuantStrategyLab/CryptoLivePoolPipelines,QuantStrategyLab/HkEquitySnapshotPipelines,QuantStrategyLab/UsEquitySnapshotPipelines,QuantStrategyLab/ResearchSignalContextPipelines}"
JOB_DIR="${CODEX_AUDIT_SERVICE_JOB_DIR:-/var/lib/codex-audit-bridge/jobs}"
ADMIN_ENV_FILE="${CODEX_AUDIT_SERVICE_ADMIN_ENV_FILE:-/etc/codex-audit-bridge/admin.env}"
EXECUTION_POLICY_FILE="${CODEX_AUDIT_SERVICE_EXECUTION_POLICY_PATH:-/etc/codex-audit-bridge-policy/execution_policy.json}"
AUDIT_MODEL="${CODEX_AUDIT_SERVICE_MODEL:-}"
AUDIT_REASONING_EFFORT="${CODEX_AUDIT_SERVICE_REASONING_EFFORT:-}"
CODEX_ACCOUNT_USAGE="${CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_USAGE:-1}"
OPENAI_USAGE_WINDOW_DAYS="${CODEX_AUDIT_SERVICE_OPENAI_USAGE_WINDOW_DAYS:-7}"
ANTHROPIC_USAGE_WINDOW_DAYS="${CODEX_AUDIT_SERVICE_ANTHROPIC_USAGE_WINDOW_DAYS:-7}"
NGINX_CONFIG="${CODEX_AUDIT_SERVICE_NGINX_CONFIG:-}"

require_sudo() {
  if ! sudo -n true; then
    echo "sudo without password is required on the self-hosted runner" >&2
    exit 1
  fi
}

nginx_bin() {
  if command -v nginx >/dev/null 2>&1; then
    command -v nginx
  elif [ -x /usr/sbin/nginx ]; then
    echo "/usr/sbin/nginx"
  fi
}

mask_infra() {
  sed -E \
    -e 's/[0-9]{1,3}(\.[0-9]{1,3}){3}\.sslip\.io/[public-service-host]/g' \
    -e 's/\b[0-9]{1,3}(\.[0-9]{1,3}){3}\b/[ip-address]/g'
}

systemctl_status_brief() {
  local service="$1"
  if systemctl list-unit-files "${service}.service" >/dev/null 2>&1; then
    echo "### ${service}.service"
    systemctl is-enabled "$service" 2>/dev/null || true
    systemctl is-active "$service" 2>/dev/null || true
    systemctl status "$service" --no-pager --lines=8 2>/dev/null | mask_infra || true
  fi
}

systemctl_environment_brief() {
  local service="$1"
  if systemctl list-unit-files "${service}.service" >/dev/null 2>&1; then
    echo "### ${service}.service environment"
    systemctl show "$service" --property=Environment --no-pager 2>/dev/null \
      | sed 's/^Environment=//' \
      | tr ' ' '\n' \
      | sed -E "s/^[\"']//; s/[\"']$//" \
      | grep -E '^CODEX_AUDIT_SERVICE_(ALLOWED_|AUDIENCE=|HOST=|PORT=|JOB_DIR=|QUOTA_STORE=|EXECUTION_POLICY_PATH=|CODEX_ACCOUNT_USAGE=|OPENAI_USAGE_WINDOW_DAYS=|ANTHROPIC_USAGE_WINDOW_DAYS=|SANDBOX=|MODEL=|REASONING_EFFORT=)' \
      | mask_infra || true
  fi
}

sshd_bin() {
  if command -v sshd >/dev/null 2>&1; then
    command -v sshd
  elif [ -x /usr/sbin/sshd ]; then
    echo "/usr/sbin/sshd"
  fi
}

ssh_service_name() {
  if systemctl list-unit-files ssh.service >/dev/null 2>&1; then
    echo "ssh"
  elif systemctl list-unit-files sshd.service >/dev/null 2>&1; then
    echo "sshd"
  fi
}

is_ssh_port_listening() {
  ss -H -ltn "sport = :22" 2>/dev/null | grep -q .
}

detect_nginx_config() {
  if [ -n "$NGINX_CONFIG" ]; then
    echo "$NGINX_CONFIG"
    return
  fi
  for candidate in \
    /etc/nginx/sites-available/codex-gateway.conf \
    /etc/nginx/sites-enabled/codex-gateway.conf; do
    if sudo test -f "$candidate"; then
      echo "$candidate"
      return
    fi
  done
  echo "Could not find codex gateway nginx config. Set CODEX_AUDIT_SERVICE_NGINX_CONFIG." >&2
  exit 1
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
  nginx_bin || true
  command -v caddy || true
  echo

  echo "## Listening ports"
  ss -ltnp 2>/dev/null | grep -E ':(22|80|443|8787|8797)\b' | mask_infra || true
  echo

  echo "## Codex process hints"
  ps -eo pid,ppid,user,comm,args | grep -E '(codex[_-]|codex gateway|codex-audit|codex_service|8787|8797)' | grep -v grep | mask_infra || true
  echo

  echo "## Codex unit files"
  systemctl list-units --all --type=service 2>/dev/null | grep -i codex | mask_infra || true
  systemctl list-unit-files 2>/dev/null | grep -i codex | mask_infra || true
  echo

  echo "## Services"
  systemctl_status_brief ssh
  systemctl_status_brief sshd
  systemctl_status_brief codex-gateway
  systemctl_status_brief "$AUDIT_SERVICE_NAME"
  systemctl_status_brief nginx
  systemctl_status_brief caddy
  echo

  echo "## Codex audit service environment"
  systemctl_environment_brief "$AUDIT_SERVICE_NAME"
  echo

  echo "## SSH access hints"
  local sshd
  sshd="$(sshd_bin || true)"
  if [ -n "$sshd" ]; then
    sudo "$sshd" -T 2>/dev/null \
      | grep -E '^(port|permitrootlogin|passwordauthentication|pubkeyauthentication|authorizedkeysfile) ' \
      | mask_infra || true
  else
    echo "sshd binary not found"
  fi
  if command -v ufw >/dev/null 2>&1; then
    sudo ufw status verbose 2>/dev/null | mask_infra || true
  fi
  if command -v fail2ban-client >/dev/null 2>&1; then
    sudo fail2ban-client status 2>/dev/null | mask_infra || true
    sudo fail2ban-client status sshd 2>/dev/null | mask_infra || true
  fi
  echo

  echo "## Reverse proxy hints"
  local nginx
  nginx="$(nginx_bin || true)"
  if [ -n "$nginx" ]; then
    sudo "$nginx" -T 2>/dev/null \
      | grep -nE 'server_name|listen|location|proxy_pass|codex|8787|8797|sslip' \
      | head -180 \
      | mask_infra || true
  fi
}

install_file() {
  local source="$1"
  local target="$2"
  local mode="$3"
  sudo install -D -m "$mode" "$source" "$target"
}

install_service_package() {
  sudo rm -rf "${DEPLOY_DIR}/service"
  sudo install -d -m 0755 "${DEPLOY_DIR}/service"
  sudo cp -R service/. "${DEPLOY_DIR}/service/"
  sudo find "${DEPLOY_DIR}/service" -type d -exec chmod 0755 {} +
  sudo find "${DEPLOY_DIR}/service" -type f -exec chmod 0644 {} +
}

write_admin_env_file_if_needed() {
  if [ -z "${OPENAI_ADMIN_KEY:-}" ] && [ -z "${ANTHROPIC_ADMIN_KEY:-}" ]; then
    sudo rm -f "$ADMIN_ENV_FILE"
    return
  fi
  local tmp
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' RETURN
  chmod 0600 "$tmp"
  {
    if [ -n "${OPENAI_ADMIN_KEY:-}" ]; then
      printf 'OPENAI_ADMIN_KEY=%s\n' "$OPENAI_ADMIN_KEY"
    fi
    if [ -n "${ANTHROPIC_ADMIN_KEY:-}" ]; then
      printf 'ANTHROPIC_ADMIN_KEY=%s\n' "$ANTHROPIC_ADMIN_KEY"
    fi
  } >"$tmp"
  sudo install -d -m 0700 "$(dirname "$ADMIN_ENV_FILE")"
  sudo install -m 0600 -o root -g root "$tmp" "$ADMIN_ENV_FILE"
  rm -f "$tmp"
  trap - RETURN
}

write_default_execution_policy_if_missing() {
  local policy_path="${EXECUTION_POLICY_FILE}"
  local policy_dir
  policy_dir="$(dirname "$policy_path")"
  if [ -L "$policy_path" ]; then
    echo "refusing to write execution policy through symlink: $policy_path" >&2
    exit 1
  fi
  if [ -e "$policy_path" ]; then
    return
  fi
  sudo python3 - "$policy_path" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
if not os.path.isabs(path):
    print(f"refusing to write execution policy to relative path: {path}", file=sys.stderr)
    raise SystemExit(1)

policy_dir, policy_name = os.path.split(path)
if not policy_dir or not policy_name:
    print(f"invalid execution policy path: {path}", file=sys.stderr)
    raise SystemExit(1)

flags_dir = os.O_RDONLY | os.O_DIRECTORY
if hasattr(os, "O_NOFOLLOW"):
    flags_dir |= os.O_NOFOLLOW


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def ensure_trusted_dir(fd: int, label: str, *, created: bool) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        fail(f"execution policy parent path is not a directory: {label}")
    if created:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o755)
        info = os.fstat(fd)
    if (info.st_uid, info.st_gid) != (0, 0):
        fail(f"execution policy parent directory owner is invalid: {label}")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        fail(f"execution policy parent directory permissions are too broad: {label}")


def open_admin_policy_dir(directory: str) -> int:
    fd = os.open(os.sep, flags_dir)
    ensure_trusted_dir(fd, os.sep, created=False)
    for component in [part for part in directory.split(os.sep) if part]:
        if component in {".", ".."}:
            fail(f"invalid execution policy directory component: {component}")
        created = False
        try:
            next_fd = os.open(component, flags_dir, dir_fd=fd)
        except FileNotFoundError:
            os.mkdir(component, 0o755, dir_fd=fd)
            created = True
            next_fd = os.open(component, flags_dir, dir_fd=fd)
        except OSError as exc:
            fail(f"refusing to write execution policy under unsafe directory component {component}: {exc}")
        try:
            ensure_trusted_dir(next_fd, component, created=created)
        finally:
            os.close(fd)
        fd = next_fd
    return fd


policy_dir_fd = open_admin_policy_dir(policy_dir)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
content = """{
  "default": {
    "max_autonomy": "auto_pr",
    "max_consecutive_failures": 3,
    "low_cost_model": "gpt-5.4-mini",
    "low_cost_provider": "openai"
  },
  "repositories": {}
}
"""
try:
    fd = os.open(policy_name, flags, 0o600, dir_fd=policy_dir_fd)
except FileExistsError:
    if os.path.islink(path):
        print(f"refusing to write execution policy through symlink: {path}", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0)
finally:
    os.close(policy_dir_fd)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        fd = -1
        handle.write(content)
        handle.flush()
        os.fchown(handle.fileno(), 0, 0)
        os.fchmod(handle.fileno(), 0o644)
except Exception:
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    raise
PY
}

write_audit_service_unit() {
  local runner_user runner_home
  runner_user="$(id -un)"
  runner_home="$(getent passwd "$runner_user" | cut -d: -f6)"
  audit_model_line=""
  if [ -n "$AUDIT_MODEL" ]; then
    audit_model_line="Environment=CODEX_AUDIT_SERVICE_MODEL=${AUDIT_MODEL}"
  fi
  audit_reasoning_effort_line=""
  if [ -n "$AUDIT_REASONING_EFFORT" ]; then
    audit_reasoning_effort_line="Environment=CODEX_AUDIT_SERVICE_REASONING_EFFORT=${AUDIT_REASONING_EFFORT}"
  fi
  audit_token_line=""
  if [ -n "${CODEX_AUDIT_SERVICE_TOKEN:-}" ]; then
    audit_token_line="Environment=CODEX_AUDIT_SERVICE_TOKEN=${CODEX_AUDIT_SERVICE_TOKEN}"
  fi
  sudo tee "/etc/systemd/system/${AUDIT_SERVICE_NAME}.service" >/dev/null <<EOF_UNIT
[Unit]
Description=QuantStrategyLab Codex audit service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${runner_user}
WorkingDirectory=${DEPLOY_DIR}
Environment=HOME=${runner_home}
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=CODEX_AUDIT_SERVICE_HOST=127.0.0.1
Environment=CODEX_AUDIT_SERVICE_PORT=${AUDIT_PORT}
Environment=CODEX_AUDIT_SERVICE_AUDIENCE=${AUDIENCE}
Environment=CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=${ALLOWED_REPOSITORIES}
Environment=CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS=${ALLOWED_WORKFLOW_REFS}
Environment=CODEX_AUDIT_SERVICE_ALLOWED_REFS=${ALLOWED_REFS}
Environment=CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES=${ALLOWED_REPOSITORY_VISIBILITIES}
Environment=CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES=${ALLOWED_SOURCE_REPOSITORIES}
Environment=CODEX_AUDIT_SERVICE_JOB_DIR=${JOB_DIR}
Environment=CODEX_AUDIT_SERVICE_QUOTA_STORE=${JOB_DIR}/quota.json
Environment=CODEX_AUDIT_SERVICE_EXECUTION_POLICY_PATH=${EXECUTION_POLICY_FILE}
Environment=CODEX_AUDIT_SERVICE_CODEX_ACCOUNT_USAGE=${CODEX_ACCOUNT_USAGE}
Environment=CODEX_AUDIT_SERVICE_OPENAI_USAGE_WINDOW_DAYS=${OPENAI_USAGE_WINDOW_DAYS}
Environment=CODEX_AUDIT_SERVICE_ANTHROPIC_USAGE_WINDOW_DAYS=${ANTHROPIC_USAGE_WINDOW_DAYS}
Environment=CODEX_AUDIT_SERVICE_SANDBOX=read-only
EnvironmentFile=-${ADMIN_ENV_FILE}
${audit_model_line}
${audit_reasoning_effort_line}
${audit_token_line}
ExecStart=/usr/bin/env python3 -m service.ai_gateway_service
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF_UNIT
}

write_managed_audit_service_dropin() {
  local dropin_dir="/etc/systemd/system/${AUDIT_SERVICE_NAME}.service.d"
  sudo install -d -m 0755 "$dropin_dir"
  sudo tee "${dropin_dir}/zzzz-managed-allowlists.conf" >/dev/null <<EOF_DROPIN
[Service]
# Managed by scripts/deploy_codex_audit_service.sh; parsed after legacy drop-ins.
Environment="CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORIES=${ALLOWED_REPOSITORIES}"
Environment="CODEX_AUDIT_SERVICE_ALLOWED_WORKFLOW_REFS=${ALLOWED_WORKFLOW_REFS}"
Environment="CODEX_AUDIT_SERVICE_ALLOWED_REFS=${ALLOWED_REFS}"
Environment="CODEX_AUDIT_SERVICE_ALLOWED_REPOSITORY_VISIBILITIES=${ALLOWED_REPOSITORY_VISIBILITIES}"
Environment="CODEX_AUDIT_SERVICE_ALLOWED_SOURCE_REPOSITORIES=${ALLOWED_SOURCE_REPOSITORIES}"
Environment="CODEX_AUDIT_SERVICE_EXECUTION_POLICY_PATH=${EXECUTION_POLICY_FILE}"
EOF_DROPIN
}

configure_nginx_codex_audit_route() {
  local config="$1"
  local nginx="$2"
  local backup
  backup="$(sudo python3 - "$config" "$AUDIT_PORT" <<'PY'
from __future__ import annotations

from pathlib import Path
import re
import sys
import time

path = Path(sys.argv[1])
port = sys.argv[2]
text = path.read_text(encoding="utf-8")
backup = path.with_name(f"{path.name}.codex-audit-backup-{int(time.time())}")
backup.write_text(text, encoding="utf-8")

start_marker = "# CodexAuditBridge route start"
end_marker = "# CodexAuditBridge route end"
text = re.sub(
    rf"\n?\s*{re.escape(start_marker)}\n.*?\s*{re.escape(end_marker)}\n",
    "\n",
    text,
    flags=re.S,
)

route_template = """
{indent}# CodexAuditBridge route start
{indent}location = /v1/codex-audit {{
{indent}    proxy_pass http://127.0.0.1:{port};
{indent}    proxy_http_version 1.1;
{indent}    proxy_set_header Host $host;
{indent}    proxy_set_header X-Real-IP $remote_addr;
{indent}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
{indent}    proxy_set_header X-Forwarded-Proto https;
{indent}    proxy_read_timeout 3600s;
{indent}    proxy_send_timeout 3600s;
{indent}}}
{indent}location ^~ /v1/codex-audit/ {{
{indent}    proxy_pass http://127.0.0.1:{port};
{indent}    proxy_http_version 1.1;
{indent}    proxy_set_header Host $host;
{indent}    proxy_set_header X-Real-IP $remote_addr;
{indent}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
{indent}    proxy_set_header X-Forwarded-Proto https;
{indent}    proxy_read_timeout 3600s;
{indent}    proxy_send_timeout 3600s;
{indent}}}
{indent}location ^~ /v1/ai/ {{
{indent}    proxy_pass http://127.0.0.1:{port};
{indent}    proxy_http_version 1.1;
{indent}    proxy_set_header Host $host;
{indent}    proxy_set_header X-Real-IP $remote_addr;
{indent}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
{indent}    proxy_set_header X-Forwarded-Proto https;
{indent}    proxy_read_timeout 3600s;
{indent}    proxy_send_timeout 3600s;
{indent}}}
{indent}# CodexAuditBridge route end
"""


def server_blocks(source: str):
    for match in re.finditer(r"\bserver\s*\{", source):
        open_brace = source.find("{", match.start())
        depth = 0
        for index in range(open_brace, len(source)):
            char = source[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield match.start(), index + 1, source[match.start() : index + 1]
                    break

for block_start, block_end, block in server_blocks(text):
    if not re.search(r"\blisten\s+443\b", block):
        continue
    location = re.search(r"(?m)^(\s*)location\s+/\s*\{", block)
    if location:
        indent = location.group(1)
        insertion = block_start + location.start()
    else:
        closing = block.rfind("}")
        if closing < 0:
            continue
        indent = "    "
        insertion = block_start + closing
    route = route_template.format(indent=indent, port=port)
    text = text[:insertion] + route + text[insertion:]
    path.write_text(text, encoding="utf-8")
    print(backup)
    raise SystemExit(0)

raise SystemExit("Could not find an nginx server block listening on 443")
PY
)"
  if ! sudo "$nginx" -t; then
    echo "nginx config test failed; restoring previous config" >&2
    sudo cp "$backup" "$config"
    sudo "$nginx" -t || true
    exit 1
  fi
  sudo systemctl reload nginx
}

verify_local_service() {
  python3 - <<PY
import json
import time
import urllib.error
import urllib.request

last_error = None
for _ in range(30):
    try:
        with urllib.request.urlopen("http://127.0.0.1:${AUDIT_PORT}/healthz", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("status") in {"ok", "healthy"}:
            break
        last_error = RuntimeError(f"unexpected health response: {payload}")
    except Exception as exc:  # noqa: BLE001 - deployment readiness probe.
        last_error = exc
    time.sleep(1)
else:
    raise SystemExit(f"audit service did not become healthy: {last_error}")
print("audit service health ok")

request = urllib.request.Request(
    "http://127.0.0.1:${AUDIT_PORT}/v1/codex-audit/jobs",
    data=b"{}",
    method="POST",
    headers={"Content-Type": "application/json"},
)
try:
    urllib.request.urlopen(request, timeout=10)
except urllib.error.HTTPError as exc:
    if exc.code != 401:
        raise SystemExit(f"expected 401 from unauthenticated audit service, got {exc.code}") from exc
else:
    raise SystemExit("expected unauthenticated async audit service request to fail")
print("audit service auth check ok")
PY
}

verify_public_route_if_possible() {
  local config="$1"
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found; skipped public route probe"
    return
  fi
  local public_host
  public_host="$(sudo python3 - "$config" <<'PY'
from __future__ import annotations

from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")


def server_blocks(source: str):
    for match in re.finditer(r"\bserver\s*\{", source):
        open_brace = source.find("{", match.start())
        depth = 0
        for index in range(open_brace, len(source)):
            char = source[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield source[match.start() : index + 1]
                    break


for block in server_blocks(text):
    if not re.search(r"\blisten\s+443\b", block):
        continue
    if "# CodexAuditBridge route start" not in block:
        continue
    match = re.search(r"\bserver_name\s+([^;\s]+)", block)
    if match and match.group(1) not in {"_", "localhost"}:
        print(match.group(1))
        break
PY
)"
  if [ -z "$public_host" ]; then
    echo "public host not found in nginx config; skipped public route probe"
    return
  fi
  local response_file status_code
  response_file="$(mktemp)"
  status_code="$(curl -sk -o "$response_file" -w '%{http_code}' \
    -X POST \
    -H 'Content-Type: application/json' \
    --data '{}' \
    "https://${public_host}/v1/codex-audit/jobs" || true)"
  rm -f "$response_file"
  if [ "$status_code" != "401" ]; then
    echo "expected public /v1/codex-audit/jobs to return 401 without bearer token, got ${status_code}" >&2
    exit 1
  fi
  echo "public async audit route auth check ok"
}

deploy() {
  require_sudo
  local nginx config
  nginx="$(nginx_bin || true)"
  if [ -z "$nginx" ]; then
    echo "nginx was not found on this host" >&2
    exit 1
  fi
  config="$(detect_nginx_config)"
  local runner_user
  runner_user="$(id -un)"

  install_file "scripts/codex_audit_service.py" "${DEPLOY_DIR}/scripts/codex_audit_service.py" "0755"
  install_service_package
  sudo install -d -m 0700 -o "$runner_user" -g "$runner_user" "$JOB_DIR"
  write_default_execution_policy_if_missing
  write_admin_env_file_if_needed
  write_audit_service_unit
  write_managed_audit_service_dropin
  sudo systemctl daemon-reload
  sudo systemctl enable --now "$AUDIT_SERVICE_NAME"
  sudo systemctl restart "$AUDIT_SERVICE_NAME"

  verify_local_service
  configure_nginx_codex_audit_route "$config" "$nginx"
  verify_public_route_if_possible "$config"
}

repair_ssh() {
  require_sudo

  local sshd service unban_ip
  sshd="$(sshd_bin || true)"
  if [ -z "$sshd" ] && command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server
    sshd="$(sshd_bin || true)"
  fi
  if [ -z "$sshd" ]; then
    echo "sshd binary was not found after repair attempt" >&2
    exit 1
  fi

  service="$(ssh_service_name || true)"
  if [ -z "$service" ]; then
    echo "ssh/sshd systemd service was not found" >&2
    exit 1
  fi

  sudo "$sshd" -t
  sudo systemctl unmask "$service" || true
  sudo systemctl enable "$service"
  if ! systemctl is-active --quiet "$service"; then
    sudo systemctl start "$service"
  elif ! is_ssh_port_listening; then
    sudo systemctl restart "$service"
  fi

  if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow OpenSSH >/dev/null 2>&1 || sudo ufw allow 22/tcp >/dev/null 2>&1 || true
  fi

  unban_ip="${CODEX_AUDIT_SSH_UNBAN_IP:-}"
  if [ -n "$unban_ip" ] && command -v fail2ban-client >/dev/null 2>&1; then
    sudo fail2ban-client set sshd unbanip "$unban_ip" >/dev/null 2>&1 || true
  fi

  inspect
}

case "$MODE" in
  inspect)
    inspect
    ;;
  deploy)
    deploy
    ;;
  repair-ssh)
    repair_ssh
    ;;
  *)
    echo "usage: $0 [inspect|deploy|repair-ssh]" >&2
    exit 2
    ;;
esac
